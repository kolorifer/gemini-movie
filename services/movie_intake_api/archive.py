from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import MAX_WEBHOOK_BODY_BYTES
from .security import (
    PrivateFileError,
    atomic_write_private_file,
    ensure_private_dir,
    read_owner_private_regular_file,
)


class ArchivePersistenceError(RuntimeError):
    """Raised when required raw webhook archival fails."""


class ArchiveIntegrityError(RuntimeError):
    """Raised when archived payload verification or extraction fails."""


class ArchiveMissingDownloadUrlError(ArchiveIntegrityError):
    """Raised when a SHA-256-verified archived payload legitimately lacks a downloadUrl."""


def _reject_nonstandard_json_constant(token: str) -> Any:
    raise ValueError(f"Non-standard JSON constant rejected: {token}")


def parse_strict_json_object(raw_bytes: bytes) -> dict[str, Any]:
    """Decode UTF-8 and parse a strict RFC 8259 JSON object (rejecting NaN/Infinity)."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Payload is not valid UTF-8") from exc
    try:
        parsed = json.loads(text, parse_constant=_reject_nonstandard_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Webhook payload root must be a JSON object")
    return parsed


@dataclass(frozen=True)
class ArchiveReceipt:
    archive_relpath: str
    archive_sha256: str
    payload_bytes: int


def verify_archive_root(archive_dir: Path) -> bool:
    try:
        ensure_private_dir(archive_dir)
        return True
    except (PrivateFileError, OSError):
        return False


def archive_raw_webhook(
    archive_dir: Path,
    occurrence_id: str,
    raw_bytes: bytes,
    *,
    received_at: Optional[datetime] = None,
) -> ArchiveReceipt:
    """
    Persist exact raw webhook bytes atomically to an owner-private 0700/0600 archive
    BEFORE any business classification or filtering occurs.
    """
    ts = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not occurrence_id or "/" in occurrence_id or "\\" in occurrence_id or ".." in occurrence_id:
        raise ArchivePersistenceError("Invalid occurrence_id for archive filename")

    year_part = ts.strftime("%Y")
    month_part = ts.strftime("%m")
    day_part = ts.strftime("%d")
    ts_part = ts.strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"{ts_part}_{occurrence_id}.json"
    relpath = f"{year_part}/{month_part}/{day_part}/{filename}"

    try:
        ensure_private_dir(archive_dir)
        cur = archive_dir
        for part in (year_part, month_part, day_part):
            cur = cur / part
            ensure_private_dir(cur)
        target = cur / filename
        atomic_write_private_file(target, raw_bytes)
    except Exception as exc:
        raise ArchivePersistenceError(f"Failed to persist raw webhook archive: {exc.__class__.__name__}") from exc

    digest = hashlib.sha256(raw_bytes).hexdigest()
    return ArchiveReceipt(
        archive_relpath=relpath,
        archive_sha256=digest,
        payload_bytes=len(raw_bytes),
    )


def _resolve_safe_archive_file(archive_dir: Path, archive_relpath: str) -> Path:
    if not archive_relpath or archive_relpath.startswith("/") or "\\" in archive_relpath:
        raise ArchiveIntegrityError("Archive relative path is invalid")
    parts = Path(archive_relpath).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ArchiveIntegrityError("Archive relative path contains traversal components")

    ensure_private_dir(archive_dir)
    cur = archive_dir
    for part in parts[:-1]:
        cur = cur / part
        if os.path.islink(str(cur)):
            raise ArchiveIntegrityError("Symlink detected inside archive directory")
    target = cur / parts[-1]
    if os.path.islink(str(target)):
        raise ArchiveIntegrityError("Archive file target is a symlink")

    root_resolved = archive_dir.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ArchiveIntegrityError("Archive path escapes archive root") from exc
    return target


def read_verified_archive_json(
    archive_dir: Path,
    archive_relpath: str,
    expected_sha256: str,
) -> dict[str, Any]:
    """
    Read and SHA-256-verify an archived raw webhook payload strictly from an owner-only
    0600 regular file without following symlinks.
    """
    try:
        target = _resolve_safe_archive_file(archive_dir, archive_relpath)
        raw_bytes = read_owner_private_regular_file(target, max_bytes=MAX_WEBHOOK_BODY_BYTES)
    except (PrivateFileError, ArchiveIntegrityError, OSError) as exc:
        raise ArchiveIntegrityError(f"Unable to read private archive file: {exc}") from exc

    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if not expected_sha256 or not hmac.compare_digest(actual_sha256, expected_sha256.lower()):
        raise ArchiveIntegrityError("Archive payload SHA-256 integrity verification failed")

    try:
        return parse_strict_json_object(raw_bytes)
    except ValueError as exc:
        raise ArchiveIntegrityError(f"Archived payload is not a valid JSON object: {exc}") from exc


def extract_radarr_push_payload(
    raw_payload: dict[str, Any],
    *,
    indexer_norm: str,
    external_id_norm: str,
    exact_release_name: str,
    fallback_publish_date: str,
) -> dict[str, Any]:
    """
    Extract ONLY the ephemeral Radarr /api/v3/release/push fields in memory from a
    verified private archive payload. Never log or persist the returned downloadUrl.
    """
    data = raw_payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    release = data_dict.get("release")
    release_dict = release if isinstance(release, dict) else {}

    download_url: Optional[str] = None
    for container in (release_dict, data_dict, raw_payload):
        for key in ("download_url", "downloadUrl", "torrent_url", "torrentUrl"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                download_url = val.strip()
                break
        if download_url:
            break

    if not download_url:
        raise ArchiveMissingDownloadUrlError("Archived webhook does not contain a downloadUrl")
    if not (download_url.startswith("https://") or download_url.startswith("http://")):
        raise ArchiveIntegrityError("Archived downloadUrl must use http or https scheme")

    title = str(release_dict.get("name") or exact_release_name).strip() or exact_release_name
    protocol_raw = str(release_dict.get("protocol") or "torrent").strip().lower()
    protocol = protocol_raw if protocol_raw in ("torrent", "usenet") else "torrent"

    publish_date = (
        release_dict.get("timestamp")
        or release_dict.get("published_at")
        or data_dict.get("timestamp")
        or raw_payload.get("timestamp")
        or fallback_publish_date
    )
    if not isinstance(publish_date, str) or not publish_date.strip():
        publish_date = fallback_publish_date

    indexer_val = data_dict.get("indexer")
    if isinstance(indexer_val, dict):
        indexer_name = str(indexer_val.get("name") or indexer_val.get("identifier") or indexer_norm).strip()
    elif isinstance(indexer_val, str) and indexer_val.strip():
        indexer_name = indexer_val.strip()
    else:
        indexer_name = str(release_dict.get("indexer") or indexer_norm).strip()

    payload: dict[str, Any] = {
        "title": title,
        "downloadUrl": download_url,
        "protocol": protocol,
        "publishDate": publish_date.strip(),
    }
    if indexer_name:
        payload["indexer"] = indexer_name

    # Deterministic non-secret GUID for Radarr queue/history reconciliation (never copy passkey URL!)
    if indexer_norm and external_id_norm:
        payload["guid"] = f"{indexer_norm}:{external_id_norm}"

    indexer_id = release_dict.get("indexer_id") or data_dict.get("indexer_id")
    if isinstance(indexer_id, int) and not isinstance(indexer_id, bool) and indexer_id > 0:
        payload["indexerId"] = indexer_id

    size_val = release_dict.get("size")
    if isinstance(size_val, int) and not isinstance(size_val, bool) and size_val >= 0:
        payload["size"] = size_val

    return payload
