from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .archive import (
    ArchiveIntegrityError,
    ArchiveMissingDownloadUrlError,
    ArchivePersistenceError,
    archive_raw_webhook,
    extract_radarr_push_payload,
    parse_strict_json_object,
    read_verified_archive_json,
    verify_archive_root,
)
from .classifier import classify_webhook_payload
from .clients import (
    ActionsApiClient,
    ShadowStubActionsClient,
    ShadowStubTriageClient,
    TriageApiClient,
)
from .config import IntakeConfig
from .db import IntakeDB, generate_uuid7, utc_now, utc_now_iso
from .security import constant_time_equals, install_log_redaction, redact_secrets
from .tmdb_verifier import TmdbIndependentClient
from .torrentleech import TorrentLeechScraper
from .worker import IntakeWorker

logger = logging.getLogger(__name__)


class PushPayloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_occurrence_id: str = Field(..., min_length=1, max_length=128)


def _is_within_dir(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _enforce_shadow_isolation(
    cfg: IntakeConfig,
    triage: TriageApiClient,
    actions: ActionsApiClient,
    *,
    tl_scraper_injected: bool = False,
) -> None:
    """
    Hard handover invariant: Shadow replay MUST use isolated state directories and
    stubbed external writers producing zero external mutations.
    """
    default_prod_state = Path.home() / ".local" / "state" / "movie-intel"
    default_prod_config = Path.home() / ".config" / "movie-intel"
    if (
        _is_within_dir(cfg.intake_db_path, default_prod_state)
        or _is_within_dir(cfg.webhook_archive_dir, default_prod_state)
        or _is_within_dir(cfg.tmdb_cache_path, default_prod_state)
    ):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true requires isolated INTAKE_DB_PATH, WEBHOOK_ARCHIVE_DIR, "
            "and TMDB_CACHE_PATH outside the production ~/.local/state/movie-intel directory"
        )
    if (
        cfg.tl_metadata_enabled
        and not tl_scraper_injected
        and _is_within_dir(cfg.tl_cookie_file, default_prod_config)
    ):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true with MOVIE_INTEL_TL_METADATA_ENABLED=true refuses "
            "the production ~/.config/movie-intel cookie file; disable TL scraping or inject a shadow stub"
        )
    if not getattr(triage, "is_shadow_stub", False):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true requires a shadow-stubbed triage client (zero external writes)"
        )
    if not getattr(actions, "is_shadow_stub", False):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true requires a shadow-stubbed actions client (zero external writes)"
        )


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Enforce body size cap on both Content-Length and chunked streaming reads."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        if declared < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")
        if declared > max_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")

    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
    return bytes(buf)


