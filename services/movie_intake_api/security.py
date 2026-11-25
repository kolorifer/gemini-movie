from __future__ import annotations

import hmac
import logging
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Optional

_SECRET_URL_RE = re.compile(
    r"https?://\S+|magnet:\S+|www\.\S+"
    r"|/webhook/[^/\s?\"']+"
    r"|/action/[^/\s?\"']+"
    r"|\b(?:passkey|authkey|api[_-]?key|apikey|secret|token|password|authorization|cookie|bearer|x-internal-token)\b\s*[:=]?\s*\S+"
    r"|\bkey\s*=\s*\S+"
    r"|[A-Za-z0-9_-]{48,}",
    re.IGNORECASE,
)


class PrivateFileError(RuntimeError):
    """Raised when an owner-private file or directory violates security invariants."""


def constant_time_equals(provided: Optional[str], expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def redact_secrets(raw: Optional[str], *, max_chars: Optional[int] = 900) -> str:
    """Strip URLs, passkeys, cookies, webhook/action tokens, API keys, and control chars."""
    if not raw:
        return ""

    def _replace(match: re.Match[str]) -> str:
        text = match.group(0)
        low = text.lower()
        if low.startswith("/webhook/"):
            return "/webhook/[REDACTED]"
        if low.startswith("/action/"):
            return "/action/[REDACTED]"
        return "[REDACTED]"

    scrubbed = _SECRET_URL_RE.sub(_replace, raw)
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in scrubbed)
    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    if max_chars is not None and len(collapsed) > max_chars:
        return collapsed[: max_chars - 3].rstrip() + "..."
    return collapsed


class SecretRedactingLogFilter(logging.Filter):
    """Logging filter that scrubs secret-bearing paths, URLs, and tokens from all records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            record.msg = redact_secrets(rendered, max_chars=2000)
            record.args = ()
        except Exception:
            pass
        return True


def install_log_redaction() -> None:
    redactor = SecretRedactingLogFilter()
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        logger_obj = logging.getLogger(name)
        if not any(isinstance(f, SecretRedactingLogFilter) for f in logger_obj.filters):
            logger_obj.addFilter(redactor)


def ensure_private_dir(path: Path) -> None:
    """Create or verify an owner-private 0700 non-symlink directory."""
    if not path.is_absolute():
        raise PrivateFileError("Private directory path must be absolute")
    if os.path.islink(str(path)):
        raise PrivateFileError("Refusing symlinked private directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(str(path))
    if not stat.S_ISDIR(st.st_mode):
        raise PrivateFileError("Target path is not a directory")
    if st.st_uid != os.getuid():
        raise PrivateFileError("Private directory is not owned by current user")
    if (st.st_mode & 0o077) != 0 and not (st.st_mode & 0o1000):
        os.chmod(str(path), 0o700)
        st = os.lstat(str(path))
        if (st.st_mode & 0o077) != 0:
            raise PrivateFileError("Unable to enforce 0700 permissions on private directory")


def read_owner_private_regular_file(path: Path, *, max_bytes: int) -> bytes:
    """
    Open and read a regular owner-only (0600-compatible, no group/other bits) file
    using O_RDONLY | O_NOFOLLOW | O_NONBLOCK and fstat validation on the open fd.
    """
    if not path.is_absolute():
        raise PrivateFileError("Configured private file path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise PrivateFileError(f"Cannot safely open private file: {exc.__class__.__name__}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PrivateFileError("Private file must be a regular file")
        if st.st_uid != os.getuid():
            raise PrivateFileError("Private file must be owned by current user")
        if (st.st_mode & 0o077) != 0:
            raise PrivateFileError("Private file permissions are too permissive (must have no group/other bits)")
        if st.st_size > max_bytes:
            raise PrivateFileError(f"Private file exceeds maximum size of {max_bytes} bytes")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(16384, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PrivateFileError(f"Private file exceeded maximum size of {max_bytes} bytes while reading")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def atomic_write_private_file(target: Path, data: bytes) -> None:
    """Atomically persist data to a 0600 file inside an owner-private 0700 directory."""
    ensure_private_dir(target.parent)
    if os.path.islink(str(target)):
        raise PrivateFileError("Refusing to overwrite symlink target")
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: Optional[int] = None
    try:
        # Ensure unique temp name even under concurrent writers
        attempt = 0
        while True:
            try:
                candidate = (
                    tmp_path
                    if attempt == 0
                    else target.parent / f".{target.name}.{os.getpid()}.{attempt}.tmp"
                )
                fd = os.open(str(candidate), flags, 0o600)
                tmp_path = candidate
                break
            except FileExistsError:
                attempt += 1
                if attempt > 64:
                    raise
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("Short write to private file")
            written += n
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(str(tmp_path), str(target))
        os.chmod(str(target), 0o600)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
