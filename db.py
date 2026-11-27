from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "triage.db"
EVALUATION_CACHE_VERSION = 2
_CACHE_RATING_KEYS = (
    "rotten_tomatoes_critics",
    "rotten_tomatoes_audience",
    "metacritic",
    "imdb",
    "letterboxd",
)

IMDB_ID_RE = re.compile(r"\b(tt\d{7,10})\b", re.IGNORECASE)
YEAR_RE = re.compile(r"^(18[89]\d|19\d\d|20\d\d)$")

_RES_2160_RE = re.compile(r"(?<![a-z0-9])2160p(?![a-z0-9])", re.IGNORECASE)
_RES_1080_RE = re.compile(r"(?<![a-z0-9])1080[pi](?![a-z0-9])", re.IGNORECASE)
_RES_720_RE = re.compile(r"(?<![a-z0-9])720p(?![a-z0-9])", re.IGNORECASE)
_RES_UHD_RE = re.compile(r"(?<![a-z0-9])(?:4k|uhd)(?![a-z0-9])", re.IGNORECASE)
_REMUX_RE = re.compile(r"(?<![a-z0-9])remux(?![a-z0-9])", re.IGNORECASE)
_DV_TAG_RE = re.compile(r"(?<![a-z0-9])(?:dv|dovi|dolby[\s._-]*vision)(?![a-z0-9])", re.IGNORECASE)
_DV_NFO_RE = re.compile(r"(?<![a-z0-9])(?:dovi|dolby[\s._-]*vision)(?![a-z0-9])", re.IGNORECASE)
_HDR10PLUS_RE = re.compile(r"(?<![a-z0-9])(?:hdr10\+|hdr10plus|hdr10p)(?![a-z0-9])", re.IGNORECASE)
_HDR_RE = re.compile(r"(?<![a-z0-9])(?:hdr10|hdr|hlg)(?![a-z0-9])", re.IGNORECASE)
_TRAILING_EXT_RE = re.compile(r"\.(?:mkv|mp4|m2ts|iso|torrent)$", re.IGNORECASE)
_TRAILING_GROUP_RE = re.compile(r"-([A-Za-z0-9]{2,24})$")
_TRAILING_BRACKET_GROUP_RE = re.compile(r"\[([A-Za-z0-9]{2,24})\]$")
_NON_GROUP_SUFFIXES = frozenset(
    {
        "dl",
        "hd",
        "ray",
        "ma",
        "264",
        "265",
        "rip",
        "atmos",
        "hevc",
        "avc",
        "hdr",
        "hdr10",
        "dv",
        "sdr",
        "remux",
    }
)
FAVORITE_RELEASE_GROUPS = frozenset({"cinephiles"})


def _get_favorite_groups() -> frozenset[str]:
    env_val = os.getenv("FAVORITE_RELEASE_GROUPS", "").strip()
    if not env_val:
        return FAVORITE_RELEASE_GROUPS
    parsed = {
        tok.strip().lower()
        for tok in re.split(r"[,;\s]+", env_val)
        if tok.strip()
    }
    return frozenset(parsed) if parsed else FAVORITE_RELEASE_GROUPS


def extract_release_group(raw_text: str, *, group: Optional[str] = None) -> Optional[str]:
    """Extract release group name from explicit tracker field or trailing -GROUP tag."""
    if group and group.strip():
        cleaned = group.strip().lstrip("-").strip()
        return cleaned or None

    text = _TRAILING_EXT_RE.sub("", (raw_text or "").strip()).strip()
    if not text:
        return None

    # Only parse trailing -GROUP when the string looks like a release name
    # (contains a year, resolution, remux, or common release marker)
    tokens_lower = [
        t.lower()
        for t in re.split(r"[\s._\-\[\]\(\)\{\}/\\,:;]+", text)
        if t
    ]
    has_release_marker = any(
        YEAR_RE.match(t)
        or t in ("2160p", "1080p", "1080i", "720p", "4k", "uhd", "remux", "bluray", "web", "webdl", "webrip", "hevc", "avc", "x264", "x265", "h264", "h265")
        for t in tokens_lower
    )
    if not has_release_marker:
        return None

    match = _TRAILING_GROUP_RE.search(text) or _TRAILING_BRACKET_GROUP_RE.search(text)
    if not match:
        return None
    candidate = match.group(1).strip()
    if candidate.lower() in _NON_GROUP_SUFFIXES or YEAR_RE.match(candidate):
        return None
    return candidate


