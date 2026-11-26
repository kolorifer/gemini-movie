from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse

from .security import read_owner_private_regular_file

MAX_POLICY_CONFIG_BYTES = 65_536
MAX_ACTION_FORM_BYTES = 16_384

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


def _parse_optional_int(env_name: str, raw: Optional[str]) -> Optional[int]:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        val = int(text)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be an integer, got {raw!r}") from exc
    if val < 0:
        raise ValueError(f"{env_name} must be >= 0, got {val}")
    return val


def _validate_absolute_path_override(env_name: str, raw: Optional[str], default: Path) -> Path:
    if raw is None or not raw.strip():
        return default
    cleaned = raw.strip()
    if cleaned.startswith("~") or not os.path.isabs(cleaned):
        raise ValueError(
            f"{env_name} must be an absolute path when explicitly configured (got {cleaned!r})"
        )
    return Path(cleaned)


def _validate_public_base_url(raw: str) -> str:
    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError(f"MOVIE_INTEL_PUBLIC_BASE_URL must be an absolute http(s) URL, got {raw!r}")
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in ALLOWED_BIND_HOSTS:
        raise ValueError(
            "MOVIE_INTEL_PUBLIC_BASE_URL must use https:// (http:// is allowed only for loopback testing)"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("MOVIE_INTEL_PUBLIC_BASE_URL must not contain query parameters or fragments")
    return cleaned


def _validate_service_url(env_name: str, raw: str) -> str:
    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{env_name} must be an absolute http(s) URL, got {raw!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{env_name} must not contain query parameters or fragments")
    return cleaned


@dataclass(frozen=True)
class AlternativeGrabConfig:
    """
    Live Ultra.cc Alternative Grab ("Grab Anyway") configuration held exclusively by
    movie-actions-api (owner-only 0600 JSON with keys: url, username, password, category, state).
    """

    url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    category: str = ""
    state_dir: Path = field(default_factory=lambda: Path("/nonexistent"))

    @classmethod
    def load_from_file(cls, path: Optional[Path]) -> Optional["AlternativeGrabConfig"]:
        if path is None:
            return None
        raw_bytes = read_owner_private_regular_file(path, max_bytes=MAX_POLICY_CONFIG_BYTES)
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG must contain a JSON object")

        required = ("url", "username", "password", "category", "state")
        values: dict[str, str] = {}
        for key in required:
            raw_val = parsed.get(key)
            if not isinstance(raw_val, str) or not raw_val.strip():
                raise ValueError(f"MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG missing non-empty string field: {key}")
            val = raw_val.strip()
            if "\n" in val or "\r" in val or "\x00" in val:
                raise ValueError(f"MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG contains control characters in field: {key}")
            values[key] = val

        parsed_url = urlparse(values["url"].rstrip("/"))
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise ValueError("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG.url must be an HTTPS URL")
        if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError(
                "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG.url must not contain userinfo, query parameters, or fragments"
            )

        state_raw = values["state"]
        if state_raw.startswith("~") or not os.path.isabs(state_raw):
            raise ValueError("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG.state must be an absolute path")

        return cls(
            url=values["url"].rstrip("/"),
            username=values["username"],
            password=values["password"],
            category=values["category"],
            state_dir=Path(state_raw),
        )


@dataclass(frozen=True)
class ActionsConfig:
    host: str
    port: int
    public_base_url: str
    internal_api_secret: str
    radarr_url: str
    radarr_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_topic_id: Optional[int]
    telegram_add_grab_topic_id: Optional[int]
    telegram_grab_anyway_topic_id: Optional[int]
    action_ttl_seconds: int
    standalone_skip_enabled: bool
    alternative_grab_config_path: Optional[Path]
    actions_db_path: Path
    intake_api_url: str
    triage_api_url: str
    shadow_replay_enabled: bool = False
    radarr_timeout_seconds: float = 20.0
    telegram_timeout_seconds: float = 20.0
    triage_timeout_seconds: float = 15.0
    intake_timeout_seconds: float = 15.0
    write_lease_grace_seconds: float = 30.0

    def topic_for_delivery_kind(
        self, delivery_kind: str, *, preferred_topic_kind: Optional[str] = None
    ) -> Optional[int]:
        effective = delivery_kind
        if delivery_kind == "evidence_review" and preferred_topic_kind == "alternative_grab":
            effective = "alternative_grab"
        if effective == "actionable_grab":
            return (
                self.telegram_add_grab_topic_id
                if self.telegram_add_grab_topic_id is not None
                else self.telegram_topic_id
            )
        if effective == "alternative_grab":
            return (
                self.telegram_grab_anyway_topic_id
                if self.telegram_grab_anyway_topic_id is not None
                else self.telegram_topic_id
            )
        return self.telegram_topic_id

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "ActionsConfig":
        source = env if env is not None else os.environ
        home = Path.home()

        host = (source.get("ACTIONS_HOST") or "127.0.0.1").strip()
        if host not in ALLOWED_BIND_HOSTS:
            raise ValueError(
                f"ACTIONS_HOST must bind to loopback ({sorted(ALLOWED_BIND_HOSTS)}), got {host!r}"
            )

        port = int((source.get("ACTIONS_PORT") or "40721").strip())
        if not (1024 <= port <= 65535):
            raise ValueError(f"ACTIONS_PORT must be between 1024 and 65535, got {port}")

        default_state = home / ".local" / "state" / "movie-intel"
        default_config = home / ".config" / "movie-intel"

        actions_db_path = _validate_absolute_path_override(
            "ACTIONS_DB_PATH",
            source.get("ACTIONS_DB_PATH"),
            default_state / "actions.db",
        )

        alt_raw = (source.get("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG") or "").strip()
        alt_path: Optional[Path] = None
        if alt_raw:
            alt_path = _validate_absolute_path_override(
                "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG",
                alt_raw,
                default_config / "alternative-grab.json",
            )

        ttl = int((source.get("MOVIE_INTEL_ACTION_TTL_SECONDS") or "604800").strip())
        if ttl < 60:
            raise ValueError(f"MOVIE_INTEL_ACTION_TTL_SECONDS must be >= 60, got {ttl}")

        public_base = _validate_public_base_url(source.get("MOVIE_INTEL_PUBLIC_BASE_URL") or "")
        radarr_url = _validate_service_url("RADARR_URL", source.get("RADARR_URL") or "")
        intake_url = _validate_service_url(
            "INTAKE_API_URL", source.get("INTAKE_API_URL") or "http://127.0.0.1:40720"
        )
        triage_url = _validate_service_url(
            "TRIAGE_API_URL", source.get("TRIAGE_API_URL") or "http://127.0.0.1:40716"
        )

        return cls(
            host=host,
            port=port,
            public_base_url=public_base,
            internal_api_secret=(source.get("INTERNAL_API_SECRET") or "").strip(),
            radarr_url=radarr_url,
            radarr_api_key=(source.get("RADARR_API_KEY") or "").strip(),
            telegram_bot_token=(source.get("TELEGRAM_BOT_TOKEN") or "").strip(),
            telegram_chat_id=(source.get("TELEGRAM_CHAT_ID") or "").strip(),
            telegram_topic_id=_parse_optional_int("TELEGRAM_TOPIC_ID", source.get("TELEGRAM_TOPIC_ID")),
            telegram_add_grab_topic_id=_parse_optional_int(
                "TELEGRAM_ADD_GRAB_TOPIC_ID", source.get("TELEGRAM_ADD_GRAB_TOPIC_ID")
            ),
            telegram_grab_anyway_topic_id=_parse_optional_int(
                "TELEGRAM_GRAB_ANYWAY_TOPIC_ID", source.get("TELEGRAM_GRAB_ANYWAY_TOPIC_ID")
            ),
            action_ttl_seconds=ttl,
            standalone_skip_enabled=_parse_bool(
                source.get("MOVIE_INTEL_STANDALONE_SKIP_ENABLED"), default=True
            ),
            alternative_grab_config_path=alt_path,
            actions_db_path=actions_db_path,
            intake_api_url=intake_url,
            triage_api_url=triage_url,
            shadow_replay_enabled=_parse_bool(
                source.get("SHADOW_REPLAY_ENABLED"), default=False
            ),
            radarr_timeout_seconds=max(
                1.0, float((source.get("RADARR_TIMEOUT_SECONDS") or "20.0").strip())
            ),
            telegram_timeout_seconds=max(
                1.0, float((source.get("TELEGRAM_TIMEOUT_SECONDS") or "20.0").strip())
            ),
            triage_timeout_seconds=max(
                1.0, float((source.get("TRIAGE_TIMEOUT_SECONDS") or "15.0").strip())
            ),
            intake_timeout_seconds=max(
                1.0, float((source.get("INTAKE_TIMEOUT_SECONDS") or "15.0").strip())
            ),
            write_lease_grace_seconds=max(
                5.0, float((source.get("WRITE_LEASE_GRACE_SECONDS") or "30.0").strip())
            ),
        )
