from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from .security import redact_secrets

MAX_ALT_GRAB_REJECTION_REASONS = 8
MAX_ALT_GRAB_RELEASE_NAME_LEN = 512

_IMDB_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)
_NUMERIC_ID_RE = re.compile(r"^\d{1,12}$")
_YEAR_TOKEN_RE = re.compile(r"^(18[89]\d|19\d\d|20\d\d)$")
_RELEASE_TECH_STOP_TOKENS = frozenset(
    {
        "2160p", "1080p", "1080i", "720p", "576p", "480p", "4k", "uhd",
        "bluray", "blu", "bdrip", "brrip", "remux", "web", "webdl", "webrip",
        "hdtv", "dvdrip", "x264", "x265", "h264", "h265", "hevc", "avc", "av1",
    }
)
_EPISODIC_RELEASE_RE = re.compile(
    r"(?<![a-z0-9])(?:s\d{1,2}e\d{1,3}|s\d{1,2}(?![a-z0-9])|season[\s._-]?\d{1,2}|episode[\s._-]?\d{1,3}|complete[\s._-]?series)(?![a-z0-9])",
    re.IGNORECASE,
)


def _extract_title_year_from_release_name(exact_name: str) -> tuple[Optional[str], Optional[int]]:
    cleaned = re.sub(r"\.(?:mkv|mp4|m2ts|iso|torrent)$", "", (exact_name or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"-[A-Za-z0-9]{2,24}$", "", cleaned)
    tokens = [t for t in re.split(r"[\s._\[\]\(\)\{\}/\\,:;]+", cleaned) if t]
    if len(tokens) < 2:
        return None, None
    tech_stop = len(tokens)
    for idx in range(1, len(tokens)):
        if tokens[idx].lower() in _RELEASE_TECH_STOP_TOKENS:
            tech_stop = idx
            break
    pre_tech = [tok for tok in tokens[:tech_stop] if not _IMDB_RE.fullmatch(tok.lower())]
    for idx in range(len(pre_tech) - 1, 0, -1):
        if _YEAR_TOKEN_RE.fullmatch(pre_tech[idx]):
            year = int(pre_tech[idx])
            if 1888 <= year <= 2100:
                title = " ".join(pre_tech[:idx]).strip()
                if title:
                    return title, year
    return None, None

_TL_DOWNLOAD_HOSTS = frozenset({"www.torrentleech.org", "torrentleech.org"})
_TL_RSS_DOWNLOAD_PATH_RE = re.compile(r"^/rss/download/(\d{1,12})/[^/]+/[^/]+\.torrent$", re.IGNORECASE)
_TL_DIRECT_DOWNLOAD_PATH_RE = re.compile(r"^/download/(\d{1,12})/[^/]+\.torrent$", re.IGNORECASE)

_NON_MOVIE_EVENT_TYPES = frozenset(
    {
        "announce",
        "irc",
        "feed",
        "housekeeping",
        "test",
        "ping",
        "healthcheck",
        "heartbeat",
        "status",
    }
)
_MOVIE_RELEASE_TYPES = frozenset({"movie", "movies", "film"})
_CONFLICTING_RELEASE_TYPES = frozenset(
    {
        "tv",
        "series",
        "episode",
        "season",
        "show",
        "anime",
        "music",
        "audio",
        "book",
        "ebook",
        "audiobook",
        "game",
        "games",
        "software",
        "app",
        "apps",
        "xxx",
    }
)
_NON_MOVIE_CATEGORY_TOKENS = frozenset(
    {
        "tv",
        "series",
        "episode",
        "episodes",
        "season",
        "boxset",
        "anime",
        "music",
        "audio",
        "flac",
        "mp3",
        "book",
        "books",
        "ebook",
        "ebooks",
        "audiobook",
        "game",
        "games",
        "console",
        "pc",
        "software",
        "app",
        "apps",
        "xxx",
        "porn",
        "sport",
        "sports",
    }
)
_MOVIE_CATEGORY_TOKENS = frozenset({"movie", "movies", "film", "films", "cinema"})

# Exact 4 legacy allowed Radarr rejection forms (OPS-REPORT #3 §2).
# Forms 1 & 2 strictly accept only Remux, Bluray, WEBDL, WEBRip, or HDTV with 720p, 1080p, or 2160p.
_LEGACY_ALLOWED_QUALITY_TOKEN = r"(?:Remux|Bluray|WEBDL|WEBRip|HDTV)-(?:720p|1080p|2160p)"
_LEGACY_ALT_GRAB_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cutoff_met",
        re.compile(rf"^Existing file meets cutoff:\s*{_LEGACY_ALLOWED_QUALITY_TOKEN}$", re.IGNORECASE),
    ),
    (
        "equal_or_higher_preference",
        re.compile(
            rf"^Existing file(?: on disk)? is of equal or higher preference:\s*{_LEGACY_ALLOWED_QUALITY_TOKEN}\s+v[1-9]\d*$",
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

# Broader routine quality-rejection patterns used to suppress non-eligible quality rejections early.
_QUALITY_REJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cutoff_met",
        re.compile(r"\b(?:cutoff\s+(?:is\s+)?(?:already\s+)?met|meets?\s+(?:quality\s+)?cutoff|quality\s+cutoff)\b", re.IGNORECASE),
    ),
    (
        "not_upgrade",
        re.compile(
            r"\b(?:not\s+an?\s+(?:quality\s+)?upgrade|does\s+not\s+improve|equal\s+or\s+higher\s+(?:quality|preference)|lower\s+quality|existing\s+file\s+is\s+better|upgrades\s+are\s+not\s+allowed|does\s+not\s+allow\s+upgrades)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "quality_unwanted",
        re.compile(
            r"\b(?:quality\s+(?:is\s+)?not\s+wanted|not\s+wanted\s+in\s+profile|unwanted\s+quality)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "custom_format_score",
        re.compile(r"\b(?:custom\s+format\s+score|minimum\s+custom\s+format|not\s+a\s+custom\s+format\s+upgrade)\b", re.IGNORECASE),
    ),
    (
        "size_limit",
        re.compile(r"\b(?:exceeds\s+maximum\s+size|below\s+minimum\s+size|size\s+is\s+(?:above|below))\b", re.IGNORECASE),
    ),
)


def is_canonical_tl_download_url(download_url: str, external_id: str) -> bool:
    """
    Verify `download_url` is a canonical HTTPS TorrentLeech torrent download URL
    bound to the exact same numeric tracker release ID (`external_id`).
    Never stores, returns, or logs the URL.
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
    text = str(reason or "").strip()
    if not text:
        return None
    for class_name, pattern in _LEGACY_ALT_GRAB_PATTERNS:
        if pattern.fullmatch(text):
            return class_name
    return None


def validate_legacy_alt_grab_rejections(reasons: list[str]) -> tuple[bool, list[str]]:
    if not isinstance(reasons, list) or not (1 <= len(reasons) <= MAX_ALT_GRAB_REJECTION_REASONS):
        return False, []
    classes: list[str] = []
    for item in reasons:
        if not isinstance(item, str) or not item.strip():
            return False, []
        matched = match_legacy_alt_grab_rejection_reason(item)
        if matched is None:
            return False, []
        if matched not in classes:
            classes.append(matched)
    return True, classes


def _clean_str(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _clean_int(value: Any, *, min_val: Optional[int] = None, max_val: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except (ValueError, TypeError):
        return None
    if min_val is not None and parsed < min_val:
        return None
    if max_val is not None and parsed > max_val:
        return None
    return parsed


def _clean_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "1", "yes"}:
            return True
        if low in {"false", "0", "no"}:
            return False
    return None


def _clean_imdb(value: Any) -> Optional[str]:
    text = _clean_str(value)
    if not text:
        return None
    low = text.lower()
    if _IMDB_RE.fullmatch(low):
        return low
    return None


def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,;/]+", value) if p.strip()]
        return parts[:8]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out[:8]
    return []


def _parse_size_gb(release_dict: dict[str, Any]) -> Optional[float]:
    """Convert raw release size into decimal GB per handover invariant."""
    raw_gb = release_dict.get("size_gb")
    if isinstance(raw_gb, (int, float)) and not isinstance(raw_gb, bool) and raw_gb >= 0:
        return round(float(raw_gb), 3)

    raw_bytes = release_dict.get("size")
    if isinstance(raw_bytes, str) and raw_bytes.strip().isdigit():
        raw_bytes = int(raw_bytes.strip())
    if isinstance(raw_bytes, (int, float)) and not isinstance(raw_bytes, bool) and raw_bytes > 0:
        # Autobrr emits size in bytes (e.g. 64_250_000_000 -> 64.25 decimal GB)
        return round(float(raw_bytes) / 1_000_000_000.0, 3)
    return None


def _tokenize_category(category: Optional[str]) -> set[str]:
    if not category:
        return set()
    return {tok for tok in re.split(r"[^a-z0-9]+", category.lower()) if tok}


def _extract_indexer_name(raw_payload: dict[str, Any], data_dict: dict[str, Any], release_dict: dict[str, Any]) -> str:
    for candidate in (data_dict.get("indexer"), release_dict.get("indexer"), raw_payload.get("indexer")):
        if isinstance(candidate, dict):
            name = _clean_str(candidate.get("identifier") or candidate.get("name"))
            if name:
                return name
        else:
            name = _clean_str(candidate)
            if name:
                return name
    return ""


DEFAULT_ALLOWED_FILTER_NAMES: tuple[str, ...] = ("Movies Remux",)
_QUALIFYING_ACTION_EVENTS = frozenset({"action.rejected", "push_rejected", "push.rejected"})
_REJECTED_EVENTS = _QUALIFYING_ACTION_EVENTS
_REJECTED_RESULT_STATUSES = frozenset({"PUSH_REJECTED", "REJECTED"})

_ELIGIBLE_REMUX_RESOLUTIONS = frozenset({"1080p", "2160p"})
_DISALLOWED_NAME_RESOLUTIONS = frozenset({"4320p", "1080i", "720p", "576p", "540p", "480p", "360p"})
_ELIGIBLE_META_DISC_SOURCES = frozenset(
    {"bluray", "uhdbluray", "ultrahdbluray", "bd", "uhdbd", "bdremux", "uhdbdremux"}
)
_ELIGIBLE_META_REMUX_PROCESSING = frozenset({"remux", "bdremux", "uhdremux", "uhdbdremux"})
_NAME_DISC_SOURCE_TOKENS = frozenset(
    {"bluray", "blu-ray", "uhdbluray", "ultrahdbluray", "bdremux", "bd-remux", "uhdbdremux"}
)
_NAME_REMUX_TOKENS = frozenset({"remux", "bdremux", "bd-remux", "uhdremux", "uhdbdremux"})
_NAME_NON_DISC_SOURCE_TOKENS = frozenset(
    {
        "web-dl",
        "webdl",
        "web-rip",
        "webrip",
        "web",
        "hdtv",
        "pdtv",
        "sdtv",
        "dvd",
        "dvdrip",
        "bdrip",
        "brrip",
        "hdrip",
        "cam",
        "hdcam",
        "ts",
        "hdts",
        "telesync",
        "telecine",
        "dvdscr",
        "tvrip",
        "vhsrip",
        "vodrip",
    }
)
_NAME_NON_REMUX_PROCESSING_TOKENS = frozenset(
    {
        "x264",
        "x265",
        "xvid",
        "divx",
        "encode",
        "reencode",
        "transcode",
        "bd25",
        "bd50",
        "bd66",
        "bd100",
        "bdmv",
        "iso",
        "untouched",
    }
)
_QUALITY_TECH_START_TOKENS = frozenset(
    {
        "4320p",
        "2160p",
        "1080p",
        "1080i",
        "720p",
        "576p",
        "540p",
        "480p",
        "360p",
        "4k",
        "uhd",
        "bluray",
        "blu-ray",
        "uhdbluray",
        "ultrahdbluray",
        "bdrip",
        "brrip",
        "bdremux",
        "bd-remux",
        "uhdbdremux",
        "remux",
        "web-dl",
        "webdl",
        "web-rip",
        "webrip",
        "hdtv",
        "dvdrip",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "avc",
        "av1",
    }
)
_HYPHEN_TECH_PREV_TOKENS = frozenset({"web", "blu", "bd", "uhd", "vc", "mpeg", "dts", "h", "dd", "ddp", "hdr"})
_HYPHEN_TECH_SUFFIX_TOKENS = frozenset(
    {"dl", "rip", "ray", "remux", "hd", "ma", "es", "x", "1", "2", "264", "265", "10", "plus", "atmos", "truehd", "hevc", "avc"}
)


def _normalize_filter_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _strip_trailing_release_group(name: str) -> str:
    cleaned = re.sub(r"\.(?:mkv|mp4|m2ts|iso|torrent)$", "", (name or "").strip(), flags=re.IGNORECASE)
    m = re.search(r"([A-Za-z0-9]+)-([A-Za-z0-9]{1,24})$", cleaned)
    if not m:
        return cleaned
    prev_part = m.group(1).lower()
    suffix_part = m.group(2).lower()
    if prev_part in _HYPHEN_TECH_PREV_TOKENS or suffix_part in _HYPHEN_TECH_SUFFIX_TOKENS:
        return cleaned
    return cleaned[: m.start(2) - 1]


def _extract_release_tech_tokens(exact_name: str) -> list[str]:
    stripped = _strip_trailing_release_group(exact_name)
    tokens = [t for t in re.split(r"[\s._\[\]\(\)\{\}/\\,:;]+", stripped) if t]
    if not tokens:
        return []

    first_tech_idx = len(tokens)
    for idx in range(1, len(tokens)):
        if tokens[idx].lower() in _QUALITY_TECH_START_TOKENS:
            first_tech_idx = idx
            break

    year_idx: Optional[int] = None
    for idx in range(first_tech_idx - 1, 0, -1):
        if _YEAR_TOKEN_RE.fullmatch(tokens[idx]):
            year_idx = idx
            break

    if year_idx is not None:
        return [t.lower() for t in tokens[year_idx + 1 :]]
    if first_tech_idx < len(tokens):
        return [t.lower() for t in tokens[first_tech_idx:]]
    return []


def _evaluate_remux_quality_eligibility(
    *,
    exact_name: str,
    resolution: Optional[str],
    source: Optional[str],
    media_processing: Optional[str],
) -> tuple[bool, Optional[str]]:
    """
    Require compatible 1080p or 2160p, Blu-ray/UHD Blu-ray disc source, and REMUX processing
    across BOTH the release name and structured metadata. Missing or contradictory evidence
    is ineligible.
    """
    tech_tokens = _extract_release_tech_tokens(exact_name)
    if not tech_tokens:
        return False, "missing_name_quality"

    token_set = set(tech_tokens)
    adjacent_pairs = {(tech_tokens[i], tech_tokens[i + 1]) for i in range(len(tech_tokens) - 1)}

    if token_set & _DISALLOWED_NAME_RESOLUTIONS:
        return False, "disallowed_name_resolution"
    name_resolutions = token_set & _ELIGIBLE_REMUX_RESOLUTIONS
    if not name_resolutions:
        return False, "missing_name_resolution"
    if len(name_resolutions) > 1:
        return False, "conflicting_name_resolutions"
    name_res = next(iter(name_resolutions))

    if (token_set & _NAME_NON_DISC_SOURCE_TOKENS) or (("web", "dl") in adjacent_pairs) or (("web", "rip") in adjacent_pairs):
        return False, "non_disc_name_source"
    has_name_disc_source = bool(
        (token_set & _NAME_DISC_SOURCE_TOKENS)
        or (("blu", "ray") in adjacent_pairs)
        or (("bd", "remux") in adjacent_pairs)
    )
    if not has_name_disc_source:
        return False, "missing_name_bluray_source"

    if (
        (token_set & _NAME_NON_REMUX_PROCESSING_TOKENS)
        or (("complete", "bluray") in adjacent_pairs)
        or (("complete", "blu-ray") in adjacent_pairs)
        or (("full", "bluray") in adjacent_pairs)
        or (("full", "blu-ray") in adjacent_pairs)
    ):
        return False, "non_remux_name_processing"
    has_name_remux = bool((token_set & _NAME_REMUX_TOKENS) or (("bd", "remux") in adjacent_pairs))
    if not has_name_remux:
        return False, "missing_name_remux_processing"

    meta_res = (resolution or "").strip().lower()
    if not meta_res:
        return False, "missing_metadata_resolution"
    if meta_res not in _ELIGIBLE_REMUX_RESOLUTIONS:
        return False, "ineligible_metadata_resolution"
    if meta_res != name_res:
        return False, "resolution_mismatch"

    meta_src = re.sub(r"[\s._-]+", "", (source or "").strip().lower())
    if not meta_src:
        return False, "missing_metadata_source"
    if meta_src not in _ELIGIBLE_META_DISC_SOURCES:
        return False, "ineligible_metadata_source"

    meta_proc = re.sub(r"[\s._-]+", "", (media_processing or "").strip().lower())
    if not meta_proc:
        return False, "missing_metadata_processing"
    if meta_proc not in _ELIGIBLE_META_REMUX_PROCESSING:
        return False, "ineligible_metadata_processing"

    if name_res == "1080p" and ("uhd" in meta_src or "uhd" in token_set):
        return False, "uhd_source_incompatible_with_1080p_remux"

    return True, None


def _extract_filter_info(
    raw_payload: dict[str, Any],
    data_dict: dict[str, Any],
) -> tuple[Optional[str], Optional[int], bool]:
    """
    Extract autobrr filter metadata from `data.filter` (or top-level `filter`).
    Returns `(filter_name, filter_id, is_valid_structure)`.
    """
    data_filter = data_dict.get("filter")
    top_filter = raw_payload.get("filter")

    def _parse_one(obj: Any) -> tuple[Optional[str], Optional[int], bool]:
        if isinstance(obj, dict):
            name = _clean_str(obj.get("name"))
            raw_id = obj.get("id")
            fid = _clean_int(raw_id, min_val=1) if raw_id is not None else None
            if raw_id is not None and fid is None:
                return name, None, False
            return name, fid, bool(name)
        if isinstance(obj, str):
            name = _clean_str(obj)
            return name, None, bool(name)
        return None, None, False

    if data_filter is not None and top_filter is not None:
        dn, did, dok = _parse_one(data_filter)
        tn, tid, tok = _parse_one(top_filter)
        if not dok or not tok:
            return dn or tn, did or tid, False
        if _normalize_filter_name(dn or "") != _normalize_filter_name(tn or ""):
            return dn, did, False
        if did is not None and tid is not None and did != tid:
            return dn, did, False
        return dn, did if did is not None else tid, True

    chosen = data_filter if data_filter is not None else top_filter
    if chosen is None:
        return None, None, False
    return _parse_one(chosen)


def _extract_action_info(
    raw_payload: dict[str, Any],
    data_dict: dict[str, Any],
    *,
    top_event: str = "",
) -> tuple[bool, bool, list[str], Optional[str], str, bool]:
    top_action = raw_payload.get("action")
    top_action_dict = top_action if isinstance(top_action, dict) else {}
    data_action = data_dict.get("action")
    data_action_dict = data_action if isinstance(data_action, dict) else {}

    result = data_dict.get("result")
    if not isinstance(result, dict):
        result = raw_payload.get("result")
    result_dict = result if isinstance(result, dict) else {}

    top_type = _clean_str(top_action_dict.get("type"))
    data_type = _clean_str(data_action_dict.get("type"))
    if top_type and data_type and top_type.lower() != data_type.lower():
        raw_type: Optional[str] = f"conflict:{data_type}/{top_type}"
        action_is_radarr = False
    else:
        raw_type = data_type or top_type
        action_is_radarr = raw_type is not None and raw_type.lower() == "radarr"

    action_rejected_flag = _clean_bool(data_action_dict.get("rejected"))
    if action_rejected_flag is None:
        action_rejected_flag = _clean_bool(top_action_dict.get("rejected"))
    result_rejected_flag = _clean_bool(result_dict.get("rejected"))
    result_status = (_clean_str(result_dict.get("status")) or "").upper()
    event_norm = top_event.strip().lower()

    explicitly_not_rejected = bool(
        action_rejected_flag is False
        or result_rejected_flag is False
        or (result_status and result_status not in _REJECTED_RESULT_STATUSES)
    )
    rejected = (not explicitly_not_rejected) and bool(
        action_rejected_flag is True
        or result_rejected_flag is True
        or event_norm in _REJECTED_EVENTS
        or result_status in _REJECTED_RESULT_STATUSES
    )

    raw_rejections = result_dict.get("rejections")
    rejections: list[str] = []
    has_malformed_rejections = False
    if raw_rejections is None:
        pass
    elif isinstance(raw_rejections, list):
        for item in raw_rejections:
            if isinstance(item, str) and item.strip():
                rejections.append(redact_secrets(item.strip(), max_chars=240))
            else:
                has_malformed_rejections = True
    elif isinstance(raw_rejections, str):
        if raw_rejections.strip():
            rejections.append(redact_secrets(raw_rejections.strip(), max_chars=240))
        else:
            has_malformed_rejections = True
    else:
        has_malformed_rejections = True

    return rejected, action_is_radarr, rejections, raw_type, result_status, has_malformed_rejections


@dataclass(frozen=True)
class ClassifiedWebhook:
    classification: str  # movie_candidate | movie_category_type_conflict | non_movie_archive_only
    diagnostic_note: Optional[str] = None
    indexer_raw: str = ""
    indexer_norm: str = ""
    external_id_norm: str = ""
    exact_release_name: str = ""
    hint_title: Optional[str] = None
    hint_year: Optional[int] = None
    category: Optional[str] = None
    meta_imdb: Optional[str] = None
    meta_tmdb: Optional[int] = None
    resolution: Optional[str] = None
    source: Optional[str] = None
    dynamic_range: list[str] = field(default_factory=list)
    media_processing: Optional[str] = None
    group: Optional[str] = None
    size_gb: Optional[float] = None
    freeleech: Optional[bool] = None
    action_rejected: bool = False
    action_is_radarr: bool = False
    rejections: list[str] = field(default_factory=list)
    has_malformed_rejections: bool = False
    has_download_url: bool = False
    has_canonical_tl_download_ref: bool = False
    has_season_or_episode: bool = False
    has_parsed_title_year: bool = False
    release_name_too_long: bool = False
    incoming_evidence_digest: str = ""

    def to_sanitized_snapshot(self) -> dict[str, Any]:
        """Serialize strictly sanitized non-secret intake evidence (no URLs/passkeys/tokens)."""
        def _opt(val: Optional[str], limit: int) -> Optional[str]:
            if not val:
                return None
            clean = redact_secrets(val, max_chars=limit)
            return clean or None

        return {
            "indexer_raw": redact_secrets(self.indexer_raw, max_chars=80),
            "indexer_norm": redact_secrets(self.indexer_norm, max_chars=80).lower(),
            "external_id_norm": redact_secrets(self.external_id_norm, max_chars=80),
            "exact_release_name": redact_secrets(self.exact_release_name, max_chars=512),
            "hint_title": _opt(self.hint_title, 240),
            "hint_year": self.hint_year,
            "category": _opt(self.category, 120),
            "meta_imdb": self.meta_imdb,
            "meta_tmdb": self.meta_tmdb,
            "resolution": _opt(self.resolution, 40),
            "source": _opt(self.source, 60),
            "dynamic_range": [
                clean
                for d in self.dynamic_range
                for clean in (redact_secrets(d, max_chars=40),)
                if clean
            ],
            "media_processing": _opt(self.media_processing, 60),
            "group": _opt(self.group, 80),
            "size_gb": self.size_gb,
            "freeleech": self.freeleech,
            "action_rejected": self.action_rejected,
            "action_is_radarr": self.action_is_radarr,
            "rejections": [
                clean
                for r in self.rejections
                for clean in (redact_secrets(r, max_chars=240),)
                if clean
            ],
            "has_malformed_rejections": self.has_malformed_rejections,
            "has_download_url": self.has_download_url,
            "has_canonical_tl_download_ref": self.has_canonical_tl_download_ref,
            "has_season_or_episode": self.has_season_or_episode,
            "has_parsed_title_year": self.has_parsed_title_year,
            "release_name_too_long": self.release_name_too_long,
        }

    @classmethod
    def from_sanitized_snapshot(cls, snap: dict[str, Any], incoming_digest: str) -> "ClassifiedWebhook":
        exact_name = str(snap.get("exact_release_name") or "")
        hint_title = snap.get("hint_title")
        hint_year = snap.get("hint_year")
        if "has_parsed_title_year" in snap:
            has_ty = bool(snap.get("has_parsed_title_year"))
        else:
            pt, py = _extract_title_year_from_release_name(exact_name)
            has_ty = bool((hint_title or pt) and (hint_year or py))
        return cls(
            classification="movie_candidate",
            indexer_raw=str(snap.get("indexer_raw") or ""),
            indexer_norm=str(snap.get("indexer_norm") or ""),
            external_id_norm=str(snap.get("external_id_norm") or ""),
            exact_release_name=exact_name,
            hint_title=hint_title,
            hint_year=hint_year,
            category=snap.get("category"),
            meta_imdb=snap.get("meta_imdb"),
            meta_tmdb=snap.get("meta_tmdb"),
            resolution=snap.get("resolution"),
            source=snap.get("source"),
            dynamic_range=list(snap.get("dynamic_range") or []),
            media_processing=snap.get("media_processing"),
            group=snap.get("group"),
            size_gb=snap.get("size_gb"),
            freeleech=snap.get("freeleech"),
            action_rejected=bool(snap.get("action_rejected")),
            action_is_radarr=bool(snap.get("action_is_radarr", False)),
            rejections=list(snap.get("rejections") or []),
            has_malformed_rejections=bool(snap.get("has_malformed_rejections", False)),
            has_download_url=bool(snap.get("has_download_url")),
            has_canonical_tl_download_ref=bool(snap.get("has_canonical_tl_download_ref")),
            has_season_or_episode=bool(snap.get("has_season_or_episode")),
            has_parsed_title_year=has_ty,
            release_name_too_long=bool(snap.get("release_name_too_long", len(exact_name) > MAX_ALT_GRAB_RELEASE_NAME_LEN)),
            incoming_evidence_digest=incoming_digest,
        )


def classify_rejection_reasons(classified: ClassifiedWebhook) -> tuple[str, Optional[str]]:
    """
    Classify autobrr/Radarr rejection signals per legacy Alternative Grab rules (OPS-REPORT #3):
      - ("not_rejected", None): not rejected
      - ("legacy_alt_grab_eligible", primary_class): Radarr rejection on a TorrentLeech movie
        release with valid title/year, no season/episode, release name <= 512 chars, numeric
        external_id, canonical HTTPS TL download URL bound to external_id, and 1-8 rejection
        reasons EVERY ONE of which matches one of the 4 strict legacy forms (with zero malformed
        or unreadable rejection items).
        (Worker must still verify movie-actions-api reports alternative_grab_configured=True!)
      - ("quality_rejected_suppress", matched_class): exclusively routine quality rejection
        that does not satisfy legacy Alternative Grab eligibility -> suppress early.
      - ("needs_identity_and_radarr_check", None): empty, malformed/unreadable, existing-movie,
        or unknown/mixed non-quality rejection -> must continue to identity verification + live Radarr check.
    """
    if not classified.action_rejected:
        return "not_rejected", None
    if classified.has_malformed_rejections or not classified.rejections:
        return "needs_identity_and_radarr_check", None

    legacy_ok, legacy_classes = validate_legacy_alt_grab_rejections(classified.rejections)
    if legacy_ok:
        source_eligible = (
            classified.action_is_radarr
            and classified.indexer_norm == "torrentleech"
            and bool(_NUMERIC_ID_RE.fullmatch(classified.external_id_norm))
            and not classified.release_name_too_long
            and 1 <= len(classified.exact_release_name) <= MAX_ALT_GRAB_RELEASE_NAME_LEN
            and not classified.has_season_or_episode
            and classified.has_parsed_title_year
            and classified.has_canonical_tl_download_ref
        )
        if source_eligible:
            return "legacy_alt_grab_eligible", legacy_classes[0]
        return "quality_rejected_suppress", legacy_classes[0]

    # Not strictly eligible for legacy Alternative Grab; check if ALL reasons are routine quality rejections.
    matched_classes: list[str] = []
    for reason in classified.rejections:
        matched: Optional[str] = match_legacy_alt_grab_rejection_reason(reason)
        if matched is None:
            for class_name, pattern in _QUALITY_REJECTION_PATTERNS:
                if pattern.search(reason):
                    matched = class_name
                    break
        if matched is None:
            # Any unknown / existing-movie / identity rejection must NOT be silently dropped as quality-only
            return "needs_identity_and_radarr_check", None
        if matched not in matched_classes:
            matched_classes.append(matched)

    return "quality_rejected_suppress", matched_classes[0]


def classify_webhook_payload(
    raw_payload: dict[str, Any],
    *,
    allowed_filter_names: tuple[str, ...] = DEFAULT_ALLOWED_FILTER_NAMES,
    allowed_filter_ids: tuple[int, ...] = (),
) -> ClassifiedWebhook:
    """
    Classify an already-archived JSON webhook delivery per docs/LEGACY_HANDOVER.md & Issue #13 §1:
      - Announce, IRC, feed, housekeeping, and global `release.new` events -> non_movie_archive_only.
      - Require a qualifying Radarr action rejection event tied to an allowed autobrr filter
        (default: `Movies Remux`).
      - Movie category + conflicting release type -> movie_category_type_conflict (diagnostic only).
      - Require compatible 1080p or 2160p, Blu-ray/UHD Blu-ray disc source, and REMUX evidence
        across both release name and structured metadata. Missing or contradictory quality evidence
        -> non_movie_archive_only.
      - Qualifying movie lane event -> movie_candidate with exact release_key correlation fields.
    """
    top_event = (_clean_str(raw_payload.get("event")) or _clean_str(raw_payload.get("type")) or "").lower()
    if top_event in _NON_MOVIE_EVENT_TYPES:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note=f"non_acquisition_event:{top_event}",
        )
    if top_event not in _QUALIFYING_ACTION_EVENTS:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="non_qualifying_event" if top_event else "missing_event",
        )

    data = raw_payload.get("data")
    if not isinstance(data, dict):
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="missing_data_object",
        )

    release = data.get("release")
    if not isinstance(release, dict):
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="missing_release_object",
        )

    exact_name = _clean_str(release.get("name")) or _clean_str(release.get("torrent_name"))
    if not exact_name:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="missing_release_name",
        )

    (
        action_rejected,
        action_is_radarr,
        rejections,
        action_type_raw,
        _result_status,
        has_malformed_rejections,
    ) = _extract_action_info(raw_payload, data, top_event=top_event)
    if not action_rejected:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="non_rejected_action",
            action_rejected=False,
            action_is_radarr=action_is_radarr,
            rejections=rejections,
            has_malformed_rejections=has_malformed_rejections,
        )
    if not action_is_radarr:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="non_radarr_action" if action_type_raw else "missing_action_type",
            action_rejected=action_rejected,
            action_is_radarr=False,
            rejections=rejections,
            has_malformed_rejections=has_malformed_rejections,
        )

    filter_name, filter_id, filter_valid = _extract_filter_info(raw_payload, data)
    allowed_names_norm = {_normalize_filter_name(n) for n in allowed_filter_names if n and n.strip()}
    if not filter_valid or not filter_name or _normalize_filter_name(filter_name) not in allowed_names_norm:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="filter_mismatch" if filter_name else "missing_filter_name",
            action_rejected=action_rejected,
            action_is_radarr=action_is_radarr,
            rejections=rejections,
            has_malformed_rejections=has_malformed_rejections,
        )
    if allowed_filter_ids and (filter_id is None or filter_id not in allowed_filter_ids):
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="filter_id_mismatch" if filter_id is not None else "missing_filter_id",
            action_rejected=action_rejected,
            action_is_radarr=action_is_radarr,
            rejections=rejections,
            has_malformed_rejections=has_malformed_rejections,
        )

    indexer_raw = _extract_indexer_name(raw_payload, data, release)
    indexer_norm = indexer_raw.lower()
    external_id_raw = release.get("external_id")
    if external_id_raw is None:
        external_id_raw = release.get("torrent_id")
    external_id_norm = str(external_id_raw).strip() if external_id_raw is not None else ""

    category = _clean_str(release.get("category")) or _clean_str(data.get("category"))
    release_type = (_clean_str(release.get("type")) or "").lower()
    cat_tokens = _tokenize_category(category)

    has_movie_cat = bool(cat_tokens & _MOVIE_CATEGORY_TOKENS) and not bool(
        cat_tokens & _NON_MOVIE_CATEGORY_TOKENS
    )
    has_non_movie_cat = bool(cat_tokens & _NON_MOVIE_CATEGORY_TOKENS)

    if has_non_movie_cat:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="non_movie_category",
        )

    if has_movie_cat and (release_type in _CONFLICTING_RELEASE_TYPES or _EPISODIC_RELEASE_RE.search(exact_name)):
        conflict_detail = release_type if release_type in _CONFLICTING_RELEASE_TYPES else "episodic_release_name"
        return ClassifiedWebhook(
            classification="movie_category_type_conflict",
            diagnostic_note=f"movie_category_conflicting_release_type:{conflict_detail}",
            indexer_raw=indexer_raw,
            indexer_norm=indexer_norm,
            external_id_norm=external_id_norm,
            exact_release_name=exact_name,
            category=category,
        )

    if release_type in _CONFLICTING_RELEASE_TYPES:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note=f"non_movie_release_type:{release_type}",
        )

    if not has_movie_cat and release_type not in _MOVIE_RELEASE_TYPES:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note="unclassified_non_movie",
        )

    resolution = _clean_str(release.get("resolution"))
    source = _clean_str(release.get("source"))
    media_processing = _clean_str(release.get("media_processing"))

    quality_ok, quality_reason = _evaluate_remux_quality_eligibility(
        exact_name=exact_name,
        resolution=resolution,
        source=source,
        media_processing=media_processing,
    )
    if not quality_ok:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note=f"ineligible_quality:{quality_reason}",
            action_rejected=action_rejected,
            action_is_radarr=action_is_radarr,
            rejections=rejections,
            has_malformed_rejections=has_malformed_rejections,
        )

    hint_title = _clean_str(release.get("title"))
    hint_year = _clean_int(release.get("year"), min_val=1888, max_val=2100)
    parsed_title, parsed_year = _extract_title_year_from_release_name(exact_name)
    has_parsed_title_year = bool((hint_title or parsed_title) and (hint_year or parsed_year))

    raw_season = release.get("season")
    raw_episode = release.get("episode")
    has_season_or_episode = bool(
        (isinstance(raw_season, int) and not isinstance(raw_season, bool) and raw_season > 0)
        or (isinstance(raw_episode, int) and not isinstance(raw_episode, bool) and raw_episode > 0)
        or (isinstance(raw_season, str) and raw_season.strip() not in ("", "0"))
        or (isinstance(raw_episode, str) and raw_episode.strip() not in ("", "0"))
        or _EPISODIC_RELEASE_RE.search(exact_name)
    )

    meta_imdb = _clean_imdb(release.get("meta_imdb") or release.get("imdb_id"))
    meta_tmdb = _clean_int(release.get("meta_tmdb") or release.get("tmdb_id"), min_val=1)
    dynamic_range = _clean_str_list(release.get("hdr") or release.get("dynamic_range"))
    group = _clean_str(release.get("group"))
    size_gb = _parse_size_gb(release)

    freeleech: Optional[bool] = None
    if "freeleech" in release:
        freeleech = _clean_bool(release.get("freeleech"))
    elif "is_freeleech" in release:
        freeleech = _clean_bool(release.get("is_freeleech"))

    # Inspect the first non-empty download URL strictly in memory (never stored/logged!)
    first_download_url: Optional[str] = None
    for container in (release, data, raw_payload):
        for k in ("download_url", "downloadUrl", "torrent_url", "torrentUrl"):
            val = container.get(k)
            if isinstance(val, str) and val.strip():
                first_download_url = val.strip()
                break
        if first_download_url:
            break

    has_download_url = bool(
        first_download_url
        and (first_download_url.startswith("https://") or first_download_url.startswith("http://"))
    )
    has_canonical_tl_download_ref = bool(
        first_download_url and is_canonical_tl_download_url(first_download_url, external_id_norm)
    )

    def _san_opt(val: Optional[str], limit: int) -> Optional[str]:
        if not val:
            return None
        cleaned = redact_secrets(val, max_chars=limit)
        return cleaned or None

    release_name_too_long = len(exact_name) > MAX_ALT_GRAB_RELEASE_NAME_LEN
    safe_indexer_raw = redact_secrets(indexer_raw, max_chars=80)
    safe_indexer_norm = safe_indexer_raw.lower()
    safe_external_id_norm = redact_secrets(external_id_norm, max_chars=80)
    safe_exact_name = redact_secrets(exact_name, max_chars=512)
    safe_hint_title = _san_opt(hint_title, 240)
    safe_category = _san_opt(category, 120)
    safe_resolution = _san_opt(resolution, 40)
    safe_source = _san_opt(source, 60)
    safe_dynamic_range = [
        clean
        for d in dynamic_range
        for clean in (redact_secrets(d, max_chars=40),)
        if clean
    ]
    safe_media_processing = _san_opt(media_processing, 60)
    safe_group = _san_opt(group, 80)

    digest_input = {
        "indexer": safe_indexer_norm,
        "external_id": safe_external_id_norm,
        "exact_release_name": safe_exact_name,
        "hint_title": safe_hint_title.lower() if safe_hint_title else None,
        "hint_year": hint_year,
        "category": safe_category.lower() if safe_category else None,
        "meta_imdb": meta_imdb,
        "meta_tmdb": meta_tmdb,
        "resolution": safe_resolution.lower() if safe_resolution else None,
        "source": safe_source.lower() if safe_source else None,
        "dynamic_range": sorted({d.lower() for d in safe_dynamic_range}),
        "media_processing": safe_media_processing.lower() if safe_media_processing else None,
        "group": safe_group.lower() if safe_group else None,
        "size_gb": size_gb,
        "freeleech": freeleech,
        "action_rejected": action_rejected,
        "action_is_radarr": action_is_radarr,
        "rejections": rejections,
        "has_malformed_rejections": has_malformed_rejections,
        "has_canonical_tl_download_ref": has_canonical_tl_download_ref,
        "has_season_or_episode": has_season_or_episode,
        "release_name_too_long": release_name_too_long,
    }
    incoming_digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()

    return ClassifiedWebhook(
        classification="movie_candidate",
        indexer_raw=safe_indexer_raw,
        indexer_norm=safe_indexer_norm,
        external_id_norm=safe_external_id_norm,
        exact_release_name=safe_exact_name,
        hint_title=safe_hint_title,
        hint_year=hint_year,
        category=safe_category,
        meta_imdb=meta_imdb,
        meta_tmdb=meta_tmdb,
        resolution=safe_resolution,
        source=safe_source,
        dynamic_range=safe_dynamic_range,
        media_processing=safe_media_processing,
        group=safe_group,
        size_gb=size_gb,
        freeleech=freeleech,
        action_rejected=action_rejected,
        action_is_radarr=action_is_radarr,
        rejections=rejections,
        has_malformed_rejections=has_malformed_rejections,
        has_download_url=has_download_url,
        has_canonical_tl_download_ref=has_canonical_tl_download_ref,
        has_season_or_episode=has_season_or_episode,
        has_parsed_title_year=has_parsed_title_year,
        release_name_too_long=release_name_too_long,
        incoming_evidence_digest=incoming_digest,
    )


def compute_legacy_occurrence_evidence_digest(raw_payload: dict[str, Any]) -> Optional[str]:
    """
    Recompute the current `incoming_evidence_digest` from a historical archived webhook JSON
    without re-applying event/filter/quality allowlist gates (used during pre-upgrade migration
    backfill so older pre-upgrade deliveries are recognized as superseded evidence on replay).
    """
    if not isinstance(raw_payload, dict):
        return None
    data = raw_payload.get("data")
    if not isinstance(data, dict):
        return None
    release = data.get("release")
    if not isinstance(release, dict):
        return None
    exact_name = _clean_str(release.get("name")) or _clean_str(release.get("torrent_name"))
    if not exact_name:
        return None

    top_event = (_clean_str(raw_payload.get("event")) or _clean_str(raw_payload.get("type")) or "").lower()
    (
        action_rejected,
        action_is_radarr,
        rejections,
        _action_type_raw,
        _result_status,
        has_malformed_rejections,
    ) = _extract_action_info(raw_payload, data, top_event=top_event)

    indexer_raw = _extract_indexer_name(raw_payload, data, release)
    external_id_raw = release.get("external_id")
    if external_id_raw is None:
        external_id_raw = release.get("torrent_id")
    external_id_norm = str(external_id_raw).strip() if external_id_raw is not None else ""

    category = _clean_str(release.get("category")) or _clean_str(data.get("category"))
    resolution = _clean_str(release.get("resolution"))
    source = _clean_str(release.get("source"))
    media_processing = _clean_str(release.get("media_processing"))
    hint_title = _clean_str(release.get("title"))
    hint_year = _clean_int(release.get("year"), min_val=1888, max_val=2100)

    raw_season = release.get("season")
    raw_episode = release.get("episode")
    has_season_or_episode = bool(
        (isinstance(raw_season, int) and not isinstance(raw_season, bool) and raw_season > 0)
        or (isinstance(raw_episode, int) and not isinstance(raw_episode, bool) and raw_episode > 0)
        or (isinstance(raw_season, str) and raw_season.strip() not in ("", "0"))
        or (isinstance(raw_episode, str) and raw_episode.strip() not in ("", "0"))
        or _EPISODIC_RELEASE_RE.search(exact_name)
    )

    meta_imdb = _clean_imdb(release.get("meta_imdb") or release.get("imdb_id"))
    meta_tmdb = _clean_int(release.get("meta_tmdb") or release.get("tmdb_id"), min_val=1)
    dynamic_range = _clean_str_list(release.get("hdr") or release.get("dynamic_range"))
    group = _clean_str(release.get("group"))
    size_gb = _parse_size_gb(release)

    freeleech: Optional[bool] = None
    if "freeleech" in release:
        freeleech = _clean_bool(release.get("freeleech"))
    elif "is_freeleech" in release:
        freeleech = _clean_bool(release.get("is_freeleech"))

    first_download_url: Optional[str] = None
    for container in (release, data, raw_payload):
        for k in ("download_url", "downloadUrl", "torrent_url", "torrentUrl"):
            val = container.get(k)
            if isinstance(val, str) and val.strip():
                first_download_url = val.strip()
                break
        if first_download_url:
            break

    has_canonical_tl_download_ref = bool(
        first_download_url and is_canonical_tl_download_url(first_download_url, external_id_norm)
    )

    def _san_opt(val: Optional[str], limit: int) -> Optional[str]:
        if not val:
            return None
        cleaned = redact_secrets(val, max_chars=limit)
        return cleaned or None

    release_name_too_long = len(exact_name) > MAX_ALT_GRAB_RELEASE_NAME_LEN
    safe_indexer_raw = redact_secrets(indexer_raw, max_chars=80)
    safe_indexer_norm = safe_indexer_raw.lower()
    safe_external_id_norm = redact_secrets(external_id_norm, max_chars=80)
    safe_exact_name = redact_secrets(exact_name, max_chars=512)
    safe_hint_title = _san_opt(hint_title, 240)
    safe_category = _san_opt(category, 120)
    safe_resolution = _san_opt(resolution, 40)
    safe_source = _san_opt(source, 60)
    safe_dynamic_range = [
        clean
        for d in dynamic_range
        for clean in (redact_secrets(d, max_chars=40),)
        if clean
    ]
    safe_media_processing = _san_opt(media_processing, 60)
    safe_group = _san_opt(group, 80)

    digest_input = {
        "indexer": safe_indexer_norm,
        "external_id": safe_external_id_norm,
        "exact_release_name": safe_exact_name,
        "hint_title": safe_hint_title.lower() if safe_hint_title else None,
        "hint_year": hint_year,
        "category": safe_category.lower() if safe_category else None,
        "meta_imdb": meta_imdb,
        "meta_tmdb": meta_tmdb,
        "resolution": safe_resolution.lower() if safe_resolution else None,
        "source": safe_source.lower() if safe_source else None,
        "dynamic_range": sorted({d.lower() for d in safe_dynamic_range}),
        "media_processing": safe_media_processing.lower() if safe_media_processing else None,
        "group": safe_group.lower() if safe_group else None,
        "size_gb": size_gb,
        "freeleech": freeleech,
        "action_rejected": action_rejected,
        "action_is_radarr": action_is_radarr,
        "rejections": rejections,
        "has_malformed_rejections": has_malformed_rejections,
        "has_canonical_tl_download_ref": has_canonical_tl_download_ref,
        "has_season_or_episode": has_season_or_episode,
        "release_name_too_long": release_name_too_long,
    }
    return hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
