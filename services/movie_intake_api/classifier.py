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


_REJECTED_EVENTS = frozenset({"action.rejected", "push_rejected", "push.rejected"})
_REJECTED_RESULT_STATUSES = frozenset({"PUSH_REJECTED", "REJECTED"})


def _extract_action_info(
    raw_payload: dict[str, Any],
    data_dict: dict[str, Any],
    *,
    top_event: str = "",
) -> tuple[bool, bool, list[str]]:
    top_action = raw_payload.get("action")
    top_action_dict = top_action if isinstance(top_action, dict) else {}
    data_action = data_dict.get("action")
    data_action_dict = data_action if isinstance(data_action, dict) else {}

    result = data_dict.get("result")
    if not isinstance(result, dict):
        result = raw_payload.get("result")
    result_dict = result if isinstance(result, dict) else {}

    raw_type = _clean_str(top_action_dict.get("type")) or _clean_str(data_action_dict.get("type"))
    action_is_radarr = raw_type is None or raw_type.lower() == "radarr"

    action_rejected_flag = _clean_bool(top_action_dict.get("rejected"))
    if action_rejected_flag is None:
        action_rejected_flag = _clean_bool(data_action_dict.get("rejected"))
    result_rejected_flag = _clean_bool(result_dict.get("rejected"))
    result_status = (_clean_str(result_dict.get("status")) or "").upper()
    event_norm = top_event.strip().lower()

    rejected = bool(
        action_rejected_flag is True
        or result_rejected_flag is True
        or event_norm in _REJECTED_EVENTS
        or result_status in _REJECTED_RESULT_STATUSES
    )

    raw_rejections = result_dict.get("rejections")
    rejections: list[str] = []
    if isinstance(raw_rejections, list):
        for item in raw_rejections:
            if isinstance(item, str) and item.strip():
                rejections.append(redact_secrets(item.strip(), max_chars=240))
    elif isinstance(raw_rejections, str) and raw_rejections.strip():
        rejections.append(redact_secrets(raw_rejections.strip(), max_chars=240))

    return rejected, action_is_radarr, rejections


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
    action_is_radarr: bool = True
    rejections: list[str] = field(default_factory=list)
    has_download_url: bool = False
    has_canonical_tl_download_ref: bool = False
    has_season_or_episode: bool = False
    has_parsed_title_year: bool = False
    incoming_evidence_digest: str = ""

    def to_sanitized_snapshot(self) -> dict[str, Any]:
        """Serialize strictly sanitized non-secret intake evidence (no URLs/passkeys)."""
        return {
            "indexer_raw": self.indexer_raw,
            "indexer_norm": self.indexer_norm,
            "external_id_norm": self.external_id_norm,
            "exact_release_name": self.exact_release_name,
            "hint_title": self.hint_title,
            "hint_year": self.hint_year,
            "category": self.category,
            "meta_imdb": self.meta_imdb,
            "meta_tmdb": self.meta_tmdb,
            "resolution": self.resolution,
            "source": self.source,
            "dynamic_range": list(self.dynamic_range),
            "media_processing": self.media_processing,
            "group": self.group,
            "size_gb": self.size_gb,
            "freeleech": self.freeleech,
            "action_rejected": self.action_rejected,
            "action_is_radarr": self.action_is_radarr,
            "rejections": list(self.rejections),
            "has_download_url": self.has_download_url,
            "has_canonical_tl_download_ref": self.has_canonical_tl_download_ref,
            "has_season_or_episode": self.has_season_or_episode,
            "has_parsed_title_year": self.has_parsed_title_year,
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
            action_is_radarr=bool(snap.get("action_is_radarr", True)),
            rejections=list(snap.get("rejections") or []),
            has_download_url=bool(snap.get("has_download_url")),
            has_canonical_tl_download_ref=bool(snap.get("has_canonical_tl_download_ref")),
            has_season_or_episode=bool(snap.get("has_season_or_episode")),
            has_parsed_title_year=has_ty,
            incoming_evidence_digest=incoming_digest,
        )


