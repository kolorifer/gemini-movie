from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .alternative_grab import (
    QBittorrentClient,
    ShadowStubQbitClient,
    ShadowStubTorrentFetcher,
    TorrentFetcher,
)
from .clients import (
    IntakePushClient,
    RadarrClient,
    ShadowStubIntakePushClient,
    ShadowStubRadarrClient,
    ShadowStubTelegramClient,
    ShadowStubTriageFeedbackClient,
    TelegramClient,
    TriageFeedbackClient,
)
from .config import MAX_ACTION_FORM_BYTES, ActionsConfig, AlternativeGrabConfig
from .db import ActionsDB
from .html_views import SECURITY_HEADERS, render_simple_status_page
from .security import constant_time_equals, install_log_redaction, redact_secrets
from .service import ActionsService, DeliveryPreconditionError, DeliveryValidationError

logger = logging.getLogger(__name__)


def _is_within_dir(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _enforce_shadow_isolation(
    cfg: ActionsConfig,
    radarr: RadarrClient,
    telegram: TelegramClient,
    intake_push: IntakePushClient,
    triage_feedback: TriageFeedbackClient,
    *,
    alt_grab_cfg: Optional[AlternativeGrabConfig] = None,
    torrent_fetcher: Optional[TorrentFetcher] = None,
    qbit_client: Optional[QBittorrentClient] = None,
) -> None:
    """Shadow mode MUST use isolated state outside ~/.local/state/movie-intel and zero-mutation stubs."""
    default_prod_state = Path.home() / ".local" / "state" / "movie-intel"
    default_prod_config = Path.home() / ".config" / "movie-intel"
    if _is_within_dir(cfg.actions_db_path, default_prod_state):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true requires ACTIONS_DB_PATH outside production ~/.local/state/movie-intel"
        )
    if alt_grab_cfg is not None and (
        _is_within_dir(alt_grab_cfg.state_dir, default_prod_state)
        or _is_within_dir(alt_grab_cfg.state_dir, default_prod_config)
    ):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true requires Alternative Grab receipt state dir outside production movie-intel directories"
        )
    for label, client in (
        ("radarr", radarr),
        ("telegram", telegram),
        ("intake_push", intake_push),
        ("triage_feedback", triage_feedback),
    ):
        if not getattr(client, "is_shadow_stub", False):
            raise ValueError(
                f"SHADOW_REPLAY_ENABLED=true requires a shadow-stubbed {label} client (zero external mutations)"
            )
    if torrent_fetcher is not None and not getattr(torrent_fetcher, "is_shadow_stub", False):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true requires a shadow-stubbed torrent_fetcher client (zero external network fetches)"
        )
    if qbit_client is not None and not getattr(qbit_client, "is_shadow_stub", False):
        raise ValueError(
            "SHADOW_REPLAY_ENABLED=true requires a shadow-stubbed qbit_client (zero external mutations)"
        )


