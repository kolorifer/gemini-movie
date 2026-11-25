#!/usr/bin/env python3
"""
Least-privilege .env slicer for Ultra.cc non-root deployment of movie-intake-api and movie-actions-api.

Reads optional legacy ~/.config/movie-intel/environment and existing per-service .env files using
O_RDONLY | O_NOFOLLOW | O_NONBLOCK with fstat owner/mode (0600) checks, enforces strict per-service
key allowlists so secrets NEVER cross service boundaries, and writes 0600 .env files atomically.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Mapping, Optional

MAX_ENV_FILE_BYTES = 65_536

# Strict least-privilege key allowlists (per docs/ARCHITECTURE.md §§2.1 & 7.2)
INTAKE_ALLOWED_KEYS: tuple[str, ...] = (
    "MOVIE_INTEL_HOST",
    "MOVIE_INTEL_PORT",
    "MOVIE_INTEL_WEBHOOK_SECRET",
    "INTERNAL_API_SECRET",
    "MOVIE_INTEL_TL_METADATA_ENABLED",
    "MOVIE_INTEL_TL_COOKIE_FILE",
    "TMDB_API_TOKEN",
    "TMDB_CACHE_PATH",
    "WEBHOOK_ARCHIVE_DIR",
    "INTAKE_DB_PATH",
    "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG",
    "TRIAGE_API_URL",
    "ACTIONS_API_URL",
    "SHADOW_REPLAY_ENABLED",
)

ACTIONS_ALLOWED_KEYS: tuple[str, ...] = (
    "ACTIONS_HOST",
    "ACTIONS_PORT",
    "MOVIE_INTEL_PUBLIC_BASE_URL",
    "INTERNAL_API_SECRET",
    "RADARR_URL",
    "RADARR_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_TOPIC_ID",
    "TELEGRAM_ADD_GRAB_TOPIC_ID",
    "TELEGRAM_GRAB_ANYWAY_TOPIC_ID",
    "MOVIE_INTEL_ACTION_TTL_SECONDS",
    "MOVIE_INTEL_STANDALONE_SKIP_ENABLED",
    "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG",
    "ACTIONS_DB_PATH",
    "INTAKE_API_URL",
    "TRIAGE_API_URL",
    "SHADOW_REPLAY_ENABLED",
)

INTAKE_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"GEMINI_API_KEY", "RADARR_API_KEY", "TELEGRAM_BOT_TOKEN"}
)
ACTIONS_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"GEMINI_API_KEY", "MOVIE_INTEL_WEBHOOK_SECRET", "MOVIE_INTEL_TL_COOKIE_FILE", "TMDB_API_TOKEN"}
)


class EnvSecurityError(RuntimeError):
    """Raised when an input/output env file violates owner-private or formatting invariants."""


def _is_within_dir(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _validate_env_value(key: str, value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise EnvSecurityError(f"Unsafe control character in env value for {key}")
    return value


def read_owner_private_env(path: Path, *, required: bool = False) -> dict[str, str]:
    """
    Safely read a regular owner-only (no group/other bits) non-symlink KEY=VALUE file.
    Returns empty dict if missing and `required=False`.
    """
    if not path.is_absolute():
        raise EnvSecurityError(f"Env file path must be absolute: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(path), flags)
    except FileNotFoundError:
        if required:
            raise EnvSecurityError(f"Required env file not found: {path}")
        return {}
    except OSError as exc:
        raise EnvSecurityError(f"Refusing to open env file safely ({path.name}): {exc.__class__.__name__}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EnvSecurityError(f"Env path is not a regular file: {path.name}")
        if st.st_uid != os.getuid():
            raise EnvSecurityError(f"Env file is not owned by current user: {path.name}")
        if (st.st_mode & 0o077) != 0:
            raise EnvSecurityError(
                f"Env file permissions are too permissive (require chmod 600): {path.name}"
            )
        if st.st_size > MAX_ENV_FILE_BYTES:
            raise EnvSecurityError(f"Env file exceeds {MAX_ENV_FILE_BYTES} bytes: {path.name}")
        raw = bytearray()
        while True:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_ENV_FILE_BYTES:
                raise EnvSecurityError(f"Env file exceeded {MAX_ENV_FILE_BYTES} bytes while reading")
    finally:
        os.close(fd)

    result: dict[str, str] = {}
    for line in bytes(raw).decode("utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        key = k.strip()
        val = v.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            quote = val[0]
            val = val[1:-1]
            if quote == '"':
                val = re.sub(r'\\(["\\])', lambda m: m.group(1), val)
        if key:
            result[key] = _validate_env_value(key, val)
    return result


def _format_env_line(key: str, value: str) -> str:
    clean = _validate_env_value(key, value)
    if any(ch in clean for ch in (" ", "\t", "#", '"', "'", "\\")):
        escaped = clean.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f"{key}={clean}"


def atomic_write_env_file(target: Path, ordered_keys: tuple[str, ...], values: Mapping[str, str]) -> None:
    """Atomically write an owner-only 0600 .env file, refusing symlink targets."""
    if not target.is_absolute():
        raise EnvSecurityError(f"Target env path must be absolute: {target}")
    if os.path.islink(str(target.parent)):
        raise EnvSecurityError("Refusing symlinked parent directory for .env")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_st = os.lstat(str(target.parent))
    if not stat.S_ISDIR(parent_st.st_mode) or stat.S_ISLNK(parent_st.st_mode):
        raise EnvSecurityError("Env target parent is not a real directory")
    if parent_st.st_uid != os.getuid():
        raise EnvSecurityError("Env target parent is not owned by current user")
    if (parent_st.st_mode & 0o077) != 0:
        os.chmod(str(target.parent), 0o700)
    if os.path.islink(str(target)):
        raise EnvSecurityError(f"Refusing to overwrite symlinked env file: {target.name}")

    lines: list[str] = [
        "# Generated by deploy/deploy-suite-ultra.sh (chmod 600, least-privilege slice)",
    ]
    for key in ordered_keys:
        lines.append(_format_env_line(key, str(values.get(key, ""))))
    payload = ("\n".join(lines) + "\n").encode("utf-8")

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: Optional[int] = None
    tmp_path: Optional[Path] = None
    try:
        for attempt in range(64):
            cand = target.parent / f".{target.name}.{os.getpid()}.{attempt}.tmp"
            try:
                fd = os.open(str(cand), flags, 0o600)
                tmp_path = cand
                break
            except FileExistsError:
                continue
        if fd is None or tmp_path is None:
            raise EnvSecurityError("Unable to create temporary .env file")
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("Short write to .env temp file")
            written += n
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(str(tmp_path), str(target))
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        raise


def build_sliced_envs(
    *,
    home_dir: Path,
    port_intake: int,
    port_actions: int,
    port_triage: int,
    internal_api_secret: str,
    legacy_env: Mapping[str, str],
    existing_intake_env: Mapping[str, str],
    existing_actions_env: Mapping[str, str],
    override_env: Mapping[str, str],
    shadow_mode: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Build least-privilege (intake_env, actions_env) dictionaries strictly obeying
    docs/ARCHITECTURE.md §§2.1 & 7.2.
    Precedence: override_env > existing_<service>_env > legacy_env > safe default.
    In shadow_mode=True, ZERO live credentials or production paths are ever inherited.
    """
    if not (1024 <= port_intake <= 65535 and 1024 <= port_actions <= 65535 and 1024 <= port_triage <= 65535):
        raise ValueError("Ports must be between 1024 and 65535")
    if port_intake == port_actions or port_intake == port_triage or port_actions == port_triage:
        raise ValueError("Intake, Actions, and Triage ports must all be distinct")

    def _pick(key: str, existing: Mapping[str, str], default: str = "") -> str:
        for src in (override_env, existing, legacy_env):
            v = src.get(key)
            if v is not None and str(v).strip() != "":
                return _validate_env_value(key, str(v).strip())
        return _validate_env_value(key, default)

    secret = _validate_env_value("INTERNAL_API_SECRET", internal_api_secret.strip())
    if not secret:
        secret = secrets.token_urlsafe(32)

    if shadow_mode:
        state_root = home_dir / ".local" / "state" / "movie-intel-shadow"
        shadow_webhook = (
            existing_intake_env.get("MOVIE_INTEL_WEBHOOK_SECRET", "").strip()
            or f"shadow-{secrets.token_urlsafe(24)}"
        )
        intake_env = {
            "MOVIE_INTEL_HOST": "127.0.0.1",
            "MOVIE_INTEL_PORT": str(port_intake),
            "MOVIE_INTEL_WEBHOOK_SECRET": _validate_env_value("MOVIE_INTEL_WEBHOOK_SECRET", shadow_webhook),
            "INTERNAL_API_SECRET": secret,
            "MOVIE_INTEL_TL_METADATA_ENABLED": "false",
            "MOVIE_INTEL_TL_COOKIE_FILE": str(state_root / "shadow-unused-cookies.json"),
            "TMDB_API_TOKEN": "",
            "TMDB_CACHE_PATH": str(state_root / "tmdb-cache.json"),
            "WEBHOOK_ARCHIVE_DIR": str(state_root / "webhook-archive"),
            "INTAKE_DB_PATH": str(state_root / "intake.db"),
            "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG": "",
            "TRIAGE_API_URL": "http://shadow-triage.invalid",
            "ACTIONS_API_URL": f"http://127.0.0.1:{port_actions}",
            "SHADOW_REPLAY_ENABLED": "true",
        }
        actions_env = {
            "ACTIONS_HOST": "127.0.0.1",
            "ACTIONS_PORT": str(port_actions),
            "MOVIE_INTEL_PUBLIC_BASE_URL": "https://shadow.invalid/movie-intel",
            "INTERNAL_API_SECRET": secret,
            "RADARR_URL": "",
            "RADARR_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "-1000000000000",
            "TELEGRAM_TOPIC_ID": "",
            "TELEGRAM_ADD_GRAB_TOPIC_ID": "",
            "TELEGRAM_GRAB_ANYWAY_TOPIC_ID": "",
            "MOVIE_INTEL_ACTION_TTL_SECONDS": "604800",
            "MOVIE_INTEL_STANDALONE_SKIP_ENABLED": "true",
            "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG": "",
            "ACTIONS_DB_PATH": str(state_root / "actions.db"),
            "INTAKE_API_URL": f"http://127.0.0.1:{port_intake}",
            "TRIAGE_API_URL": "http://shadow-triage.invalid",
            "SHADOW_REPLAY_ENABLED": "true",
        }
        assert not (set(intake_env.keys()) & INTAKE_FORBIDDEN_KEYS)
        assert not (set(actions_env.keys()) & ACTIONS_FORBIDDEN_KEYS)
        return intake_env, actions_env

    state_root = home_dir / ".local" / "state" / "movie-intel"
    config_root = home_dir / ".config" / "movie-intel"
    tmdb_cache = _pick("TMDB_CACHE_PATH", existing_intake_env, str(state_root / "tmdb-cache.json"))
    archive_dir = _pick("WEBHOOK_ARCHIVE_DIR", existing_intake_env, str(state_root / "webhook-archive"))
    intake_db = _pick("INTAKE_DB_PATH", existing_intake_env, str(state_root / "intake.db"))
    actions_db = _pick("ACTIONS_DB_PATH", existing_actions_env, str(state_root / "actions.db"))
    tl_enabled = _pick("MOVIE_INTEL_TL_METADATA_ENABLED", existing_intake_env, "true")
    tl_cookie = _pick(
        "MOVIE_INTEL_TL_COOKIE_FILE",
        existing_intake_env,
        str(config_root / "torrentleech-cookies-pr42.json"),
    )
    alt_cfg = _pick("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG", existing_intake_env, "")
    if not alt_cfg:
        alt_cfg = _pick("MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG", existing_actions_env, "")

    intake_env: dict[str, str] = {
        "MOVIE_INTEL_HOST": "127.0.0.1",
        "MOVIE_INTEL_PORT": str(port_intake),
        "MOVIE_INTEL_WEBHOOK_SECRET": _pick("MOVIE_INTEL_WEBHOOK_SECRET", existing_intake_env, ""),
        "INTERNAL_API_SECRET": secret,
        "MOVIE_INTEL_TL_METADATA_ENABLED": tl_enabled,
        "MOVIE_INTEL_TL_COOKIE_FILE": tl_cookie,
        "TMDB_API_TOKEN": _pick("TMDB_API_TOKEN", existing_intake_env, ""),
        "TMDB_CACHE_PATH": tmdb_cache,
        "WEBHOOK_ARCHIVE_DIR": archive_dir,
        "INTAKE_DB_PATH": intake_db,
        "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG": alt_cfg,
        "TRIAGE_API_URL": f"http://127.0.0.1:{port_triage}",
        "ACTIONS_API_URL": f"http://127.0.0.1:{port_actions}",
        "SHADOW_REPLAY_ENABLED": "false",
    }

    actions_env: dict[str, str] = {
        "ACTIONS_HOST": "127.0.0.1",
        "ACTIONS_PORT": str(port_actions),
        "MOVIE_INTEL_PUBLIC_BASE_URL": _pick("MOVIE_INTEL_PUBLIC_BASE_URL", existing_actions_env, ""),
        "INTERNAL_API_SECRET": secret,
        "RADARR_URL": _pick("RADARR_URL", existing_actions_env, ""),
        "RADARR_API_KEY": _pick("RADARR_API_KEY", existing_actions_env, ""),
        "TELEGRAM_BOT_TOKEN": _pick("TELEGRAM_BOT_TOKEN", existing_actions_env, ""),
        "TELEGRAM_CHAT_ID": _pick("TELEGRAM_CHAT_ID", existing_actions_env, ""),
        "TELEGRAM_TOPIC_ID": _pick("TELEGRAM_TOPIC_ID", existing_actions_env, ""),
        "TELEGRAM_ADD_GRAB_TOPIC_ID": _pick("TELEGRAM_ADD_GRAB_TOPIC_ID", existing_actions_env, ""),
        "TELEGRAM_GRAB_ANYWAY_TOPIC_ID": _pick("TELEGRAM_GRAB_ANYWAY_TOPIC_ID", existing_actions_env, ""),
        "MOVIE_INTEL_ACTION_TTL_SECONDS": _pick("MOVIE_INTEL_ACTION_TTL_SECONDS", existing_actions_env, "604800"),
        "MOVIE_INTEL_STANDALONE_SKIP_ENABLED": _pick(
            "MOVIE_INTEL_STANDALONE_SKIP_ENABLED", existing_actions_env, "true"
        ),
        "MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG": alt_cfg,
        "ACTIONS_DB_PATH": actions_db,
        "INTAKE_API_URL": f"http://127.0.0.1:{port_intake}",
        "TRIAGE_API_URL": f"http://127.0.0.1:{port_triage}",
        "SHADOW_REPLAY_ENABLED": "false",
    }

    # Final boundary invariant check: forbidden secrets NEVER cross service lines
    assert not (set(intake_env.keys()) & INTAKE_FORBIDDEN_KEYS)
    assert not (set(actions_env.keys()) & ACTIONS_FORBIDDEN_KEYS)
    return intake_env, actions_env


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Least-privilege .env slicer for movie-intake-api & movie-actions-api")
    parser.add_argument("--home", default=str(Path.home()), help="Absolute user home directory")
    parser.add_argument("--port-intake", type=int, required=True)
    parser.add_argument("--port-actions", type=int, required=True)
    parser.add_argument("--port-triage", type=int, default=40716)
    parser.add_argument("--intake-env-out", required=True, help="Absolute output path for movie-intake-api .env")
    parser.add_argument("--actions-env-out", required=True, help="Absolute output path for movie-actions-api .env")
    parser.add_argument("--triage-env", default="", help="Optional path to movie-triage-api .env to reuse INTERNAL_API_SECRET")
    parser.add_argument("--legacy-env", default="", help="Optional path to legacy ~/.config/movie-intel/environment")
    parser.add_argument("--shadow", action="store_true", help="Slice isolated shadow-mode .env files")
    parser.add_argument("--dry-run", action="store_true", help="Validate and report key presence without writing")
    args = parser.parse_args(argv)

    home_dir = Path(args.home)
    if not home_dir.is_absolute():
        raise EnvSecurityError("--home must be an absolute path")

    intake_out = Path(args.intake_env_out)
    actions_out = Path(args.actions_env_out)

    existing_intake = read_owner_private_env(intake_out)
    existing_actions = read_owner_private_env(actions_out)
    legacy_env = {} if args.shadow else (read_owner_private_env(Path(args.legacy_env)) if args.legacy_env else {})
    triage_env = {} if args.shadow else (read_owner_private_env(Path(args.triage_env)) if args.triage_env else {})

    if args.shadow:
        # Shadow mode NEVER inherits ambient or production INTERNAL_API_SECRET.
        internal_secret = (
            existing_intake.get("INTERNAL_API_SECRET", "").strip()
            or existing_actions.get("INTERNAL_API_SECRET", "").strip()
            or f"shadow-{secrets.token_urlsafe(24)}"
        )
    else:
        internal_secret = (
            os.environ.get("INTERNAL_API_SECRET", "").strip()
            or triage_env.get("INTERNAL_API_SECRET", "").strip()
            or existing_intake.get("INTERNAL_API_SECRET", "").strip()
            or existing_actions.get("INTERNAL_API_SECRET", "").strip()
            or secrets.token_urlsafe(32)
        )

    intake_env, actions_env = build_sliced_envs(
        home_dir=home_dir,
        port_intake=args.port_intake,
        port_actions=args.port_actions,
        port_triage=args.port_triage,
        internal_api_secret=internal_secret,
        legacy_env=legacy_env,
        existing_intake_env=existing_intake,
        existing_actions_env=existing_actions,
        override_env={} if args.shadow else os.environ,
        shadow_mode=args.shadow,
    )

    if not args.shadow:
        if not triage_env.get("INTERNAL_API_SECRET", "").strip():
            print(
                "Warning: movie-triage-api .env has no INTERNAL_API_SECRET; run 'bash deploy-ultra.sh' "
                "so movie-triage-api shares INTERNAL_API_SECRET with intake/actions.",
                file=sys.stderr,
            )
        missing_live = [
            k
            for k in (
                "MOVIE_INTEL_WEBHOOK_SECRET",
                "RADARR_URL",
                "RADARR_API_KEY",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHAT_ID",
                "MOVIE_INTEL_PUBLIC_BASE_URL",
            )
            if not (intake_env.get(k) or actions_env.get(k))
        ]
        if missing_live:
            print(
                f"Warning: production keys currently empty (fill before cutover): {', '.join(missing_live)}",
                file=sys.stderr,
            )

    if args.dry_run:
        print(f"DRY-RUN OK: intake_keys={len(intake_env)} actions_keys={len(actions_env)} shadow={args.shadow}")
        return 0

    atomic_write_env_file(intake_out, INTAKE_ALLOWED_KEYS, intake_env)
    atomic_write_env_file(actions_out, ACTIONS_ALLOWED_KEYS, actions_env)
    print(f"Wrote locked-down 0600 env files: {intake_out} and {actions_out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EnvSecurityError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
