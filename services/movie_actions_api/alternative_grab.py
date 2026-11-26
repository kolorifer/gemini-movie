from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import httpx

from .security import (
    PrivateFileError,
    ensure_private_dir,
    read_owner_private_regular_file,
    redact_secrets,
)

MAX_ALT_GRAB_REJECTION_REASONS = 8
MAX_ALT_GRAB_RELEASE_NAME_LEN = 512
MAX_TORRENT_BYTES = 4_000_000
MAX_BENCODE_DEPTH = 32
MAX_RECEIPT_BYTES = 32_768

_NUMERIC_ID_RE = re.compile(r"^\d{1,12}$")
_INFO_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
_EPISODIC_RELEASE_RE = re.compile(
    r"(?<![a-z0-9])(?:s\d{1,2}e\d{1,3}|s\d{1,2}(?![a-z0-9])|season[\s._-]?\d{1,2}|episode[\s._-]?\d{1,3}|complete[\s._-]?series)(?![a-z0-9])",
    re.IGNORECASE,
)

_TL_DOWNLOAD_HOSTS = frozenset({"www.torrentleech.org", "torrentleech.org"})
_TL_RSS_DOWNLOAD_PATH_RE = re.compile(r"^/rss/download/(\d{1,12})/[^/]+/[^/]+\.torrent$", re.IGNORECASE)
_TL_DIRECT_DOWNLOAD_PATH_RE = re.compile(r"^/download/(\d{1,12})/[^/]+\.torrent$", re.IGNORECASE)

# Exact 4 legacy allowed Radarr rejection forms (OPS-REPORT #3 §2).
# Forms 1 & 2 strictly accept only Remux, Bluray, WEBDL, WEBRip, or HDTV with 720p, 1080p, or 2160p.
_ALLOWED_QUALITY_TOKEN = r"(?:Remux|Bluray|WEBDL|WEBRip|HDTV)-(?:720p|1080p|2160p)"
_REJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cutoff_met",
        re.compile(rf"^Existing file meets cutoff:\s*{_ALLOWED_QUALITY_TOKEN}$", re.IGNORECASE),
    ),
    (
        "equal_or_higher_preference",
        re.compile(
            rf"^Existing file(?: on disk)? is of equal or higher preference:\s*{_ALLOWED_QUALITY_TOKEN}\s+v[1-9]\d*$",
            re.IGNORECASE,
        ),
    ),
    (
        "upgrades_disallowed",
        re.compile(
            r"^(?:Upgrades are not allowed (?:by|for) (?:the|this) quality profile|Quality profile does not allow upgrades)\.?$",
            re.IGNORECASE,
        ),
    ),
    (
        "custom_format_score",
        re.compile(
            r"^(?:Existing file(?: on disk)? has an? equal or higher Custom Format score(?:\s*[:(][^)\n]{1,120}\)?)?|Not a Custom Format upgrade for existing movie file\(s\)\.?\s*New:\s*.+do not improve on Existing:\s*.+)$",
            re.IGNORECASE,
        ),
    ),
)

SNAPSHOT_COMPARE_FIELDS: tuple[str, ...] = (
    "movie_id",
    "file_id",
    "monitored",
    "quality_profile_id",
    "relative_path",
    "edition",
    "release_group",
    "size",
    "quality",
)


def is_canonical_tl_download_url(download_url: str, external_id: str) -> bool:
    """
    Verify `download_url` is a canonical HTTPS TorrentLeech torrent download URL
    bound to the exact same numeric tracker release ID (`external_id`).
    Never logs or returns the URL.
    """
    ext = str(external_id or "").strip()
    if not _NUMERIC_ID_RE.fullmatch(ext):
        return False
    raw = str(download_url or "").strip()
    if not raw or any(ch in raw for ch in ("\r", "\n", "\x00", " ", "\t")):
        return False
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.port is not None and parsed.port != 443:
        return False
    host = (parsed.hostname or "").lower()
    if host not in _TL_DOWNLOAD_HOSTS:
        return False
    path = parsed.path or ""
    if ".." in path or "\\" in path:
        return False
    m = _TL_RSS_DOWNLOAD_PATH_RE.fullmatch(path) or _TL_DIRECT_DOWNLOAD_PATH_RE.fullmatch(path)
    if not m:
        return False
    return m.group(1) == ext