def classify_rejection_reasons(classified: ClassifiedWebhook) -> tuple[str, Optional[str]]:
    """
    Classify autobrr/Radarr rejection signals per legacy Alternative Grab rules (OPS-REPORT #3):
      - ("not_rejected", None): not rejected
      - ("legacy_alt_grab_eligible", primary_class): Radarr rejection on a TorrentLeech movie
        release with valid title/year, no season/episode, release name <= 512 chars, numeric
        external_id, canonical HTTPS TL download URL bound to external_id, and 1-8 rejection
        reasons EVERY ONE of which matches one of the 4 strict legacy forms.
        (Worker must still verify movie-actions-api reports alternative_grab_configured=True!)
      - ("quality_rejected_suppress", matched_class): exclusively routine quality rejection
        that does not satisfy legacy Alternative Grab eligibility -> suppress early.
      - ("needs_identity_and_radarr_check", None): empty, existing-movie, or unknown/mixed
        non-quality rejection -> must continue to identity verification + live Radarr check.
    """
    if not classified.action_rejected:
        return "not_rejected", None
    if not classified.rejections:
        return "needs_identity_and_radarr_check", None

    legacy_ok, legacy_classes = validate_legacy_alt_grab_rejections(classified.rejections)
    if legacy_ok:
        source_eligible = (
            classified.action_is_radarr
            and classified.indexer_norm == "torrentleech"
            and bool(_NUMERIC_ID_RE.fullmatch(classified.external_id_norm))
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


def classify_webhook_payload(raw_payload: dict[str, Any]) -> ClassifiedWebhook:
    """
    Classify an already-archived JSON webhook delivery per docs/LEGACY_HANDOVER.md:
      - Inspect event type, action.rejected, data.result.rejections, data.release, data.indexer.
      - Announce, IRC, feed, and housekeeping events -> non_movie_archive_only.
      - Movie category + conflicting release type -> movie_category_type_conflict (diagnostic only).
      - Valid movie lane event -> movie_candidate with exact release_key correlation fields.
    """
    top_event = (_clean_str(raw_payload.get("event")) or _clean_str(raw_payload.get("type")) or "").lower()
    if top_event in _NON_MOVIE_EVENT_TYPES:
        return ClassifiedWebhook(
            classification="non_movie_archive_only",
            diagnostic_note=f"non_acquisition_event:{top_event}",
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
            diagnostic_note=f"non_movie_category:{redact_secrets(category, max_chars=80)}",
        )

    if has_movie_cat and (release_type in _CONFLICTING_RELEASE_TYPES or _EPISODIC_RELEASE_RE.search(exact_name)):
        conflict_detail = release_type or "episodic_release_name"
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
            diagnostic_note=f"unclassified_non_movie:cat={redact_secrets(category or 'none', max_chars=40)},type={release_type or 'none'}",
        )

    action_rejected, action_is_radarr, rejections = _extract_action_info(
        raw_payload, data, top_event=top_event
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
    resolution = _clean_str(release.get("resolution"))
    source = _clean_str(release.get("source"))
    dynamic_range = _clean_str_list(release.get("hdr") or release.get("dynamic_range"))
    media_processing = _clean_str(release.get("media_processing") or release.get("container"))
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

    digest_input = {
        "indexer": indexer_norm,
        "external_id": external_id_norm,
        "exact_release_name": exact_name,
        "hint_title": hint_title.lower() if hint_title else None,
        "hint_year": hint_year,
        "category": category.lower() if category else None,
        "meta_imdb": meta_imdb,
        "meta_tmdb": meta_tmdb,
        "resolution": resolution.lower() if resolution else None,
        "source": source.lower() if source else None,
        "dynamic_range": sorted({d.lower() for d in dynamic_range}),
        "media_processing": media_processing.lower() if media_processing else None,
        "group": group.lower() if group else None,
        "size_gb": size_gb,
        "freeleech": freeleech,
        "action_rejected": action_rejected,
        "action_is_radarr": action_is_radarr,
        "rejections": rejections,
        "has_canonical_tl_download_ref": has_canonical_tl_download_ref,
        "has_season_or_episode": has_season_or_episode,
    }
    incoming_digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()

    return ClassifiedWebhook(
        classification="movie_candidate",
        indexer_raw=indexer_raw,
        indexer_norm=indexer_norm,
        external_id_norm=external_id_norm,
        exact_release_name=exact_name,
        hint_title=hint_title,
        hint_year=hint_year,
        category=category,
        meta_imdb=meta_imdb,
        meta_tmdb=meta_tmdb,
        resolution=resolution,
        source=source,
        dynamic_range=dynamic_range,
        media_processing=media_processing,
        group=group,
        size_gb=size_gb,
        freeleech=freeleech,
        action_rejected=action_rejected,
        action_is_radarr=action_is_radarr,
        rejections=rejections,
        has_download_url=has_download_url,
        has_canonical_tl_download_ref=has_canonical_tl_download_ref,
        has_season_or_episode=has_season_or_episode,
        has_parsed_title_year=has_parsed_title_year,
        incoming_evidence_digest=incoming_digest,
    )
