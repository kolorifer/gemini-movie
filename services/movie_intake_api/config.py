from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

MAX_WEBHOOK_BODY_BYTES = 1_000_000
MAX_TL_BODY_BYTES = 1_000_000
MAX_TL_COOKIE_BYTES = 16_384

ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _parse_bool(value: Optional[str], *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    low = value.strip().lower()
    if low in {"1", "true", "yes", "on"}:
        return True
    if low in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean setting: {value!r}")


def _validate_absolute_path_override(env_name: str, raw: Optional[str], default: Path) -> Path:
    """
    Validate configured file/directory path overrides per docs/LEGACY_HANDOVER.md:
    configured overrides MUST be absolute paths (rejecting relative paths and unexpanded '~').
    """
    if raw is None or not raw.strip():
        return default
    cleaned = raw.strip()
    if cleaned.startswith("~") or not os.path.isabs(cleaned):
        raise ValueError(
            f"{env_name} must be an absolute path when explicitly configured (got {cleaned!r})"
        )
    return Path(cleaned)


def _parse_filter_names(raw: Optional[str]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ("Movies Remux",)
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise ValueError("MOVIE_INTEL_ALLOWED_FILTERS cannot be empty when configured")
    return names


def _parse_filter_ids(raw: Optional[str]) -> tuple[int, ...]:
    if raw is None or not raw.strip():
        return ()
    ids: list[int] = []
    for part in raw.split(","):
        cleaned = part.strip()
        if not cleaned:
            continue
        val = int(cleaned)
        if val < 1:
            raise ValueError(f"MOVIE_INTEL_ALLOWED_FILTER_IDS entries must be positive integers, got {cleaned!r}")
        ids.append(val)
    return tuple(ids)


@dataclass(frozen=True)
class IntakeConfig:
    host: str
    port: int
    webhook_secret: str
    internal_api_secret: str
    tl_metadata_enabled: bool
    tl_cookie_file: Path
    tmdb_api_token: str
    tmdb_cache_path: Path
    webhook_archive_dir: Path
    intake_db_path: Path
    triage_api_url: str
    actions_api_url: str
    shadow_replay_enabled: bool = False
    worker_lease_seconds: int = 90
    worker_poll_interval_seconds: float = 2.0
    tl_min_interval_seconds: float = 1.5
    retry_backoff_seconds: float = 30.0
    max_webhook_body_bytes: int = MAX_WEBHOOK_BODY_BYTES
    allowed_filter_names: tuple[str, ...] = ("Movies Remux",)
    allowed_filter_ids: tuple[int, ...] = ()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "IntakeConfig":
        source = env if env is not None else os.environ
        home = Path.home()

        host = (source.get("MOVIE_INTEL_HOST") or "127.0.0.1").strip()
        if host not in ALLOWED_BIND_HOSTS:
            raise ValueError(
                f"MOVIE_INTEL_HOST must bind to loopback ({sorted(ALLOWED_BIND_HOSTS)}), got {host!r}"
            )

        port_raw = (source.get("MOVIE_INTEL_PORT") or "40720").strip()
        port = int(port_raw)
        if not (1024 <= port <= 65535):
            raise ValueError(f"MOVIE_INTEL_PORT must be between 1024 and 65535, got {port}")

        default_state = home / ".local" / "state" / "movie-intel"
        default_config = home / ".config" / "movie-intel"

        tl_cookie_file = _validate_absolute_path_override(
            "MOVIE_INTEL_TL_COOKIE_FILE",
            source.get("MOVIE_INTEL_TL_COOKIE_FILE"),
            default_config / "torrentleech-cookies-pr42.json",
        )
        tmdb_cache_path = _validate_absolute_path_override(
            "TMDB_CACHE_PATH",
            source.get("TMDB_CACHE_PATH"),
            default_state / "intake-tmdb-cache.json",
        )
        webhook_archive_dir = _validate_absolute_path_override(
            "WEBHOOK_ARCHIVE_DIR",
            source.get("WEBHOOK_ARCHIVE_DIR"),
            default_state / "webhook-archive",
        )
        intake_db_path = _validate_absolute_path_override(
            "INTAKE_DB_PATH",
            source.get("INTAKE_DB_PATH"),
            default_state / "intake.db",
        )

        triage_url = (source.get("TRIAGE_API_URL") or "http://127.0.0.1:40716").strip().rstrip("/")
        actions_url = (source.get("ACTIONS_API_URL") or "http://127.0.0.1:40721").strip().rstrip("/")

        return cls(
            host=host,
            port=port,
            webhook_secret=(source.get("MOVIE_INTEL_WEBHOOK_SECRET") or "").strip(),
            internal_api_secret=(source.get("INTERNAL_API_SECRET") or "").strip(),
            tl_metadata_enabled=_parse_bool(source.get("MOVIE_INTEL_TL_METADATA_ENABLED"), default=True),
            tl_cookie_file=tl_cookie_file,
            tmdb_api_token=(source.get("TMDB_API_TOKEN") or "").strip(),
            tmdb_cache_path=tmdb_cache_path,
            webhook_archive_dir=webhook_archive_dir,
            intake_db_path=intake_db_path,
            triage_api_url=triage_url,
            actions_api_url=actions_url,
            shadow_replay_enabled=_parse_bool(source.get("SHADOW_REPLAY_ENABLED"), default=False),
            worker_lease_seconds=max(30, int((source.get("WORKER_LEASE_SECONDS") or "90").strip())),
            worker_poll_interval_seconds=max(
                0.05, float((source.get("WORKER_POLL_INTERVAL_SECONDS") or "2.0").strip())
            ),
            tl_min_interval_seconds=max(
                0.0, float((source.get("TL_MIN_INTERVAL_SECONDS") or "1.5").strip())
            ),
            retry_backoff_seconds=max(
                0.0, float((source.get("RETRY_BACKOFF_SECONDS") or "30.0").strip())
            ),
            allowed_filter_names=_parse_filter_names(source.get("MOVIE_INTEL_ALLOWED_FILTERS")),
            allowed_filter_ids=_parse_filter_ids(source.get("MOVIE_INTEL_ALLOWED_FILTER_IDS")),
        )