def match_legacy_alt_grab_rejection_reason(reason: str) -> Optional[str]:
    """Return canonical rejection class if `reason` fully matches one of the 4 allowed legacy forms."""
    text = str(reason or "").strip()
    if not text:
        return None
    for class_name, pattern in _REJECTION_PATTERNS:
        if pattern.fullmatch(text):
            return class_name
    return None


def validate_legacy_alt_grab_rejections(reasons: list[str]) -> tuple[bool, list[str], str]:
    """
    Every one of 1-8 rejection reasons must fully match an allowed legacy form.
    A mixed list with even one disallowed reason fails.
    """
    if not isinstance(reasons, list) or not (1 <= len(reasons) <= MAX_ALT_GRAB_REJECTION_REASONS):
        return False, [], "rejection_count_invalid"
    classes: list[str] = []
    for item in reasons:
        if not isinstance(item, str) or not item.strip():
            return False, [], "empty_rejection_reason"
        matched = match_legacy_alt_grab_rejection_reason(item)
        if matched is None:
            return False, [], "disallowed_rejection_reason"
        if matched not in classes:
            classes.append(matched)
    return True, classes, "ok"


def validate_legacy_alt_grab_source(
    *,
    indexer: str,
    external_id: str,
    exact_release_name: str,
    title: Optional[str],
    year: Optional[int],
    rejections: list[str],
) -> tuple[bool, list[str], str]:
    """Verify legacy TorrentLeech movie source & 1-8 rejection rules."""
    if str(indexer or "").strip().lower() != "torrentleech":
        return False, [], "indexer_not_torrentleech"
    if not _NUMERIC_ID_RE.fullmatch(str(external_id or "").strip()):
        return False, [], "invalid_tracker_external_id"
    rel = str(exact_release_name or "").strip()
    if not rel or len(rel) > MAX_ALT_GRAB_RELEASE_NAME_LEN:
        return False, [], "invalid_release_name_length"
    if _EPISODIC_RELEASE_RE.search(rel):
        return False, [], "episodic_release_disallowed"
    if not title or not str(title).strip():
        return False, [], "missing_title"
    if not isinstance(year, int) or isinstance(year, bool) or not (1888 <= year <= 2100):
        return False, [], "invalid_year"
    return validate_legacy_alt_grab_rejections(rejections)


class TorrentMetainfoError(ValueError):
    """Raised when fetched .torrent bytes fail strict v1 metainfo validation."""