_DV_P7_RE = re.compile(
    r"(?<![a-z0-9])(?:dvhe\.07(?:\.\d{2})?|bl\+el\+rpu|profile[\s._-]*7(?:\.\d+)?|dv[\s._-]*p7|dovi[\s._-]*p7|p7[\s._-]*(?:fel|mel))(?![a-z0-9])",
    re.IGNORECASE,
)
_DV_P8_RE = re.compile(
    r"(?<![a-z0-9])(?:dvhe\.08(?:\.\d{2})?|bl\+rpu|profile[\s._-]*8(?:\.\d+)?|dv[\s._-]*p8|dovi[\s._-]*p8|p8(?:\.1)?)(?![a-z0-9])",
    re.IGNORECASE,
)
_DV_P5_RE = re.compile(
    r"(?<![a-z0-9])(?:dvhe\.05(?:\.\d{2})?|profile[\s._-]*5(?:\.\d+)?|dv[\s._-]*p5|dovi[\s._-]*p5)(?![a-z0-9])",
    re.IGNORECASE,
)
_FEL_NEGATED_RE = re.compile(
    r"\b(?:no|not|without|dropped|drop|stripped|strip|removed|remove|converted\s+from)\s+(?:p7[\s._-]*)?fel\b|\bfel\s*/\s*mel\b|\bmel\s*/\s*fel\b",
    re.IGNORECASE,
)
_FEL_EXPLICIT_RE = re.compile(
    r"\bfull\s+enhancement\s+layer\b"
    r"|\b(?:el\s*type|enhancement\s*layer)\s*[:=]\s*fel\b"
    r"|\b(?:p7|profile[\s._-]*7(?:\.\d+)?|dvhe\.07(?:\.\d{2})?|bl\+el\+rpu)[^.\n]{0,40}\bfel\b"
    r"|\bfel\b[^.\n]{0,30}\b(?:p7|profile[\s._-]*7|dolby[\s._-]*vision|dovi|dv|enhancement|layer|rpu|remux)\b"
    r"|\b(?:dolby[\s._-]*vision|dovi|dv)[^.\n]{0,30}\bfel\b",
    re.IGNORECASE,
)
_FEL_BRACKET_RE = re.compile(r"[\[\(]\s*FEL\s*[\]\)]")
_MEL_EXPLICIT_RE = re.compile(
    r"\bminim(?:al|um)\s+enhancement\s+layer\b"
    r"|\b(?:el\s*type|enhancement\s*layer)\s*[:=]\s*mel\b"
    r"|\b(?:p7|profile[\s._-]*7(?:\.\d+)?|dvhe\.07(?:\.\d{2})?|bl\+el\+rpu)[^.\n]{0,40}\bmel\b"
    r"|\bmel\b[^.\n]{0,30}\b(?:p7|profile[\s._-]*7|dolby[\s._-]*vision|dovi|dv|enhancement|layer|rpu|remux)\b"
    r"|\b(?:dolby[\s._-]*vision|dovi|dv)[^.\n]{0,30}\bmel\b",
    re.IGNORECASE,
)
_MEL_BRACKET_RE = re.compile(r"[\[\(]\s*MEL\s*[\]\)]")
_BDINFO_1080_HEVC_RE = re.compile(
    r"hevc\s+video[^\n]{0,20}?(\d+(?:[.,]\d+)?)\s*(kbps|mbps)[^\n]{0,20}?1080p",
    re.IGNORECASE,
)