def create_app(
    config: Optional[IntakeConfig] = None,
    *,
    db: Optional[IntakeDB] = None,
    tl_scraper: Optional[TorrentLeechScraper] = None,
    tmdb_client: Optional[TmdbIndependentClient] = None,
    triage_client: Optional[TriageApiClient] = None,
    actions_client: Optional[ActionsApiClient] = None,
    start_worker: bool = True,
) -> FastAPI:
    cfg = config or IntakeConfig.from_env()
    install_log_redaction()

    intake_db = db or IntakeDB(cfg.intake_db_path)
    scraper = tl_scraper or TorrentLeechScraper(
        db=intake_db,
        cookie_file=cfg.tl_cookie_file,
        enabled=cfg.tl_metadata_enabled,
        min_interval_seconds=cfg.tl_min_interval_seconds,
    )
    tmdb = tmdb_client or TmdbIndependentClient(
        api_token=cfg.tmdb_api_token,
        cache_path=cfg.tmdb_cache_path,
    )
    if cfg.shadow_replay_enabled:
        triage = triage_client if triage_client is not None else ShadowStubTriageClient()
        actions = actions_client if actions_client is not None else ShadowStubActionsClient()
        _enforce_shadow_isolation(
            cfg,
            triage,
            actions,
            tl_scraper_injected=tl_scraper is not None,
        )
    else:
        triage = triage_client or TriageApiClient(
            base_url=cfg.triage_api_url,
            internal_token=cfg.internal_api_secret,
        )
        actions = actions_client or ActionsApiClient(
            base_url=cfg.actions_api_url,
            internal_token=cfg.internal_api_secret,
        )

    worker = IntakeWorker(
        config=cfg,
        db=intake_db,
        tl_scraper=scraper,
        tmdb_client=tmdb,
        triage_client=triage,
        actions_client=actions,
    )

    def require_internal_token(
        x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    ) -> None:
        # Fail closed: /internal/* (including on-demand downloadUrl extraction) is NEVER unauthenticated
        if not cfg.internal_api_secret:
            raise HTTPException(status_code=503, detail="Internal API secret not configured")
        if not constant_time_equals(x_internal_token, cfg.internal_api_secret):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await asyncio.to_thread(intake_db.init_db)
        await asyncio.to_thread(verify_archive_root, cfg.webhook_archive_dir)
        if start_worker:
            await worker.start()
        try:
            yield
        finally:
            if start_worker:
                await worker.stop()

    app = FastAPI(
        title="Movie Intake API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.db = intake_db
    app.state.worker = worker
    app.state.triage_client = triage
    app.state.actions_client = actions

    async def _ingest_raw_bytes(raw_bytes: bytes) -> dict[str, Any]:
        try:
            parsed = parse_strict_json_object(raw_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=redact_secrets(str(exc), max_chars=200)) from exc

        occurrence_id = generate_uuid7()
        received_dt = utc_now()
        received_iso = utc_now_iso(received_dt)

        # Invariant: Persist exact raw JSON delivery atomically BEFORE business filtering
        try:
            receipt = await asyncio.to_thread(
                archive_raw_webhook,
                cfg.webhook_archive_dir,
                occurrence_id,
                raw_bytes,
                received_at=received_dt,
            )
        except ArchivePersistenceError as exc:
            logger.error("Required raw archive persistence failed: %s", redact_secrets(str(exc), max_chars=160))
            raise HTTPException(status_code=503, detail="Archive persistence unavailable") from exc

        classified = classify_webhook_payload(
            parsed,
            allowed_filter_names=cfg.allowed_filter_names,
            allowed_filter_ids=cfg.allowed_filter_ids,
        )
        if classified.classification != "movie_candidate":
            if classified.classification == "movie_category_type_conflict":
                logger.warning(
                    "Movie category with conflicting release type (diagnostic only): occ=%s note=%s",
                    occurrence_id,
                    redact_secrets(classified.diagnostic_note, max_chars=160),
                )
            claim = await asyncio.to_thread(
                intake_db.record_non_movie_occurrence,
                occurrence_id=occurrence_id,
                received_at_iso=received_iso,
                receipt=receipt,
                classification=classified.classification,
                diagnostic_note=classified.diagnostic_note,
            )
            return {
                "status": claim.status,
                "occurrence_id": claim.occurrence_id,
                "release_id": None,
            }

        claim = await asyncio.to_thread(
            intake_db.record_movie_occurrence,
            occurrence_id=occurrence_id,
            received_at_iso=received_iso,
            receipt=receipt,
            classified=classified,
        )
        if claim.status in ("accepted", "updated_correction"):
            worker.notify()
        return {
            "status": claim.status,
            "occurrence_id": claim.occurrence_id,
            "release_id": claim.release_id,
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        counts = await asyncio.to_thread(intake_db.get_health_counts)
        archive_ok = await asyncio.to_thread(verify_archive_root, cfg.webhook_archive_dir)
        return {
            "status": "ok",
            "port": cfg.port,
            "queued_releases": counts["queued_releases"],
            "in_flight_releases": counts["in_flight_releases"],
            "ambiguous_deliveries": counts["ambiguous_deliveries"],
            "tl_cooldown_active": counts["tl_cooldown_active"],
            "archive_dir_ok": archive_ok,
            "webhook_configured": bool(cfg.webhook_secret),
            "internal_auth_configured": bool(cfg.internal_api_secret),
            "shadow_replay_enabled": cfg.shadow_replay_enabled,
        }

    @app.post("/webhook/{secret}")
    async def receive_webhook(secret: str, request: Request) -> dict[str, Any]:
        # Handover contract: missing webhook secret -> 503; wrong secret -> 404
        if not cfg.webhook_secret:
            raise HTTPException(status_code=503, detail="Webhook endpoint not configured")
        if not constant_time_equals(secret, cfg.webhook_secret):
            raise HTTPException(status_code=404, detail="Not found")

        raw_bytes = await _read_bounded_body(request, cfg.max_webhook_body_bytes)
        return await _ingest_raw_bytes(raw_bytes)

    @app.post(
        "/internal/releases/{release_id}/push-payload",
        dependencies=[Depends(require_internal_token)],
    )
    async def get_push_payload(
        release_id: str,
        body: PushPayloadRequest,
    ) -> Response:
        """
        On-demand ephemeral resolution of Radarr /api/v3/release/push payload from the
        verified 0600 private archive. Called ONLY by movie-actions-api on confirmed grab.
        """
        ref = await asyncio.to_thread(
            intake_db.get_occurrence_archive_ref,
            release_id,
            body.source_occurrence_id,
        )
        if ref is None:
            raise HTTPException(status_code=404, detail="Release occurrence not found")

        resolved_occurrence_id = body.source_occurrence_id
        try:
            raw_payload = await asyncio.to_thread(
                read_verified_archive_json,
                cfg.webhook_archive_dir,
                ref.archive_relpath,
                ref.archive_sha256,
            )
            try:
                push_payload = extract_radarr_push_payload(
                    raw_payload,
                    indexer_norm=ref.indexer_norm,
                    external_id_norm=ref.external_id_norm,
                    exact_release_name=ref.exact_release_name,
                    fallback_publish_date=ref.received_at,
                )
            except ArchiveMissingDownloadUrlError:
                # Requested occurrence passed SHA-256 integrity verification but had no URL;
                # if a later coalesced duplicate of the SAME release supplied a usable URL,
                # verify and extract from releases.source_occurrence_id.
                rel_row = await asyncio.to_thread(intake_db.get_release, release_id)
                fallback_occ_id = str(rel_row["source_occurrence_id"]) if rel_row else ""
                if not fallback_occ_id or fallback_occ_id == body.source_occurrence_id:
                    raise
                fallback_ref = await asyncio.to_thread(
                    intake_db.get_occurrence_archive_ref,
                    release_id,
                    fallback_occ_id,
                )
                if fallback_ref is None:
                    raise
                fallback_raw = await asyncio.to_thread(
                    read_verified_archive_json,
                    cfg.webhook_archive_dir,
                    fallback_ref.archive_relpath,
                    fallback_ref.archive_sha256,
                )
                push_payload = extract_radarr_push_payload(
                    fallback_raw,
                    indexer_norm=fallback_ref.indexer_norm,
                    external_id_norm=fallback_ref.external_id_norm,
                    exact_release_name=fallback_ref.exact_release_name,
                    fallback_publish_date=fallback_ref.received_at,
                )
                resolved_occurrence_id = fallback_occ_id
        except ArchiveIntegrityError as exc:
            logger.error(
                "Private archive verification failed for release=%s: %s",
                release_id,
                redact_secrets(str(exc), max_chars=160),
            )
            raise HTTPException(status_code=409, detail="Archive payload verification failed") from exc

        return JSONResponse(
            content={
                "release_id": release_id,
                "source_occurrence_id": resolved_occurrence_id,
                "push_payload": push_payload,
            },
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    @app.post(
        "/internal/replay",
        dependencies=[Depends(require_internal_token)],
    )
    async def shadow_replay(request: Request) -> dict[str, Any]:
        """Shadow-mode deterministic replay endpoint (disabled in production; stubbed writers only)."""
        if not cfg.shadow_replay_enabled:
            raise HTTPException(status_code=404, detail="Not found")
        if not getattr(triage, "is_shadow_stub", False) or not getattr(actions, "is_shadow_stub", False):
            raise HTTPException(
                status_code=503,
                detail="Shadow replay refused: external writers are not stubbed",
            )
        raw_bytes = await _read_bounded_body(request, cfg.max_webhook_body_bytes)
        ingest_result = await _ingest_raw_bytes(raw_bytes)
        if ingest_result.get("release_id") and not start_worker:
            await worker.process_next_queued()
        release_state = None
        if ingest_result.get("release_id"):
            release_state = await asyncio.to_thread(
                intake_db.get_release,
                str(ingest_result["release_id"]),
            )
        return {
            "ingest": ingest_result,
            "release": release_state,
        }

    return app


def build_default_app() -> FastAPI:
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists() and not env_file.is_symlink():
        load_dotenv(env_file)
    return create_app()