def _bdecode(
    data: bytes,
    pos: int,
    depth: int,
    *,
    capture_top_info: bool = False,
) -> tuple[Any, int, Optional[tuple[int, int]]]:
    if depth > MAX_BENCODE_DEPTH:
        raise TorrentMetainfoError("Bencode nesting depth exceeded")
    if pos >= len(data):
        raise TorrentMetainfoError("Unexpected EOF in bencode")

    tag = data[pos : pos + 1]
    if tag == b"i":
        end = data.find(b"e", pos + 1)
        if end == -1:
            raise TorrentMetainfoError("Unterminated bencode integer")
        num_bytes = data[pos + 1 : end]
        if not num_bytes:
            raise TorrentMetainfoError("Empty bencode integer")
        if num_bytes == b"-0":
            raise TorrentMetainfoError("Invalid -0 bencode integer")
        if num_bytes.startswith(b"-"):
            digits = num_bytes[1:]
            if not digits or not digits.isdigit() or digits.startswith(b"0"):
                raise TorrentMetainfoError("Malformed negative bencode integer")
        else:
            if not num_bytes.isdigit() or (len(num_bytes) > 1 and num_bytes.startswith(b"0")):
                raise TorrentMetainfoError("Malformed bencode integer")
        return int(num_bytes.decode("ascii")), end + 1, None

    if tag == b"l":
        items: list[Any] = []
        idx = pos + 1
        while idx < len(data) and data[idx : idx + 1] != b"e":
            val, idx, _ = _bdecode(data, idx, depth + 1)
            items.append(val)
        if idx >= len(data):
            raise TorrentMetainfoError("Unterminated bencode list")
        return items, idx + 1, None

    if tag == b"d":
        out: dict[bytes, Any] = {}
        idx = pos + 1
        prev_key: Optional[bytes] = None
        info_span: Optional[tuple[int, int]] = None
        while idx < len(data) and data[idx : idx + 1] != b"e":
            key, idx, _ = _bdecode(data, idx, depth + 1)
            if not isinstance(key, bytes):
                raise TorrentMetainfoError("Bencode dict key must be bytes")
            if prev_key is not None and key <= prev_key:
                raise TorrentMetainfoError("Bencode dict keys must be strictly sorted and unique")
            prev_key = key
            val_start = idx
            val, idx, _ = _bdecode(data, idx, depth + 1)
            if capture_top_info and key == b"info":
                info_span = (val_start, idx)
            out[key] = val
        if idx >= len(data):
            raise TorrentMetainfoError("Unterminated bencode dict")
        return out, idx + 1, info_span

    if b"0" <= tag <= b"9":
        colon = data.find(b":", pos)
        if colon == -1:
            raise TorrentMetainfoError("Missing colon in bencode byte string")
        len_bytes = data[pos:colon]
        if not len_bytes.isdigit() or (len(len_bytes) > 1 and len_bytes.startswith(b"0")):
            raise TorrentMetainfoError("Malformed bencode string length")
        str_len = int(len_bytes.decode("ascii"))
        start = colon + 1
        end = start + str_len
        if end > len(data):
            raise TorrentMetainfoError("Truncated bencode byte string")
        return data[start:end], end, None

    raise TorrentMetainfoError(f"Invalid bencode token at byte {pos}")


def parse_v1_torrent_info_hash(raw_bytes: bytes) -> str:
    """
    Strictly validate v1 BitTorrent metainfo (<= 4,000,000 bytes) and compute the
    40-character lowercase SHA-1 v1 info hash directly from the raw `info` dict bytes.
    """
    if not raw_bytes:
        raise TorrentMetainfoError("Empty torrent payload")
    if len(raw_bytes) > MAX_TORRENT_BYTES:
        raise TorrentMetainfoError(f"Torrent payload exceeds {MAX_TORRENT_BYTES} bytes")

    root, end_pos, info_span = _bdecode(raw_bytes, 0, 0, capture_top_info=True)
    if end_pos != len(raw_bytes):
        raise TorrentMetainfoError("Trailing garbage after torrent bencode root")
    if not isinstance(root, dict) or info_span is None:
        raise TorrentMetainfoError("Torrent metainfo missing top-level info dictionary")

    info = root.get(b"info")
    if not isinstance(info, dict):
        raise TorrentMetainfoError("Torrent info field is not a dictionary")

    name = info.get(b"name")
    if not isinstance(name, bytes) or not name:
        raise TorrentMetainfoError("Torrent info.name missing or empty")

    piece_length = info.get(b"piece length")
    if not isinstance(piece_length, int) or isinstance(piece_length, bool) or piece_length <= 0:
        raise TorrentMetainfoError("Torrent info.piece length invalid")

    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes) or not pieces or (len(pieces) % 20) != 0:
        raise TorrentMetainfoError("Torrent info.pieces missing or not v1 20-byte SHA-1 hashes")

    has_length = b"length" in info
    has_files = b"files" in info
    if has_length == has_files:
        raise TorrentMetainfoError("Torrent info must specify either length (single-file) or files (multi-file)")
    if has_length:
        length = info[b"length"]
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise TorrentMetainfoError("Torrent info.length invalid")
    else:
        files = info[b"files"]
        if not isinstance(files, list) or not files:
            raise TorrentMetainfoError("Torrent info.files must be a non-empty list")
        for fentry in files:
            if not isinstance(fentry, dict):
                raise TorrentMetainfoError("Torrent info.files entry must be a dict")
            flen = fentry.get(b"length")
            fpath = fentry.get(b"path")
            if not isinstance(flen, int) or isinstance(flen, bool) or flen < 0:
                raise TorrentMetainfoError("Torrent info.files entry length invalid")
            if not isinstance(fpath, list) or not fpath or not all(isinstance(p, bytes) and p for p in fpath):
                raise TorrentMetainfoError("Torrent info.files entry path invalid")

    info_start, info_end = info_span
    return hashlib.sha1(raw_bytes[info_start:info_end]).hexdigest().lower()


