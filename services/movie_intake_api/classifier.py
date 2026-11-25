from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import AlternativeGrabPolicy
from .security import redact_secrets

_IMDB_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)
_EPISODIC_RELEASE_RE = re.compile(
    r"(?<![a-z0-9])(?:s\d{1,2}e\d{1,3}|s\d{1,2}(?![a-z0-9])|season[\s._-]?\d{1,2}|episode[\s._-]?\d{1,3}|complete[\s._-]?series)(?![a-z0-9])",
    re.IGNORECASE,
)

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

_QUALITY_REJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cutoff_met",
        re.compile(r"\b(?:cutoff\s+(?:is\s+)?(?:already\s+)?met|meets?\s+(?:quality\s+)?cutoff|quality\s+cutoff)\b", re.IGNORECASE),
    ),
    (
        "not_upgrade",
        re.compile(
            r"\b(?:not\s+an?\s+(?:quality\s+)?upgrade|does\s+not\s+improve|equal\s+or\s+higher\s+quality|lower\s+quality|existing\s+file\s+is\s+better)\b",
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
        re.compile(r"\b(?:custom\s+format\s+score|minimum\s+custom\s+format)\b", re.IGNORECASE),
    ),
    (
        "size_limit",
        re.compile(r"\b(?:exceeds\s+maximum\s+size|below\s+minimum\s+size|size\s+is\s+(?:above|below))\b", re.IGNORECASE),
    ),
)


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


def _extract_rejections(raw_payload: dict[str, Any], data_dict: dict[str, Any]) -> tuple[bool, list[str]]:
    action = raw_payload.get("action")
    action_dict = action if isinstance(action, dict) else {}
    rejected = bool(_clean_bool(action_dict.get("rejected")) or False)

    result = data_dict.get("result")
    result_dict = result if isinstance(result, dict) else {}
    raw_rejections = result_dict.get("rejections")
    rejections: list[str] = []
    if isinstance(raw_rejections, list):
        for item in raw_rejections:
            if isinstance(item, str) and item.strip():
                rejections.append(redact_secrets(item.strip(), max_chars=200))
    elif isinstance(raw_rejections, str) and raw_rejections.strip():
        rejections.append(redact_secrets(raw_rejections.strip(), max_chars=200))

    return rejected, rejections


def classify_rejection_reasons(
    action_rejected: bool,
    rejections: list[str],
    policy: AlternativeGrabPolicy,
) -> tuple[str, Optional[str]]:
    """
    Classify autobrr/Radarr rejection signals:
      - ("not_rejected", None): not rejected
      - ("quality_rejected_suppress", matched_class): exclusively routine quality rejection,
        not allowed by Alternative Grab policy -> suppress early
      - ("alternative_grab_candidate", matched_class): exclusively routine quality rejection
        AND every matched class is explicitly permitted by Alternative Grab policy
      - ("needs_identity_and_radarr_check", None): empty, existing-movie, or unknown rejection;
        must continue to identity verification + live Radarr check (never suppress blindly)
    """
    if not action_rejected:
        return "not_rejected", None
    if not rejections:
        return "needs_identity_and_radarr_check", None

    matched_classes: list[str] = []
    for reason in rejections:
        matched: Optional[str] = None
        for class_name, pattern in _QUALITY_REJECTION_PATTERNS:
            if pattern.search(reason):
                matched = class_name
                break
        if matched is None:
            # Any unknown / existing-movie / identity rejection must NOT be silently dropped as quality-only
            return "needs_identity_and_radarr_check", None
        if matched not in matched_classes:
            matched_classes.append(matched)

    primary_class = matched_classes[0]
    if policy.enabled and all(c in policy.allowed_rejection_classes for c in matched_classes):
        return "alternative_grab_candidate", primary_class
    return "quality_rejected_suppress", primary_class


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
    rejections: list[str] = field(default_factory=list)
    has_download_url: bool = False
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
            "rejections": list(self.rejections),
            "has_download_url": self.has_download_url,
        }

    @classmethod
    def from_sanitized_snapshot(cls, snap: dict[str, Any], incoming_digest: str) -> "ClassifiedWebhook":
        return cls(
            classification="movie_candidate",
            indexer_raw=str(snap.get("indexer_raw") or ""),
            indexer_norm=str(snap.get("indexer_norm") or ""),
            external_id_norm=str(snap.get("external_id_norm") or ""),
            exact_release_name=str(snap.get("exact_release_name") or ""),
            hint_title=snap.get("hint_title"),
            hint_year=snap.get("hint_year"),
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
            rejections=list(snap.get("rejections") or []),
            has_download_url=bool(snap.get("has_download_url")),
            incoming_evidence_digest=incoming_digest,
        )


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

    action_rejected, rejections = _extract_rejections(raw_payload, data)
    hint_title = _clean_str(release.get("title"))
    hint_year = _clean_int(release.get("year"), min_val=1888, max_val=2100)
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

    has_download_url = any(
        isinstance(container.get(k), str)
        and (
            str(container.get(k)).strip().startswith("https://")
            or str(container.get(k)).strip().startswith("http://")
        )
        for container in (release, data, raw_payload)
        for k in ("download_url", "downloadUrl", "torrent_url", "torrentUrl")
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
        "rejections": rejections,
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
        rejections=rejections,
        has_download_url=has_download_url,
        incoming_evidence_digest=incoming_digest,
    )
