from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .security import read_owner_private_regular_file

MAX_WEBHOOK_BODY_BYTES = 1_000_000
MAX_TL_BODY_BYTES = 1_000_000
MAX_TL_COOKIE_BYTES = 16_384
MAX_POLICY_CONFIG_BYTES = 65_536

ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
QUALITY_ONLY_REJECTION_CLASSES = frozenset(
    {"cutoff_met", "not_upgrade", "quality_unwanted", "custom_format_score", "size_limit"}
)


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


@dataclass(frozen=True)
class AlternativeGrabPolicy:
    enabled: bool = False
    allowed_rejection_classes: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def load_from_file(cls, path: Optional[Path]) -> "AlternativeGrabPolicy":
        if path is None:
            return cls(enabled=False, allowed_rejection_classes=frozenset())
        raw_bytes = read_owner_private_regular_file(path, max_bytes=MAX_POLICY_CONFIG_BYTES)
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG must contain a JSON object")
        enabled = bool(parsed.get("enabled", True))
        raw_classes = parsed.get("allowed_rejection_classes") or []
        if not isinstance(raw_classes, list):
            raise ValueError("allowed_rejection_classes must be a list of strings")
        normalized = {
            str(item).strip().lower()
            for item in raw_classes
            if isinstance(item, str) and item.strip()
        }
        # Hard invariant: Alternative Grab is strictly a quality-only exception;
        # never permit identity or unknown rejection classes regardless of file content.
        invalid = normalized - QUALITY_ONLY_REJECTION_CLASSES
        if invalid:
            raise ValueError(
                f"Alternative Grab config contains non-quality rejection classes: {sorted(invalid)}"
            )
        return cls(enabled=enabled and bool(normalized), allowed_rejection_classes=frozenset(normalized))


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
    alternative_grab_config_path: Optional[Path]
    triage_api_url: str
    actions_api_url: str
    shadow_replay_enabled: bool = False
    worker_lease_seconds: int = 90
    worker_poll_interval_seconds: float = 2.0
    tl_min_interval_seconds: float = 1.5
    max_webhook_body_bytes: int = MAX_WEBHOOK_BODY_BYTES

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
            default_state / "tmdb-cache.json",
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

        alt_raw = (source.get("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG") or "").strip()
        alt_path: Optional[Path] = None
        if alt_raw:
            alt_path = _validate_absolute_path_override(
                "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG",
                alt_raw,
                default_config / "alternative-grab.json",
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
            alternative_grab_config_path=alt_path,
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
        )