def build_movie_file_snapshot(movie: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """
    Build the exact legacy Radarr movie+file snapshot required for Alternative Grab minting
    and pre-/post-fetch revalidation. Returns None if movie is unmonitored or lacks a valid file.
    """
    movie_id = movie.get("id")
    if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id <= 0:
        return None
    if movie.get("monitored") is not True:
        return None
    if movie.get("hasFile") is not True:
        return None

    mf = movie.get("movieFile")
    if not isinstance(mf, Mapping):
        return None
    file_id = mf.get("id")
    if not isinstance(file_id, int) or isinstance(file_id, bool) or file_id <= 0:
        return None

    movie_file_id_ref = movie.get("movieFileId")
    if (
        movie_file_id_ref is not None
        and (
            not isinstance(movie_file_id_ref, int)
            or isinstance(movie_file_id_ref, bool)
            or movie_file_id_ref != file_id
        )
    ):
        return None

    qp_id = movie.get("qualityProfileId")
    if not isinstance(qp_id, int) or isinstance(qp_id, bool) or qp_id <= 0:
        return None

    rel_path = str(mf.get("relativePath") or "").strip()
    if not rel_path:
        return None

    size = mf.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None

    q_outer = mf.get("quality")
    q_inner = q_outer.get("quality") if isinstance(q_outer, Mapping) else None
    quality_name = str(q_inner.get("name") or "").strip() if isinstance(q_inner, Mapping) else ""
    if not quality_name:
        return None

    edition = str(mf.get("edition") or "").strip()
    release_group = str(mf.get("releaseGroup") or "").strip()

    return {
        "movie_id": movie_id,
        "file_id": file_id,
        "monitored": True,
        "quality_profile_id": qp_id,
        "relative_path": rel_path,
        "edition": edition,
        "release_group": release_group,
        "size": size,
        "quality": quality_name,
    }


def compare_movie_file_snapshots(saved: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[bool, str]:
    """Compare all saved legacy movie/file snapshot fields against live Radarr state."""
    mismatches: list[str] = []
    for field_name in SNAPSHOT_COMPARE_FIELDS:
        if saved.get(field_name) != current.get(field_name):
            mismatches.append(field_name)
    if mismatches:
        return False, f"snapshot_drift:{','.join(mismatches)}"
    return True, "ok"


def _normalize_url_base(raw: str) -> str:
    text = (raw or "").strip()
    if not text or text == "/":
        return ""
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/")


def validate_radarr_qbit_route(
    download_clients: list[Any],
    *,
    configured_url: str,
    configured_category: str,
) -> tuple[bool, str]:
    """
    Verify Radarr has EXACTLY ONE enabled qBittorrent download client matching the
    configured HTTPS TLS endpoint (host, port, urlBase) and movie category.
    """
    parsed_cfg = urlparse(configured_url.strip())
    if parsed_cfg.scheme != "https" or not parsed_cfg.hostname:
        return False, "qbit_url_must_be_https"
    cfg_host = parsed_cfg.hostname.lower()
    cfg_port = parsed_cfg.port or 443
    cfg_base = _normalize_url_base(parsed_cfg.path)
    cfg_cat = configured_category.strip()
    if not cfg_cat:
        return False, "qbit_category_missing"

    enabled_qbits: list[dict[str, Any]] = []
    for client in download_clients:
        if not isinstance(client, Mapping):
            continue
        if client.get("enable") is not True:
            continue
        impl = str(client.get("implementation") or client.get("implementationName") or "").strip().lower()
        if impl == "qbittorrent":
            enabled_qbits.append(dict(client))

    if len(enabled_qbits) == 0:
        return False, "radarr_no_enabled_qbittorrent_client"
    if len(enabled_qbits) > 1:
        return False, "radarr_multiple_enabled_qbittorrent_clients"

    qbit = enabled_qbits[0]
    raw_fields = qbit.get("fields")
    if not isinstance(raw_fields, list):
        return False, "radarr_qbit_missing_fields"
    field_map: dict[str, Any] = {}
    for entry in raw_fields:
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            field_map[entry["name"].strip().lower()] = entry.get("value")

    if field_map.get("usessl") is not True:
        return False, "radarr_qbit_tls_disabled"
    radarr_host = str(field_map.get("host") or "").strip().lower()
    raw_port = field_map.get("port")
    if isinstance(raw_port, bool) or raw_port is None:
        return False, "radarr_qbit_port_missing"
    try:
        radarr_port = int(raw_port)
    except (TypeError, ValueError):
        return False, "radarr_qbit_port_invalid"
    radarr_base = _normalize_url_base(str(field_map.get("urlbase") or ""))
    radarr_cat = str(field_map.get("moviecategory") or "").strip()

    if radarr_host != cfg_host or radarr_port != cfg_port or radarr_base != cfg_base:
        return False, "radarr_qbit_endpoint_mismatch"
    if radarr_cat != cfg_cat:
        return False, "radarr_qbit_category_mismatch"
    return True, "ok"


def _receipt_path(state_dir: Path, external_id: str) -> Path:
    ext = str(external_id or "").strip()
    if not _NUMERIC_ID_RE.fullmatch(ext):
        raise PrivateFileError("Invalid numeric external_id for alternative grab receipt")
    return state_dir / f"alt-grab-tl-{ext}.json"


def read_alt_grab_receipt(state_dir: Path, external_id: str) -> Optional[dict[str, Any]]:
    """Safely read an owner-private non-symlink receipt keyed to tracker `external_id`."""
    ensure_private_dir(state_dir)
    path = _receipt_path(state_dir, external_id)
    if not os.path.lexists(str(path)):
        return None
    raw = read_owner_private_regular_file(path, max_bytes=MAX_RECEIPT_BYTES)
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else None


def write_alt_grab_receipt(state_dir: Path, external_id: str, receipt: Mapping[str, Any]) -> Path:
    """Atomically write an owner-only 0600 JSON receipt keyed to tracker `external_id` (zero secrets)."""
    ensure_private_dir(state_dir)
    target = _receipt_path(state_dir, external_id)
    if os.path.islink(str(target)):
        raise PrivateFileError(f"Refusing symlinked receipt path: {target.name}")

    payload = (json.dumps(dict(receipt), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd: Optional[int] = None
    tmp_path: Optional[Path] = None
    try:
        for attempt in range(64):
            cand = state_dir / f".{target.name}.{os.getpid()}.{attempt}.tmp"
            try:
                fd = os.open(str(cand), flags, 0o600)
                tmp_path = cand
                break
            except FileExistsError:
                continue
        if fd is None or tmp_path is None:
            raise PrivateFileError("Unable to allocate temporary receipt file")
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("Short write to receipt temp file")
            written += n
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(str(tmp_path), str(target))
        return target
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


@dataclass(frozen=True)
class TorrentFetchResult:
    ok: bool
    torrent_bytes: Optional[bytes] = None
    info_hash: Optional[str] = None
    error_redacted: Optional[str] = None


class TorrentFetcher:
    """
    Fetches ONLY the exact bound canonical TorrentLeech .torrent URL with redirects disabled,
    zero shared cookies, hard 4,000,000-byte cap, and strict v1 metainfo/info-hash validation.
    """

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client

    async def fetch_bound_torrent(self, *, download_url: str, external_id: str) -> TorrentFetchResult:
        if not is_canonical_tl_download_url(download_url, external_id):
            return TorrentFetchResult(ok=False, error_redacted="download_url_not_canonical_tl_bound")

        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            async with client.stream(
                "GET",
                download_url,
                headers={"User-Agent": "movie-actions-api/1.0"},
            ) as resp:
                if 300 <= resp.status_code < 400:
                    return TorrentFetchResult(
                        ok=False,
                        error_redacted=f"torrent_fetch_redirect_refused:{resp.status_code}",
                    )
                if resp.status_code != 200:
                    return TorrentFetchResult(
                        ok=False,
                        error_redacted=f"torrent_fetch_http_{resp.status_code}",
                    )
                cl = resp.headers.get("content-length")
                if cl is not None:
                    try:
                        if int(cl.strip()) > MAX_TORRENT_BYTES:
                            return TorrentFetchResult(ok=False, error_redacted="torrent_exceeds_4mb_cap")
                    except ValueError:
                        return TorrentFetchResult(ok=False, error_redacted="torrent_invalid_content_length")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_TORRENT_BYTES:
                        return TorrentFetchResult(ok=False, error_redacted="torrent_exceeds_4mb_cap")
                raw_bytes = bytes(buf)
        except httpx.HTTPError as exc:
            return TorrentFetchResult(
                ok=False,
                error_redacted=f"torrent_fetch_error:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()

        try:
            info_hash = parse_v1_torrent_info_hash(raw_bytes)
        except TorrentMetainfoError as exc:
            return TorrentFetchResult(
                ok=False,
                error_redacted=f"torrent_invalid_v1_metainfo:{redact_secrets(str(exc), max_chars=120)}",
            )
        return TorrentFetchResult(ok=True, torrent_bytes=raw_bytes, info_hash=info_hash)


class ShadowStubTorrentFetcher(TorrentFetcher):
    is_shadow_stub: bool = True

    def __init__(self, *, torrent_bytes: Optional[bytes] = None) -> None:
        super().__init__(timeout_seconds=5.0)
        self.default_torrent_bytes = torrent_bytes or _build_synthetic_v1_torrent(b"shadow-release.mkv")
        self.fetch_calls: list[dict[str, str]] = []

    async def fetch_bound_torrent(self, *, download_url: str, external_id: str) -> TorrentFetchResult:
        if not is_canonical_tl_download_url(download_url, external_id):
            return TorrentFetchResult(ok=False, error_redacted="download_url_not_canonical_tl_bound")
        self.fetch_calls.append({"external_id": external_id})
        info_hash = parse_v1_torrent_info_hash(self.default_torrent_bytes)
        return TorrentFetchResult(ok=True, torrent_bytes=self.default_torrent_bytes, info_hash=info_hash)


def _build_synthetic_v1_torrent(name_bytes: bytes) -> bytes:
    """Build a minimal valid v1 bencode torrent for shadow/test use."""
    pieces = hashlib.sha1(name_bytes).digest()
    info = (
        b"d"
        + b"6:lengthi1048576e"
        + f"4:name{len(name_bytes)}:".encode("ascii")
        + name_bytes
        + b"12:piece lengthi262144e"
        + f"6:pieces{len(pieces)}:".encode("ascii")
        + pieces
        + b"e"
    )
    return b"d4:info" + info + b"e"


@dataclass(frozen=True)
class QbitReadResult:
    ok: bool
    data: Any = None
    status_code: Optional[int] = None
    error_redacted: Optional[str] = None


@dataclass(frozen=True)
class QbitAddResult:
    outcome: str  # ok | refused | failed_not_transmitted | ambiguous
    status_code: Optional[int] = None
    error_redacted: Optional[str] = None


class QBittorrentClient:
    """
    Isolated HTTPS qBittorrent Web API v2 client for Alternative Grab.
    Always opens a dedicated clean session (never reuses TorrentLeech cookies),
    never logs credentials, and submits validated .torrent bytes ONLY (no URLs, no pause/start overrides).
    """

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 20.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client

    def _new_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._external_client is not None:
            return self._external_client, False
        return httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds), True

    async def login(self, client: httpx.AsyncClient) -> QbitReadResult:
        if not self.base_url.startswith("https://"):
            return QbitReadResult(ok=False, error_redacted="qbit_url_must_be_https")
        try:
            resp = await client.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": f"{self.base_url}/"},
            )
            if resp.status_code != 200 or resp.text.strip() != "Ok.":
                return QbitReadResult(
                    ok=False,
                    status_code=resp.status_code,
                    error_redacted=f"qbit_login_failed_http_{resp.status_code}",
                )
            return QbitReadResult(ok=True, status_code=200)
        except httpx.HTTPError as exc:
            return QbitReadResult(
                ok=False,
                error_redacted=f"qbit_login_error:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )

    async def get_torrent_info(self, client: httpx.AsyncClient, info_hash: str) -> QbitReadResult:
        h = str(info_hash or "").strip().lower()
        if not _INFO_HASH_RE.fullmatch(h):
            return QbitReadResult(ok=False, error_redacted="invalid_info_hash")
        try:
            resp = await client.get(
                f"{self.base_url}/api/v2/torrents/info",
                params={"hashes": h},
            )
            if resp.status_code != 200:
                return QbitReadResult(
                    ok=False,
                    status_code=resp.status_code,
                    error_redacted=f"qbit_info_http_{resp.status_code}",
                )
            data = resp.json()
            if not isinstance(data, list):
                return QbitReadResult(ok=False, error_redacted="qbit_info_non_list")
            return QbitReadResult(ok=True, data=data, status_code=200)
        except Exception as exc:
            return QbitReadResult(
                ok=False,
                error_redacted=f"qbit_info_error:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )

    async def get_categories(self, client: httpx.AsyncClient) -> QbitReadResult:
        try:
            resp = await client.get(f"{self.base_url}/api/v2/torrents/categories")
            if resp.status_code != 200:
                return QbitReadResult(
                    ok=False,
                    status_code=resp.status_code,
                    error_redacted=f"qbit_categories_http_{resp.status_code}",
                )
            data = resp.json()
            if not isinstance(data, dict):
                return QbitReadResult(ok=False, error_redacted="qbit_categories_non_dict")
            return QbitReadResult(ok=True, data=data, status_code=200)
        except Exception as exc:
            return QbitReadResult(
                ok=False,
                error_redacted=f"qbit_categories_error:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )

    async def get_preferences(self, client: httpx.AsyncClient) -> QbitReadResult:
        try:
            resp = await client.get(f"{self.base_url}/api/v2/app/preferences")
            if resp.status_code != 200:
                return QbitReadResult(
                    ok=False,
                    status_code=resp.status_code,
                    error_redacted=f"qbit_prefs_http_{resp.status_code}",
                )
            data = resp.json()
            if not isinstance(data, dict):
                return QbitReadResult(ok=False, error_redacted="qbit_prefs_non_dict")
            return QbitReadResult(ok=True, data=data, status_code=200)
        except Exception as exc:
            return QbitReadResult(
                ok=False,
                error_redacted=f"qbit_prefs_error:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )

    async def add_torrent_bytes(
        self,
        client: httpx.AsyncClient,
        *,
        torrent_bytes: bytes,
        category: str,
        filename: str,
    ) -> QbitAddResult:
        """
        Submit ONE multipart POST /api/v2/torrents/add with validated .torrent bytes and
        fixed Radarr category ONLY (no `urls` field, no paused/stopped/start override).
        """
        try:
            resp = await client.post(
                f"{self.base_url}/api/v2/torrents/add",
                files={"torrents": (filename, torrent_bytes, "application/x-bittorrent")},
                data={"category": category},
            )
        except httpx.ConnectError:
            return QbitAddResult(outcome="failed_not_transmitted", error_redacted="qbit_connect_error")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            return QbitAddResult(
                outcome="ambiguous",
                error_redacted=f"qbit_add_ambiguous:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )

        body = (resp.text or "").strip()
        if resp.status_code == 200 and body == "Ok.":
            return QbitAddResult(outcome="ok", status_code=200)
        if resp.status_code in (400, 403, 415) or body.lower().startswith("fails"):
            return QbitAddResult(
                outcome="refused",
                status_code=resp.status_code,
                error_redacted=f"qbit_add_refused_http_{resp.status_code}",
            )
        return QbitAddResult(
            outcome="ambiguous",
            status_code=resp.status_code,
            error_redacted=f"qbit_add_ambiguous_http_{resp.status_code}",
        )

    async def open_authenticated_session(self) -> tuple[Optional[httpx.AsyncClient], bool, QbitReadResult]:
        """Open a clean client (or use injected test client) and authenticate via /api/v2/auth/login."""
        client, owns = self._new_client()
        login_res = await self.login(client)
        if not login_res.ok:
            if owns:
                await client.aclose()
            return None, False, login_res
        return client, owns, login_res


class ShadowStubQbitClient(QBittorrentClient):
    is_shadow_stub: bool = True

    def __init__(self, *, category: str = "movies") -> None:
        super().__init__(base_url="https://shadow-qbit.invalid", username="shadow", password="shadow-unused")
        self.torrents: dict[str, dict[str, Any]] = {}
        self.categories: dict[str, dict[str, Any]] = {category: {"name": category, "savePath": "/downloads/movies"}}
        self.preferences: dict[str, Any] = {
            "autorun_enabled": False,
            "autorun_on_torrent_added_enabled": False,
        }
        self.add_calls: list[dict[str, Any]] = []
        self.add_queue: list[QbitAddResult] = []

    async def open_authenticated_session(self) -> tuple[Optional[httpx.AsyncClient], bool, QbitReadResult]:
        return None, False, QbitReadResult(ok=True, status_code=200)

    async def get_torrent_info(self, client: Optional[httpx.AsyncClient], info_hash: str) -> QbitReadResult:
        h = str(info_hash or "").strip().lower()
        found = self.torrents.get(h)
        return QbitReadResult(ok=True, data=[dict(found)] if found is not None else [], status_code=200)

    async def get_categories(self, client: Optional[httpx.AsyncClient]) -> QbitReadResult:
        return QbitReadResult(ok=True, data=dict(self.categories), status_code=200)

    async def get_preferences(self, client: Optional[httpx.AsyncClient]) -> QbitReadResult:
        return QbitReadResult(ok=True, data=dict(self.preferences), status_code=200)

    async def add_torrent_bytes(
        self,
        client: Optional[httpx.AsyncClient],
        *,
        torrent_bytes: bytes,
        category: str,
        filename: str,
    ) -> QbitAddResult:
        info_hash = parse_v1_torrent_info_hash(torrent_bytes)
        self.add_calls.append({"info_hash": info_hash, "category": category, "filename": filename, "size": len(torrent_bytes)})
        if self.add_queue:
            return self.add_queue.pop(0)
        self.torrents[info_hash] = {"hash": info_hash, "category": category, "state": "downloading"}
        return QbitAddResult(outcome="ok", status_code=200)
