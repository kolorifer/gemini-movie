from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query

from db import TriageDB, parse_release_quality
from gemini_client import GeminiClientError, GeminiTimeoutError, GeminiTriageClient
from schemas import (
    EvaluateRequest,
    EvaluateResponse,
    FeedbackItem,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    RecentEvaluationItem,
)
from tmdb_client import (
    TmdbClient,
    apply_tmdb_verification,
    compute_evidence_key,
    format_torrent_info_for_prompt,
    is_tmdb_compatible,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip()
PORT = int(os.getenv("PORT", "8000"))
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "72"))
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "").strip()

db = TriageDB(BASE_DIR / "data" / "triage.db")
gemini_client = GeminiTriageClient(
    api_key=os.getenv("GEMINI_API_KEY", "").strip(),
    model=GEMINI_MODEL,
    taste_profile_path=BASE_DIR / "taste_profile.txt",
)
tmdb_client = TmdbClient(api_key=os.getenv("TMDB_API_KEY", "").strip())


def require_internal_token(
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
) -> None:
    expected = INTERNAL_API_SECRET.strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Service misconfigured: INTERNAL_API_SECRET is required",
        )
    if not x_internal_token or not hmac.compare_digest(
        x_internal_token.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(db.init_db)
    if not INTERNAL_API_SECRET.strip():
        logger.error(
            "INTERNAL_API_SECRET is not set; protected endpoints will fail closed with HTTP 503 until configured"
        )
    yield


app = FastAPI(
    title="Movie Triage API",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    cache_entries = await asyncio.to_thread(
        db.get_cache_entries_count, ttl_hours=CACHE_TTL_HOURS
    )
    return HealthResponse(
        status="ok",
        model=GEMINI_MODEL,
        port=PORT,
        cache_entries=cache_entries,
        tmdb_enabled=tmdb_client.enabled,
        auth_enabled=bool(INTERNAL_API_SECRET.strip()),
    )


@app.post(
    "/evaluate",
    response_model=EvaluateResponse,
    dependencies=[Depends(require_internal_token)],
)
async def evaluate(payload: EvaluateRequest) -> EvaluateResponse:
    tinfo = payload.torrent_info
    release_quality = parse_release_quality(
        payload.raw_text,
        resolution=tinfo.resolution if tinfo else None,
        source=tinfo.source if tinfo else None,
        dynamic_range=tinfo.dynamic_range if tinfo else None,
        media_processing=tinfo.media_processing if tinfo else None,
        group=tinfo.group if tinfo else None,
        nfo_text=tinfo.nfo_text if tinfo else None,
        description=tinfo.description if tinfo else None,
    )
    evidence_key = compute_evidence_key(payload)

    if not payload.force_refresh:
        cached = await asyncio.to_thread(
            db.get_cached_evaluation,
            payload.raw_text,
            hint_title=payload.hint_title,
            hint_year=payload.hint_year,
            extra_context=payload.extra_context,
            quality_key=release_quality.cache_key,
            edition_key=release_quality.edition_key,
            evidence_key=evidence_key,
            allow_imdb_lookup=False,
            allow_release_key_fallback=False,
            ttl_hours=CACHE_TTL_HOURS,
        )
        if cached is not None:
            return EvaluateResponse(cached=True, **cached)

    lookup = await tmdb_client.lookup_with_diagnostics(payload)
    tmdb_movie = lookup.movie

    # Cross-language/alias cache check ONLY when TMDB lookup had no tracker conflict
    # and quality tier, edition, and supplemental evidence digest all match.
    if (
        not payload.force_refresh
        and lookup.trusted_for_cache
        and tmdb_movie is not None
        and tmdb_movie.imdb_id
    ):
        cached_by_imdb = await asyncio.to_thread(
            db.get_cached_evaluation,
            payload.raw_text,
            hint_title=tmdb_movie.canonical_title,
            hint_year=tmdb_movie.year,
            extra_context=tmdb_movie.imdb_id,
            quality_key=release_quality.cache_key,
            edition_key=release_quality.edition_key,
            evidence_key=evidence_key,
            allow_imdb_lookup=True,
            allow_release_key_fallback=False,
            ttl_hours=CACHE_TTL_HOURS,
        )
        if cached_by_imdb is not None:
            return EvaluateResponse(cached=True, **cached_by_imdb)

    recent_feedback = await asyncio.to_thread(db.get_recent_feedback, limit=20)
    tmdb_context = tmdb_movie.to_prompt_block() if tmdb_movie is not None else None
    torrent_context = format_torrent_info_for_prompt(
        payload.torrent_info,
        conflict_warning=lookup.tracker_conflict,
    )
    try:
        result = await gemini_client.evaluate(
            payload,
            recent_feedback,
            tmdb_context=tmdb_context,
            torrent_context=torrent_context,
            release_quality_context=release_quality.to_prompt_line(),
        )
    except GeminiTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except GeminiClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    release_tmdb = lookup.movie
    if release_tmdb is not None:
        # Blocker 1: Never replace a release-grounded catalogue identity with a
        # lookup of the model's competing suggestion. Keep both separate so any
        # disagreement returns an explicit conflict and withholds trusted caching.
        verified_result = apply_tmdb_verification(result, release_tmdb)
    else:
        verified_candidate = (
            await tmdb_client.verify_identification(result.identification)
            if tmdb_client.enabled
            else None
        )
        verified_result = apply_tmdb_verification(result, verified_candidate)

    result_dict = verified_result.model_dump()

    # Withhold caching when catalogue verification found an unresolved incompatibility
    has_catalogue_conflict = bool(
        verified_result.catalogue_identification is not None
        and not verified_result.catalogue_identification.compatible
    )
    if not has_catalogue_conflict:
        await asyncio.to_thread(
            db.save_evaluation,
            payload.raw_text,
            result_dict,
            hint_title=payload.hint_title,
            hint_year=payload.hint_year,
            quality_key=release_quality.cache_key,
            edition_key=release_quality.edition_key,
            evidence_key=evidence_key,
        )
    return EvaluateResponse(cached=False, **result_dict)


@app.post(
    "/feedback",
    response_model=FeedbackResponse,
    dependencies=[Depends(require_internal_token)],
)
async def submit_feedback(payload: FeedbackRequest) -> FeedbackResponse:
    total = await asyncio.to_thread(
        db.add_feedback,
        imdb_id=payload.imdb_id,
        tmdb_id=payload.tmdb_id,
        title=payload.title,
        decision=payload.decision,
        note=payload.note,
    )
    return FeedbackResponse(
        status="saved",
        decision=payload.decision,
        total_feedback=total,
    )


@app.get(
    "/feedback",
    response_model=list[FeedbackItem],
    dependencies=[Depends(require_internal_token)],
)
async def list_feedback(limit: int = Query(default=20, ge=1, le=50)) -> list[FeedbackItem]:
    rows = await asyncio.to_thread(db.get_recent_feedback, limit=limit)
    return [FeedbackItem(**row) for row in rows]


@app.get(
    "/recent",
    response_model=list[RecentEvaluationItem],
    dependencies=[Depends(require_internal_token)],
)
async def recent(limit: int = Query(default=20, ge=1, le=100)) -> list[RecentEvaluationItem]:
    rows = await asyncio.to_thread(db.get_recent_evaluations, limit=limit)
    return [RecentEvaluationItem(**row) for row in rows]