async def _read_bounded_form(request: Request, max_bytes: int) -> dict[str, str]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        if declared < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")
        if declared > max_bytes:
            raise HTTPException(status_code=413, detail="Form payload too large")

    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(status_code=413, detail="Form payload too large")

    try:
        text = bytes(buf).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid UTF-8 form body") from exc

    parsed = parse_qs(text, keep_blank_values=True, max_num_fields=32)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def create_app(
    config: Optional[ActionsConfig] = None,
    *,
    db: Optional[ActionsDB] = None,
    process_instance_id: Optional[str] = None,
    radarr_client: Optional[RadarrClient] = None,
    telegram_client: Optional[TelegramClient] = None,
    intake_push_client: Optional[IntakePushClient] = None,
    triage_feedback_client: Optional[TriageFeedbackClient] = None,
    alt_grab_config: Optional[AlternativeGrabConfig] = None,
    torrent_fetcher: Optional[TorrentFetcher] = None,
    qbit_client: Optional[QBittorrentClient] = None,
) -> FastAPI:
    cfg = config or ActionsConfig.from_env()
    install_log_redaction()

    instance_id = process_instance_id or str(uuid.uuid4())
    actions_db = db or ActionsDB(cfg.actions_db_path)
    alt_cfg = (
        alt_grab_config
        if alt_grab_config is not None
        else AlternativeGrabConfig.load_from_file(cfg.alternative_grab_config_path)
    )

    if cfg.shadow_replay_enabled:
        radarr = radarr_client if radarr_client is not None else ShadowStubRadarrClient()
        telegram = telegram_client if telegram_client is not None else ShadowStubTelegramClient()
        intake_push = (
            intake_push_client if intake_push_client is not None else ShadowStubIntakePushClient()
        )
        triage_feedback = (
            triage_feedback_client
            if triage_feedback_client is not None
            else ShadowStubTriageFeedbackClient()
        )
        fetcher = torrent_fetcher if torrent_fetcher is not None else ShadowStubTorrentFetcher()
        qbit = (
            qbit_client
            if qbit_client is not None
            else (ShadowStubQbitClient(category=alt_cfg.category) if alt_cfg is not None else None)
        )
        _enforce_shadow_isolation(
            cfg,
            radarr,
            telegram,
            intake_push,
            triage_feedback,
            alt_grab_cfg=alt_cfg,
            torrent_fetcher=fetcher,
            qbit_client=qbit,
        )
    else:
        radarr = radarr_client or RadarrClient(
            base_url=cfg.radarr_url,
            api_key=cfg.radarr_api_key,
            timeout_seconds=cfg.radarr_timeout_seconds,
        )
        telegram = telegram_client or TelegramClient(
            bot_token=cfg.telegram_bot_token,
            timeout_seconds=cfg.telegram_timeout_seconds,
        )
        intake_push = intake_push_client or IntakePushClient(
            base_url=cfg.intake_api_url,
            internal_token=cfg.internal_api_secret,
            timeout_seconds=cfg.intake_timeout_seconds,
        )
        triage_feedback = triage_feedback_client or TriageFeedbackClient(
            base_url=cfg.triage_api_url,
            internal_token=cfg.internal_api_secret,
            timeout_seconds=cfg.triage_timeout_seconds,
        )
        fetcher = torrent_fetcher or TorrentFetcher(timeout_seconds=cfg.radarr_timeout_seconds)
        qbit = qbit_client or (
            QBittorrentClient(
                base_url=alt_cfg.url,
                username=alt_cfg.username,
                password=alt_cfg.password,
                timeout_seconds=cfg.radarr_timeout_seconds,
            )
            if alt_cfg is not None
            else None
        )

    service = ActionsService(
        config=cfg,
        db=actions_db,
        process_instance_id=instance_id,
        radarr_client=radarr,
        telegram_client=telegram,
        intake_push_client=intake_push,
        triage_feedback_client=triage_feedback,
        alt_grab_config=alt_cfg,
        torrent_fetcher=fetcher,
        qbit_client=qbit,
    )

    def require_internal_token(
        x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    ) -> None:
        if not cfg.internal_api_secret:
            raise HTTPException(status_code=503, detail="Internal API secret not configured")
        if not constant_time_equals(x_internal_token, cfg.internal_api_secret):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await asyncio.to_thread(actions_db.init_db)
        # §6.4 Synchronous orphan recovery sweep BEFORE accepting HTTP traffic
        recovery = await asyncio.to_thread(actions_db.recover_orphaned_state, instance_id)
        if any(recovery.values()):
            logger.warning("Startup orphan recovery promoted records: %s", recovery)
        yield

    app = FastAPI(
        title="Movie Actions API",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.db = actions_db
    app.state.process_instance_id = instance_id
    app.state.service = service
    app.state.radarr_client = radarr
    app.state.telegram_client = telegram
    app.state.intake_push_client = intake_push
    app.state.triage_feedback_client = triage_feedback
    app.state.alt_grab_config = alt_cfg
    app.state.torrent_fetcher = fetcher
    app.state.qbit_client = qbit

    @app.get("/health")
    async def health() -> dict[str, Any]:
        counts = await asyncio.to_thread(actions_db.get_health_counts)
        return {
            "status": "ok",
            "port": cfg.port,
            "pending_ambiguous_deliveries": counts["pending_ambiguous_deliveries"],
            "pending_ambiguous_replies": counts["pending_ambiguous_replies"],
            "pending_ambiguous_radarr": counts["pending_ambiguous_radarr"],
            "active_actions": counts["active_actions"],
            "internal_auth_configured": bool(cfg.internal_api_secret),
            "alternative_grab_configured": service.alternative_grab_configured,
            "shadow_replay_enabled": cfg.shadow_replay_enabled,
        }

    @app.get(
        "/internal/capabilities",
        dependencies=[Depends(require_internal_token)],
    )
    async def internal_capabilities() -> dict[str, Any]:
        return {
            "alternative_grab_configured": service.alternative_grab_configured,
        }

    @app.get(
        "/internal/radarr/status",
        dependencies=[Depends(require_internal_token)],
    )
    async def radarr_status(
        tmdb_id: Optional[int] = Query(default=None, ge=1),
        imdb_id: Optional[str] = Query(default=None, max_length=32),
        release_title: Optional[str] = Query(default=None, max_length=300),
    ) -> dict[str, Any]:
        return await service.get_radarr_status(
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            release_title=release_title,
        )

    @app.post(
        "/internal/deliver/card",
        dependencies=[Depends(require_internal_token)],
    )
    async def deliver_card(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await service.deliver_card(payload)
        except DeliveryValidationError as exc:
            raise HTTPException(
                status_code=400, detail=redact_secrets(str(exc), max_chars=200)
            ) from exc

    @app.post(
        "/internal/deliver/evaluation",
        dependencies=[Depends(require_internal_token)],
    )
    async def deliver_evaluation(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await service.deliver_evaluation(payload)
        except DeliveryValidationError as exc:
            raise HTTPException(
                status_code=400, detail=redact_secrets(str(exc), max_chars=200)
            ) from exc
        except DeliveryPreconditionError as exc:
            raise HTTPException(
                status_code=409, detail=redact_secrets(str(exc), max_chars=200)
            ) from exc

    @app.get("/action/{token}", response_class=HTMLResponse)
    async def action_confirm_get(token: str) -> HTMLResponse:
        if not token or len(token) > 256:
            return HTMLResponse(
                content=render_simple_status_page(
                    title="Action Not Found", badge="404", message="Invalid action token.", tone="err"
                ),
                status_code=404,
                headers=SECURITY_HEADERS,
            )
        page = await service.render_action_get(token)
        return HTMLResponse(content=page.html, status_code=page.status_code, headers=SECURITY_HEADERS)

    @app.post("/action/{token}", response_class=HTMLResponse)
    async def action_execute_post(token: str, request: Request) -> HTMLResponse:
        if not token or len(token) > 256:
            return HTMLResponse(
                content=render_simple_status_page(
                    title="Action Not Found", badge="404", message="Invalid action token.", tone="err"
                ),
                status_code=404,
                headers=SECURITY_HEADERS,
            )
        form_data = await _read_bounded_form(request, MAX_ACTION_FORM_BYTES)
        page = await service.handle_action_post(token, form_data)
        return HTMLResponse(content=page.html, status_code=page.status_code, headers=SECURITY_HEADERS)

    return app


def build_default_app() -> FastAPI:
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists() and not env_file.is_symlink():
        load_dotenv(env_file)
    return create_app()