def _detect_dv_profile_and_el(
    *,
    has_dv: bool,
    resolution: Optional[str],
    tag_text: str,
    nfo_text: str,
    description: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Detect Dolby Vision profile ('7', '8', '5') and Profile 7 enhancement layer
    ('FEL' vs 'MEL') from explicit tags/NFO/description or BDInfo 1080p EL bitrate.
    Returns (dv_profile, dv_el_type, dv_el_source).
    """
    combined = "\n".join(p for p in (tag_text, nfo_text, description) if p)
    if not combined:
        return None, None, None

    dv_profile: Optional[str] = None
    if _DV_P7_RE.search(combined):
        dv_profile = "7"
    elif _DV_P8_RE.search(combined):
        dv_profile = "8"
    elif _DV_P5_RE.search(combined):
        dv_profile = "5"

    # Strip negated / slash-ambiguous phrases (e.g. "not FEL", "FEL/MEL") before EL matching
    sanitized = _FEL_NEGATED_RE.sub(" ", combined)
    has_fel = bool(_FEL_EXPLICIT_RE.search(sanitized) or _FEL_BRACKET_RE.search(sanitized))
    has_mel = bool(_MEL_EXPLICIT_RE.search(sanitized) or _MEL_BRACKET_RE.search(sanitized))

    if has_fel and not has_mel:
        return (dv_profile or "7"), "FEL", "explicit"
    if has_mel and not has_fel:
        return (dv_profile or "7"), "MEL", "explicit"
    if has_fel and has_mel:
        # Both FEL and MEL mentioned (e.g. disc comparison notes); stay conservative
        return (dv_profile or ("7" if has_dv else None)), None, None

    # BDInfo 1080p HEVC enhancement-layer bitrate heuristic (only for 2160p DV / Profile 7)
    if (has_dv and resolution == "2160p") or dv_profile == "7":
        bd_source = "\n".join(p for p in (nfo_text, description) if p)
        match = _BDINFO_1080_HEVC_RE.search(bd_source)
        if match:
            raw_num = match.group(1).replace(",", "")
            unit = match.group(2).lower()
            try:
                kbps = float(raw_num) * (1000.0 if unit == "mbps" else 1.0)
            except ValueError:
                kbps = 0.0
            if kbps >= 3000.0:
                return "7", "FEL", "bdinfo_bitrate"
            if kbps > 0.0:
                return "7", "MEL", "bdinfo_bitrate"

    return dv_profile, None, None


_EDITION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("directors_cut", re.compile(r"(?<![a-z0-9])(?:director['’]?s?[\s._-]*cut|dir[\s._-]*cut)(?![a-z0-9])", re.IGNORECASE)),
    ("extended", re.compile(r"(?<![a-z0-9])(?:extended(?:[\s._-]*(?:cut|edition|version))?)(?![a-z0-9])", re.IGNORECASE)),
    ("theatrical", re.compile(r"(?<![a-z0-9])(?:theatrical(?:[\s._-]*(?:cut|edition|version))?)(?![a-z0-9])", re.IGNORECASE)),
    ("final_cut", re.compile(r"(?<![a-z0-9])final[\s._-]*cut(?![a-z0-9])", re.IGNORECASE)),
    ("ultimate_cut", re.compile(r"(?<![a-z0-9])ultimate[\s._-]*(?:cut|edition)(?![a-z0-9])", re.IGNORECASE)),
    ("assembly_cut", re.compile(r"(?<![a-z0-9])assembly[\s._-]*cut(?![a-z0-9])", re.IGNORECASE)),
    ("alternate_cut", re.compile(r"(?<![a-z0-9])alternate[\s._-]*(?:cut|edition|ending)(?![a-z0-9])", re.IGNORECASE)),
    ("special_edition", re.compile(r"(?<![a-z0-9])special[\s._-]*edition(?![a-z0-9])", re.IGNORECASE)),
    ("collectors_edition", re.compile(r"(?<![a-z0-9])collector['’]?s?[\s._-]*edition(?![a-z0-9])", re.IGNORECASE)),
    ("anniversary_edition", re.compile(r"(?<![a-z0-9])(?:\d{1,2}(?:th|st|nd|rd)[\s._-]*)?anniversary[\s._-]*edition(?![a-z0-9])", re.IGNORECASE)),
    ("unrated", re.compile(r"(?<![a-z0-9])unrated(?![a-z0-9])", re.IGNORECASE)),
    ("uncut", re.compile(r"(?<![a-z0-9])uncut(?![a-z0-9])", re.IGNORECASE)),
    ("redux", re.compile(r"(?<![a-z0-9])redux(?![a-z0-9])", re.IGNORECASE)),
    ("workprint", re.compile(r"(?<![a-z0-9])workprint(?![a-z0-9])", re.IGNORECASE)),
    ("roadshow", re.compile(r"(?<![a-z0-9])roadshow(?![a-z0-9])", re.IGNORECASE)),
    ("open_matte", re.compile(r"(?<![a-z0-9])open[\s._-]*matte(?![a-z0-9])", re.IGNORECASE)),
    ("imax", re.compile(r"(?<![a-z0-9])imax(?:[\s._-]*enhanced)?(?![a-z0-9])", re.IGNORECASE)),
    ("criterion", re.compile(r"(?<![a-z0-9])criterion(?![a-z0-9])", re.IGNORECASE)),
    ("remastered", re.compile(r"(?<![a-z0-9])remastered(?![a-z0-9])", re.IGNORECASE)),
    ("restored", re.compile(r"(?<![a-z0-9])restor(?:ed|ation)(?![a-z0-9])", re.IGNORECASE)),
)
_DC_TOKEN_RE = re.compile(r"(?<![a-z0-9])dc(?![a-z0-9])", re.IGNORECASE)


def extract_edition_key(raw_text: str) -> str:
    """
    Extract canonical sorted edition/cut markers from the release string's post-year/tag section
    so theatrical, extended, director's cut, unrated, IMAX, Criterion, etc. never collide in cache.
    Scans forward for the release year (skipping adjacent title-embedded years like 'Blade Runner 2049 2017')
    so a later restoration year (e.g. '2004.Extended.2020.Restoration') never erases the cut marker.
    """
    text = _TRAILING_EXT_RE.sub("", (raw_text or "").strip())
    if not text:
        return ""
    # Focus on post-release-year text so title words before the release year are not misread as editions
    tokens = [t for t in re.split(r"[\s._\[\]\(\)\{\}/\\,:;]+", text) if t]
    search_text = text
    for idx in range(1, len(tokens)):
        if YEAR_RE.match(tokens[idx]):
            next_idx = idx + 1
            while next_idx < len(tokens) and YEAR_RE.match(tokens[next_idx]):
                next_idx += 1
            search_text = " ".join(tokens[next_idx:])
            break

    found: list[str] = []
    for label, pattern in _EDITION_PATTERNS:
        if pattern.search(search_text):
            found.append(label)
    if "directors_cut" not in found and search_text != text and _DC_TOKEN_RE.search(search_text):
        found.append("directors_cut")
    return "+".join(sorted(set(found)))


@dataclass(frozen=True)
class ReleaseQuality:
    resolution: Optional[str] = None
    is_remux: bool = False
    dolby_vision: bool = False
    dv_profile: Optional[str] = None
    dv_el_type: Optional[str] = None
    dv_el_source: Optional[str] = None
    hdr: bool = False
    hdr_label: str = "unknown"
    format_label: str = "unknown"
    release_group: Optional[str] = None
    is_favorite_group: bool = False
    edition_key: str = ""
    cache_key: str = ""

    def to_prompt_line(self) -> Optional[str]:
        if not self.cache_key and not self.release_group:
            return None

        if self.resolution == "2160p" and self.dv_profile == "7" and self.dv_el_type == "FEL":
            via = "BDInfo 1080p EL bitrate" if self.dv_el_source == "bdinfo_bitrate" else "NFO/tracker notes"
            tier_note = (
                f"TOP PREFERRED TIER: Remux-2160p + confirmed Dolby Vision Profile 7 FEL ({via}) "
                "— HUGE positive bonus on edge cases"
            )
        elif self.resolution == "2160p" and self.dv_profile == "7" and self.dv_el_type == "MEL":
            tier_note = (
                "preferred 2160p + Dolby Vision Profile 7 MEL (strong DV bonus, below Profile 7 FEL)"
            )
        elif self.resolution == "2160p" and self.dv_profile == "7":
            tier_note = (
                "preferred 2160p + Dolby Vision Profile 7 dual-layer (BL+EL+RPU); NFO does not "
                "specify FEL vs MEL — check via Google Search if this UHD Blu-ray disc is a known P7 FEL transfer"
            )
        elif self.resolution == "2160p" and self.dv_profile == "8":
            tier_note = "preferred 2160p + Dolby Vision Profile 8 (single-layer DV bonus, below Profile 7 FEL)"
        elif self.resolution == "2160p" and self.dolby_vision:
            tier_note = "preferred tier: 2160p + Dolby Vision bonus (profile/FEL unspecified; positive tie-breaker on edge cases)"
        elif self.resolution == "2160p":
            tier_note = "preferred resolution tier: 2160p (strong preference over 1080p on edge cases)"
        elif self.resolution == "1080p" and self.dolby_vision:
            tier_note = "1080p with Dolby Vision (Dolby Vision bonus, though 2160p Remux is preferred)"
        elif self.resolution == "1080p":
            tier_note = "1080p tier (less preferred than Remux-2160p / Dolby Vision on borderline edge cases)"
        else:
            tier_note = "unspecified quality tier"

        parts = [f"Release quality signals: {self.format_label}", f"Dynamic range: {self.hdr_label}"]
        if self.release_group:
            if self.is_favorite_group:
                parts.append(
                    f"Release group: {self.release_group} (FAVORITE RELEASE GROUP — strong positive signal on edge cases)"
                )
            else:
                parts.append(f"Release group: {self.release_group}")
        return " | ".join(parts) + f" ({tier_note})"


def parse_release_quality(
    raw_text: str,
    *,
    resolution: Optional[str] = None,
    source: Optional[str] = None,
    dynamic_range: Optional[list[str]] = None,
    media_processing: Optional[str] = None,
    group: Optional[str] = None,
    nfo_text: Optional[str] = None,
    description: Optional[str] = None,
) -> ReleaseQuality:
    """Deterministically parse release resolution, remux status, Dolby Vision profile/FEL, HDR, and release group."""
    res_candidates = " ".join(p for p in (resolution or "", raw_text or "", source or "") if p)
    detected_res: Optional[str] = None
    if _RES_2160_RE.search(res_candidates):
        detected_res = "2160p"
    elif _RES_1080_RE.search(res_candidates):
        detected_res = "1080p"
    elif _RES_720_RE.search(res_candidates):
        detected_res = "720p"
    elif _RES_UHD_RE.search(res_candidates):
        detected_res = "2160p"

    remux_text = " ".join(p for p in (media_processing or "", source or "", raw_text or "") if p)
    is_remux = bool(_REMUX_RE.search(remux_text))

    dr_joined = " ".join(dynamic_range or [])
    tag_text = " ".join(p for p in (dr_joined, raw_text or "") if p)
    nfo_str = nfo_text or ""
    desc_str = description or ""

    has_dv = bool(_DV_TAG_RE.search(tag_text) or _DV_NFO_RE.search(nfo_str))
    dv_profile, dv_el_type, dv_el_source = _detect_dv_profile_and_el(
        has_dv=has_dv,
        resolution=detected_res,
        tag_text=tag_text,
        nfo_text=nfo_str,
        description=desc_str,
    )
    if dv_profile is not None or dv_el_type is not None:
        has_dv = True

    has_hdr10plus = bool(_HDR10PLUS_RE.search(tag_text) or _HDR10PLUS_RE.search(nfo_str))
    has_hdr = has_hdr10plus or bool(_HDR_RE.search(tag_text) or _HDR_RE.search(nfo_str))

    if has_dv:
        if dv_profile == "7" and dv_el_type == "FEL":
            dv_label = "Dolby Vision Profile 7 FEL"
        elif dv_profile == "7" and dv_el_type == "MEL":
            dv_label = "Dolby Vision Profile 7 MEL"
        elif dv_profile == "7":
            dv_label = "Dolby Vision Profile 7 (FEL/MEL unconfirmed)"
        elif dv_profile == "8":
            dv_label = "Dolby Vision Profile 8"
        elif dv_profile == "5":
            dv_label = "Dolby Vision Profile 5"
        else:
            dv_label = "Dolby Vision"

        if has_hdr10plus:
            hdr_label = f"{dv_label} + HDR10+"
        elif has_hdr:
            hdr_label = f"{dv_label} + HDR"
        else:
            hdr_label = dv_label
    elif has_hdr10plus:
        hdr_label = "HDR10+"
    elif has_hdr:
        hdr_label = "HDR"
    elif detected_res in ("2160p", "1080p", "720p"):
        hdr_label = "SDR"
    else:
        hdr_label = "unknown"

    if is_remux and detected_res:
        format_label = f"Remux-{detected_res}"
    elif is_remux:
        format_label = "Remux"
    elif detected_res:
        format_label = detected_res
    elif source:
        format_label = source.strip()
    else:
        format_label = "unknown"

    release_group = extract_release_group(raw_text, group=group)
    fav_groups = _get_favorite_groups()
    is_favorite_group = bool(release_group and release_group.lower() in fav_groups)

    if not detected_res and not is_remux and not has_dv and not has_hdr and not is_favorite_group:
        cache_key = ""
    else:
        if has_dv:
            if dv_profile == "7" and dv_el_type == "FEL":
                dr_key = "dv_p7_fel"
            elif dv_profile == "7" and dv_el_type == "MEL":
                dr_key = "dv_p7_mel"
            elif dv_profile == "7":
                dr_key = "dv_p7"
            elif dv_profile == "8":
                dr_key = "dv_p8"
            else:
                dr_key = "dv"
        elif has_hdr:
            dr_key = "hdr"
        else:
            dr_key = "sdr"

        parts = [detected_res or "unknown"]
        if is_remux:
            parts.append("remux")
        parts.append(dr_key)
        if is_favorite_group and release_group:
            parts.append(f"fav_{release_group.lower()}")
        cache_key = "_".join(parts)

    edition_key = extract_edition_key(raw_text)

    return ReleaseQuality(
        resolution=detected_res,
        is_remux=is_remux,
        dolby_vision=has_dv,
        dv_profile=dv_profile,
        dv_el_type=dv_el_type,
        dv_el_source=dv_el_source,
        hdr=has_hdr or has_dv,
        hdr_label=hdr_label,
        format_label=format_label,
        release_group=release_group,
        is_favorite_group=is_favorite_group,
        edition_key=edition_key,
        cache_key=cache_key,
    )

RELEASE_STOP_TOKENS = frozenset(
    {
        "2160p",
        "1080p",
        "1080i",
        "720p",
        "576p",
        "480p",
        "4k",
        "uhd",
        "bluray",
        "blu",
        "bdrip",
        "brrip",
        "bdr",
        "remux",
        "web",
        "webdl",
        "webrip",
        "hdtv",
        "dvdrip",
        "dvd",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "av1",
        "xvid",
        "divx",
        "10bit",
        "8bit",
        "hdr",
        "hdr10",
        "hdr10plus",
        "dv",
        "dovi",
        "dolby",
        "atmos",
        "dts",
        "dtshd",
        "truehd",
        "aac",
        "ac3",
        "eac3",
        "ddp5",
        "dd5",
        "dd2",
        "flac",
        "lpcm",
        "nf",
        "amzn",
        "atvp",
        "dsnp",
        "hmax",
        "max",
        "mubi",
        "criterion",
        "cc",
        "proper",
        "repack",
        "rerip",
        "internal",
        "extended",
        "unrated",
        "remastered",
        "restored",
        "imax",
        "hybrid",
        "openmatte",
        "multi",
        "dual",
        "subbed",
        "dubbed",
        "french",
        "italian",
        "japanese",
        "korean",
        "spanish",
        "german",
        "nordic",
        "swedish",
        "danish",
        "polish",
        "russian",
        "cantonese",
        "mandarin",
        "hindi",
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_raw_text(raw_text: str) -> str:
    return re.sub(r"\s+", " ", raw_text.strip().lower())


def extract_imdb_id(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        match = IMDB_ID_RE.search(text)
        if match:
            return match.group(1).lower()
    return None


def normalize_release_key(
    raw_text: str,
    hint_title: Optional[str] = None,
    hint_year: Optional[int] = None,
) -> Optional[str]:
    """
    Build a release-agnostic title+year key so 1080p and 4K releases of the
    same film deduplicate cleanly while remakes from different years stay distinct.
    Returns None when no release year can be established reliably.
    """
    cleaned = raw_text.strip()
    # Strip trailing release group suffix like "-GROUP" if preceded by technical dots/tags
    cleaned = re.sub(r"-[A-Za-z0-9]{2,20}$", "", cleaned)
    tokens = [t for t in re.split(r"[\s._\-\[\]\(\)\{\}/\\,:;]+", cleaned) if t]

    title_tokens: list[str] = []
    detected_year: Optional[int] = None

    for idx, token in enumerate(tokens):
        low = token.lower()
        if YEAR_RE.match(low) and idx > 0:
            detected_year = int(low)
            break
        if low in RELEASE_STOP_TOKENS and title_tokens:
            break
        title_tokens.append(low)

    year = detected_year or hint_year
    if not title_tokens and hint_title:
        title_tokens = [
            t.lower()
            for t in re.split(r"[\s._\-\[\]\(\):;]+", hint_title.strip())
            if t
        ]

    if not title_tokens or year is None:
        return None

    return f"{' '.join(title_tokens)} {year}"


def canonical_release_key(canonical_title: str, year: int) -> str:
    tokens = [
        t.lower()
        for t in re.split(r"[\s._\-\[\]\(\):;]+", canonical_title.strip())
        if t
    ]
    return f"{' '.join(tokens)} {year}"


class TriageDB:
    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        env_path = os.getenv("DB_PATH")
        raw = Path(db_path or env_path or DEFAULT_DB_PATH).expanduser()
        self.db_path = Path(os.path.abspath(str(raw)))

    def _ensure_private_permissions(self) -> None:
        parent = self.db_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            st = parent.stat()
            if st.st_uid == os.getuid() and not (st.st_mode & 0o1000):
                os.chmod(parent, 0o700)
        except OSError:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            try:
                st = os.lstat(str(candidate))
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode):
                raise RuntimeError(f"Refusing to open symlinked database path: {candidate}")
        try:
            fd = os.open(
                str(self.db_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )
            os.close(fd)
        except FileExistsError:
            pass

    def _harden_db_file_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{self.db_path}{suffix}")
            try:
                st = os.lstat(str(target))
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            if st.st_uid == os.getuid() and (st.st_mode & 0o077) != 0:
                try:
                    os.chmod(str(target), 0o600)
                except FileNotFoundError:
                    continue
                except OSError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        self._ensure_private_permissions()
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        self._harden_db_file_permissions()
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_text_normalized TEXT NOT NULL,
                    release_key TEXT,
                    quality_key TEXT NOT NULL DEFAULT '',
                    edition_key TEXT NOT NULL DEFAULT '',
                    evidence_key TEXT NOT NULL DEFAULT '',
                    cache_version INTEGER NOT NULL DEFAULT 1,
                    raw_text_original TEXT NOT NULL,
                    imdb_id TEXT,
                    canonical_title TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    verdict TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_evaluations_raw_text
                    ON evaluations (raw_text_normalized, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_evaluations_release_key
                    ON evaluations (release_key, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_evaluations_imdb_id
                    ON evaluations (imdb_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_evaluations_created_at
                    ON evaluations (created_at DESC);

                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    imdb_id TEXT,
                    tmdb_id INTEGER,
                    title TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('WATCH', 'MAYBE', 'SKIP')),
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_created_at
                    ON feedback (created_at DESC, id DESC);
                """
            )
            eval_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(evaluations)").fetchall()
            }
            if "quality_key" not in eval_cols:
                conn.execute(
                    "ALTER TABLE evaluations ADD COLUMN quality_key TEXT NOT NULL DEFAULT ''"
                )
            if "edition_key" not in eval_cols:
                conn.execute(
                    "ALTER TABLE evaluations ADD COLUMN edition_key TEXT NOT NULL DEFAULT ''"
                )
            if "evidence_key" not in eval_cols:
                conn.execute(
                    "ALTER TABLE evaluations ADD COLUMN evidence_key TEXT NOT NULL DEFAULT ''"
                )
            if "cache_version" not in eval_cols:
                conn.execute(
                    "ALTER TABLE evaluations ADD COLUMN cache_version INTEGER NOT NULL DEFAULT 1"
                )
            fb_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(feedback)").fetchall()
            }
            if "tmdb_id" not in fb_cols:
                conn.execute("ALTER TABLE feedback ADD COLUMN tmdb_id INTEGER")

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evaluations_imdb_quality
                    ON evaluations (imdb_id, quality_key, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evaluations_rel_quality
                    ON evaluations (release_key, quality_key, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evaluations_raw_qual_ed_ev
                    ON evaluations (raw_text_normalized, quality_key, edition_key, evidence_key, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evaluations_imdb_qual_ed_ev
                    ON evaluations (lower(imdb_id), quality_key, edition_key, evidence_key, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evaluations_rel_qual_ed_ev
                    ON evaluations (release_key, quality_key, edition_key, evidence_key, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feedback_imdb_id
                    ON feedback (lower(imdb_id))
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feedback_tmdb_id
                    ON feedback (tmdb_id)
                """
            )
        self._harden_db_file_permissions()

    @staticmethod
    def _validate_cached_payload(raw_json: str) -> Optional[dict[str, Any]]:
        """Revalidate cached JSON against current schema, identity compatibility, and rating sources."""
        from pydantic import ValidationError
        from schemas import EvaluationPayload

        try:
            parsed = json.loads(raw_json)
            validated = EvaluationPayload.model_validate(parsed)
        except (ValueError, TypeError, ValidationError):
            return None

        if (
            validated.catalogue_identification is not None
            and not validated.catalogue_identification.compatible
        ):
            return None

        sources = validated.ratings.sources or {}
        for key in _CACHE_RATING_KEYS:
            if getattr(validated.ratings, key, None) is not None:
                src = sources.get(key)
                if not isinstance(src, str) or not src.strip():
                    return None

        return validated.model_dump()

    def get_cached_evaluation(
        self,
        raw_text: str,
        *,
        hint_title: Optional[str] = None,
        hint_year: Optional[int] = None,
        extra_context: Optional[str] = None,
        quality_key: Optional[str] = None,
        edition_key: Optional[str] = None,
        evidence_key: str = "",
        allow_imdb_lookup: bool = True,
        allow_release_key_fallback: bool = True,
        ttl_hours: int = 72,
    ) -> Optional[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds"
        )
        norm_raw = normalize_raw_text(raw_text)
        imdb_id = extract_imdb_id(raw_text, extra_context) if allow_imdb_lookup else None
        rel_key = normalize_release_key(raw_text, hint_title=hint_title, hint_year=hint_year)
        query_year: Optional[int] = hint_year
        if query_year is None and rel_key:
            tail = rel_key.rsplit(" ", 1)[-1]
            if YEAR_RE.match(tail):
                query_year = int(tail)
        parsed_q = parse_release_quality(raw_text) if (quality_key is None or edition_key is None) else None
        q_key = quality_key if quality_key is not None else parsed_q.cache_key  # type: ignore[union-attr]
        ed_key = edition_key if edition_key is not None else parsed_q.edition_key  # type: ignore[union-attr]
        ev_key = (evidence_key or "").strip()

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT response_json, year
                FROM evaluations
                WHERE raw_text_normalized = ?
                  AND quality_key = ?
                  AND edition_key = ?
                  AND evidence_key = ?
                  AND cache_version = ?
                  AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (norm_raw, q_key, ed_key, ev_key, EVALUATION_CACHE_VERSION, cutoff),
            ).fetchone()

            if row is None and imdb_id:
                candidate = conn.execute(
                    """
                    SELECT response_json, year
                    FROM evaluations
                    WHERE lower(imdb_id) = ?
                      AND quality_key = ?
                      AND edition_key = ?
                      AND evidence_key = ?
                      AND cache_version = ?
                      AND created_at >= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (imdb_id.lower(), q_key, ed_key, ev_key, EVALUATION_CACHE_VERSION, cutoff),
                ).fetchone()
                if candidate is not None:
                    cached_year = int(candidate["year"])
                    if query_year is None or abs(cached_year - query_year) <= 1:
                        row = candidate

            if row is None and allow_release_key_fallback and rel_key:
                row = conn.execute(
                    """
                    SELECT response_json, year
                    FROM evaluations
                    WHERE release_key = ?
                      AND quality_key = ?
                      AND edition_key = ?
                      AND evidence_key = ?
                      AND cache_version = ?
                      AND created_at >= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (rel_key, q_key, ed_key, ev_key, EVALUATION_CACHE_VERSION, cutoff),
                ).fetchone()

        if row is None:
            return None

        return self._validate_cached_payload(str(row["response_json"]))

    def save_evaluation(
        self,
        raw_text: str,
        payload: dict[str, Any],
        *,
        hint_title: Optional[str] = None,
        hint_year: Optional[int] = None,
        quality_key: Optional[str] = None,
        edition_key: Optional[str] = None,
        evidence_key: str = "",
        created_at: Optional[str] = None,
    ) -> None:
        identification = payload["identification"]
        decision_aid = payload["decision_aid"]
        norm_raw = normalize_raw_text(raw_text)
        imdb_id = identification.get("imdb_id")
        if imdb_id:
            imdb_id = imdb_id.strip().lower()
        canonical_title = str(identification["canonical_title"])
        year = int(identification["year"])
        verdict = str(decision_aid["verdict"])
        rel_key = (
            normalize_release_key(raw_text, hint_title=hint_title, hint_year=hint_year)
            or canonical_release_key(canonical_title, year)
        )
        parsed_q = parse_release_quality(raw_text) if (quality_key is None or edition_key is None) else None
        q_key = quality_key if quality_key is not None else parsed_q.cache_key  # type: ignore[union-attr]
        ed_key = edition_key if edition_key is not None else parsed_q.edition_key  # type: ignore[union-attr]
        ev_key = (evidence_key or "").strip()
        ts = created_at or utc_now_iso()
        serialized = json.dumps(payload, ensure_ascii=False)

        with self._connect() as conn:
            # Replace superseded entries for the same movie, quality tier, edition, AND evidence digest
            conn.execute(
                "DELETE FROM evaluations WHERE raw_text_normalized = ? AND quality_key = ? AND edition_key = ? AND evidence_key = ?",
                (norm_raw, q_key, ed_key, ev_key),
            )
            if imdb_id:
                conn.execute(
                    "DELETE FROM evaluations WHERE lower(imdb_id) = ? AND quality_key = ? AND edition_key = ? AND evidence_key = ?",
                    (imdb_id, q_key, ed_key, ev_key),
                )
            elif rel_key:
                conn.execute(
                    "DELETE FROM evaluations WHERE release_key = ? AND quality_key = ? AND edition_key = ? AND evidence_key = ?",
                    (rel_key, q_key, ed_key, ev_key),
                )

            conn.execute(
                """
                INSERT INTO evaluations (
                    raw_text_normalized,
                    release_key,
                    quality_key,
                    edition_key,
                    evidence_key,
                    cache_version,
                    raw_text_original,
                    imdb_id,
                    canonical_title,
                    year,
                    verdict,
                    response_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm_raw,
                    rel_key,
                    q_key,
                    ed_key,
                    ev_key,
                    EVALUATION_CACHE_VERSION,
                    raw_text,
                    imdb_id,
                    canonical_title,
                    year,
                    verdict,
                    serialized,
                    ts,
                ),
            )
        self._harden_db_file_permissions()

    def get_cache_entries_count(self, ttl_hours: int = 72) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM evaluations WHERE cache_version = ? AND created_at >= ?",
                (EVALUATION_CACHE_VERSION, cutoff),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def get_recent_evaluations(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT canonical_title, year, verdict, imdb_id, created_at
                FROM evaluations
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "title": row["canonical_title"],
                "year": int(row["year"]),
                "verdict": row["verdict"],
                "imdb_id": row["imdb_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def add_feedback(
        self,
        *,
        imdb_id: Optional[str],
        tmdb_id: Optional[int] = None,
        title: str,
        decision: str,
        note: Optional[str],
        created_at: Optional[str] = None,
    ) -> int:
        ts = created_at or utc_now_iso()
        clean_imdb = imdb_id.strip().lower() if imdb_id and imdb_id.strip() else None
        clean_tmdb = (
            int(tmdb_id)
            if isinstance(tmdb_id, int) and not isinstance(tmdb_id, bool) and tmdb_id > 0
            else None
        )
        clean_title = title.strip()
        clean_decision = decision.strip().upper()
        clean_note = note.strip() if note and note.strip() else None
        with self._connect() as conn:
            # Preserve existing verified ID links across partial updates (TMDb-only or IMDb-only)
            # so updating via one ID never drops the other verified ID and duplicates feedback rows.
            if clean_imdb and clean_tmdb is None:
                linked_tmdb = conn.execute(
                    """
                    SELECT DISTINCT tmdb_id FROM feedback
                    WHERE imdb_id IS NOT NULL AND lower(imdb_id) = ? AND tmdb_id IS NOT NULL
                    ORDER BY id DESC
                    """,
                    (clean_imdb,),
                ).fetchall()
                if len(linked_tmdb) == 1:
                    clean_tmdb = int(linked_tmdb[0]["tmdb_id"])
            elif clean_tmdb is not None and clean_imdb is None:
                linked_imdb = conn.execute(
                    """
                    SELECT DISTINCT lower(imdb_id) AS imdb_id FROM feedback
                    WHERE tmdb_id = ? AND imdb_id IS NOT NULL AND trim(imdb_id) != ''
                    ORDER BY id DESC
                    """,
                    (clean_tmdb,),
                ).fetchall()
                if len(linked_imdb) == 1:
                    clean_imdb = str(linked_imdb[0]["imdb_id"])

            if clean_imdb and clean_tmdb is not None:
                conn.execute(
                    """
                    DELETE FROM feedback
                    WHERE (imdb_id IS NOT NULL AND lower(imdb_id) = ?)
                       OR (tmdb_id = ? AND (imdb_id IS NULL OR lower(imdb_id) = ?))
                    """,
                    (clean_imdb, clean_tmdb, clean_imdb),
                )
            elif clean_imdb:
                conn.execute(
                    "DELETE FROM feedback WHERE imdb_id IS NOT NULL AND lower(imdb_id) = ?",
                    (clean_imdb,),
                )
            elif clean_tmdb is not None:
                conn.execute(
                    "DELETE FROM feedback WHERE tmdb_id = ?",
                    (clean_tmdb,),
                )
            else:
                conn.execute(
                    "DELETE FROM feedback WHERE imdb_id IS NULL AND tmdb_id IS NULL AND lower(trim(title)) = ?",
                    (clean_title.lower(),),
                )
            conn.execute(
                """
                INSERT INTO feedback (imdb_id, tmdb_id, title, decision, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (clean_imdb, clean_tmdb, clean_title, clean_decision, clean_note, ts),
            )
            row = conn.execute("SELECT COUNT(*) AS cnt FROM feedback").fetchone()
            return int(row["cnt"]) if row else 0

    def get_recent_feedback(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, imdb_id, tmdb_id, title, decision, note, created_at
                FROM feedback
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]
