from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

from db import TriageDB
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
from tmdb_client import TmdbClient, apply_tmdb_verification, is_tmdb_compatible

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
PORT = int(os.getenv("PORT", "8000"))
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "72"))

db = TriageDB(BASE_DIR / "data" / "triage.db")
gemini_client = GeminiTriageClient(
    api_key=os.getenv("GEMINI_API_KEY", "").strip(),
    model=GEMINI_MODEL,
    taste_profile_path=BASE_DIR / "taste_profile.txt",
)
tmdb_client = TmdbClient(api_key=os.getenv("TMDB_API_KEY", "").strip())


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Movie Triage API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        model=GEMINI_MODEL,
        port=PORT,
        cache_entries=db.get_cache_entries_count(ttl_hours=CACHE_TTL_HOURS),
        tmdb_enabled=tmdb_client.enabled,
    )


@app.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(payload: EvaluateRequest) -> EvaluateResponse:
    if not payload.force_refresh:
        cached = db.get_cached_evaluation(
            payload.raw_text,
            hint_title=payload.hint_title,
            hint_year=payload.hint_year,
            extra_context=payload.extra_context,
            ttl_hours=CACHE_TTL_HOURS,
        )
        if cached is not None:
            return EvaluateResponse(cached=True, **cached)

    tmdb_movie = await tmdb_client.lookup_from_request(payload)

    # Cross-language/alias cache check via deterministic TMDB -> IMDb ID resolution
    if not payload.force_refresh and tmdb_movie is not None and tmdb_movie.imdb_id:
        cached_by_imdb = db.get_cached_evaluation(
            payload.raw_text,
            hint_title=tmdb_movie.canonical_title,
            hint_year=tmdb_movie.year,
            extra_context=tmdb_movie.imdb_id,
            ttl_hours=CACHE_TTL_HOURS,
        )
        if cached_by_imdb is not None:
            return EvaluateResponse(cached=True, **cached_by_imdb)

    recent_feedback = db.get_recent_feedback(limit=20)
    tmdb_context = tmdb_movie.to_prompt_block() if tmdb_movie is not None else None
    try:
        result = await gemini_client.evaluate(
            payload,
            recent_feedback,
            tmdb_context=tmdb_context,
        )
    except GeminiTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except GeminiClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if tmdb_client.enabled and (
        tmdb_movie is None or not is_tmdb_compatible(result.identification, tmdb_movie)
    ):
        tmdb_movie = await tmdb_client.verify_identification(result.identification)

    verified_result = apply_tmdb_verification(result, tmdb_movie)
    result_dict = verified_result.model_dump()
    db.save_evaluation(
        payload.raw_text,
        result_dict,
        hint_title=payload.hint_title,
        hint_year=payload.hint_year,
    )
    return EvaluateResponse(cached=False, **result_dict)


@app.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(payload: FeedbackRequest) -> FeedbackResponse:
    total = db.add_feedback(
        imdb_id=payload.imdb_id,
        title=payload.title,
        decision=payload.decision,
        note=payload.note,
    )
    return FeedbackResponse(
        status="saved",
        decision=payload.decision,
        total_feedback=total,
    )


@app.get("/feedback", response_model=list[FeedbackItem])
async def list_feedback(limit: int = Query(default=20, ge=1, le=50)) -> list[FeedbackItem]:
    rows = db.get_recent_feedback(limit=limit)
    return [FeedbackItem(**row) for row in rows]


@app.get("/recent", response_model=list[RecentEvaluationItem])
async def recent(limit: int = Query(default=20, ge=1, le=100)) -> list[RecentEvaluationItem]:
    rows = db.get_recent_evaluations(limit=limit)
    return [RecentEvaluationItem(**row) for row in rows]
