#!/usr/bin/env bash
set -euo pipefail

# Ultra.cc Non-Root Movie Triage FastAPI Installer & Live Smoke Test
# Deploys to ~/apps/movie-triage-api with userland systemd service bound strictly to 127.0.0.1

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Error: Do not run this script as root or with sudo on Ultra.cc." >&2
  exit 1
fi

APP_DIR="$HOME/apps/movie-triage-api"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="movie-triage-api.service"

echo "[1/6] Finding an unassigned Ultra.cc port via app-ports free..."
if [[ -n "${PORT:-}" ]]; then
  CHOSEN_PORT="$PORT"
else
  if ! command -v app-ports >/dev/null 2>&1; then
    echo "Error: 'app-ports' command not found. Run this on your Ultra.cc seedbox or pass PORT=<port>." >&2
    exit 1
  fi
  CHOSEN_PORT="$(app-ports free | grep -Eo '[0-9]{4,5}' | head -n 1 || true)"
fi

if [[ -z "$CHOSEN_PORT" ]]; then
  echo "Error: Could not parse a free port from 'app-ports free'." >&2
  exit 1
fi
echo "Selected local port: $CHOSEN_PORT"

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  read -r -s -p "Enter your GEMINI_API_KEY (input hidden): " GEMINI_API_KEY
  echo ""
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "Error: GEMINI_API_KEY is required." >&2
  exit 1
fi

echo "[2/6] Creating project directory and writing microservice files..."
mkdir -p "$APP_DIR/data" "$SYSTEMD_DIR"

cat <<'EOF_REQUIREMENTS' > "$APP_DIR/requirements.txt"
fastapi
uvicorn[standard]
google-genai
pydantic
python-dotenv
EOF_REQUIREMENTS

cat <<'EOF_TASTE_PROFILE' > "$APP_DIR/taste_profile.txt"
No strong genre bias yet. Prioritize films with strong craft, originality, or notable festival/critical recognition. Flag derivative blockbusters, slow pacing with weak payoff, or ratings inflated by very low vote counts.

Release quality preference (soft edge-case factor, not a hard rule):
- Incoming releases are normally Remux-2160p or Remux-1080p.
- Strongly prefer Remux-2160p over Remux-1080p, and treat Dolby Vision (DV / DoVi) as a notable visual plus.
- Dolby Vision Profile 7 FEL (Full Enhancement Layer) is a HUGE positive bonus — rank preference as: Remux-2160p DV Profile 7 FEL >> Remux-2160p DV (P7 MEL / P8 / unspecified) > Remux-2160p HDR/SDR > Remux-1080p.
- Favorite release group: CiNEPHiLES. Treat releases from CiNEPHiLES as a meaningful positive signal (both for technical/remux quality and curated film selection), especially on borderline/edge-case titles.
- Never turn a clearly weak or derivative movie into WATCH just because it is 2160p DV P7 FEL or from a favorite group, and never downgrade a clearly great movie to SKIP just because it is 1080p SDR.
- Let release quality (especially 2160p DV Profile 7 FEL) and favorite release group tip the scales on borderline/edge-case films, and briefly mention confirmed P7 FEL in verdict_reason when it factors into the decision.
EOF_TASTE_PROFILE

cat <<'EOF_SCHEMAS' > "$APP_DIR/schemas.py"
from __future__ import annotations

import re
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


MatchConfidence = Literal["high", "medium", "low"]
Verdict = Literal["WATCH", "MAYBE", "SKIP"]
FeedbackDecision = Literal["WATCH", "SKIP"]

_IMDB_ID_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)


class TorrentInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    indexer: Optional[str] = Field(default=None, description="Tracker/indexer name (e.g. TorrentLeech)")
    category: Optional[str] = Field(default=None, description="Tracker category (e.g. Movies :: 4K)")
    imdb_id: Optional[str] = Field(default=None, description="Tracker-supplied IMDb ID (treated as unverified hint)")
    tmdb_id: Optional[int] = Field(default=None, ge=1, description="Tracker-supplied TMDB ID (treated as unverified hint)")
    runtime_minutes: Optional[int] = Field(
        default=None, ge=1, le=1440, description="Actual file duration in minutes (e.g. from MediaInfo/NFO)"
    )
    audio_languages: list[str] = Field(
        default_factory=list, description="Audio track language(s) from MediaInfo/NFO"
    )
    resolution: Optional[str] = Field(default=None, description="Release resolution (e.g. 2160p, 1080p)")
    source: Optional[str] = Field(default=None, description="Release source (e.g. UHD BluRay REMUX, WEB-DL)")
    dynamic_range: list[str] = Field(
        default_factory=list,
        description="Dynamic range / HDR tags from tracker or MediaInfo (e.g. ['DV', 'HDR10'])",
    )
    media_processing: Optional[str] = Field(
        default=None, description="Release processing tag (e.g. REMUX)"
    )
    group: Optional[str] = Field(
        default=None, description="Release group name (e.g. CiNEPHiLES)"
    )
    size_gb: Optional[float] = Field(default=None, ge=0, description="Release size in GB")
    freeleech: Optional[bool] = Field(default=None, description="Whether release is freeleech")
    description: Optional[str] = Field(
        default=None, description="Sanitized tracker listing description/summary"
    )
    nfo_text: Optional[str] = Field(
        default=None, description="Sanitized MediaInfo / NFO text from tracker listing"
    )

    @field_validator("imdb_id", mode="before")
    @classmethod
    def _clean_imdb_id(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().lower()
            if _IMDB_ID_RE.fullmatch(text):
                return text
        return None

    @field_validator("audio_languages", "dynamic_range", mode="before")
    @classmethod
    def _clean_string_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            parts = [p.strip() for p in re.split(r"[,;/]+", value) if p.strip()]
            return parts[:6]
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()][:6]
        return []


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw_text: str = Field(..., min_length=1, description="Messy release string or movie title")
    hint_title: Optional[str] = Field(default=None, description="Optional title hint")
    hint_year: Optional[int] = Field(default=None, ge=1888, le=2100, description="Optional release year hint")
    extra_context: Optional[str] = Field(default=None, description="Optional tracker, IMDb ID, or release context")
    torrent_info: Optional[TorrentInfo] = Field(
        default=None,
        description="Optional tracker listing metadata, description, and MediaInfo/NFO (cross-checked for mismatches)",
    )
    force_refresh: bool = Field(default=False, description="Bypass SQLite cache and query Gemini fresh")


class Identification(BaseModel):
    canonical_title: str = Field(..., description="Primary English or widely recognized title")
    original_title: Optional[str] = Field(default=None, description="Original language title if different")
    year: int = Field(..., description="Theatrical or festival release year")
    director: str = Field(..., description="Director full name(s)")
    imdb_id: Optional[str] = Field(default=None, description="Verified IMDb ID in ttNNNNNNN format or null")
    tmdb_id: Optional[int] = Field(default=None, description="Verified TMDB movie ID or null if unverified")
    runtime_minutes: Optional[int] = Field(default=None, description="Runtime in minutes or null if unverified")
    genres: list[str] = Field(default_factory=list, description="Up to 4 concise genre labels")
    match_confidence: MatchConfidence = Field(..., description="Confidence in exact movie identification")
    disambiguation_note: Optional[str] = Field(
        default=None,
        description="Brief note when disambiguating remakes, foreign titles, or ambiguous strings",
    )


class Ratings(BaseModel):
    rotten_tomatoes_critics: Optional[int] = Field(
        default=None, description="Tomatometer critics percentage (0-100) or null if unverified"
    )
    rotten_tomatoes_audience: Optional[int] = Field(
        default=None, description="Popcornmeter audience percentage (0-100) or null if unverified"
    )
    metacritic: Optional[int] = Field(
        default=None, description="Metascore (0-100) or null if unverified"
    )
    imdb: Optional[float] = Field(
        default=None, description="IMDb rating out of 10 (e.g. 7.4) or null if unverified"
    )
    letterboxd: Optional[float] = Field(
        default=None, description="Letterboxd rating out of 5 (e.g. 3.9) or null if unverified"
    )
    missing_sources: list[str] = Field(
        default_factory=list,
        description="List of rating platforms whose scores could not be verified and were set to null",
    )


class Recognition(BaseModel):
    awards_highlights: list[str] = Field(
        default_factory=list,
        description="Concise major festival selections, wins, or nominations (max 4)",
    )
    critic_consensus: str = Field(
        ...,
        description="1-2 crisp sentences summarizing critical reception",
    )


class DecisionAid(BaseModel):
    one_line_pitch: str = Field(..., description="Single punchy sentence capturing premise and craft")
    similar_to: list[str] = Field(
        default_factory=list,
        description="2-4 comparable films for quick mental anchoring",
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description="Concise caveats (pacing, low vote count, derivative tropes, polarizing ending)",
    )
    verdict: Verdict = Field(..., description="Triage decision tailored to taste profile and feedback history")
    verdict_reason: str = Field(..., description="1-2 short sentences justifying the verdict")


class EvaluationPayload(BaseModel):
    identification: Identification
    ratings: Ratings
    recognition: Recognition
    decision_aid: DecisionAid


class EvaluateResponse(EvaluationPayload):
    cached: bool = Field(..., description="True when served from SQLite cache within TTL")


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    imdb_id: Optional[str] = Field(default=None, description="IMDb ID if known (e.g. tt14561712)")
    tmdb_id: Optional[int] = Field(default=None, description="Optional TMDB movie ID")
    title: str = Field(..., min_length=1, description="Movie title")
    decision: FeedbackDecision = Field(..., description="User decision: watch or skip (case-insensitive)")
    note: Optional[str] = Field(default=None, description="Optional personal note explaining why")

    @field_validator("decision", mode="before")
    @classmethod
    def _normalize_decision(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value


class FeedbackResponse(BaseModel):
    status: Literal["saved"] = "saved"
    decision: FeedbackDecision
    total_feedback: int


class FeedbackItem(BaseModel):
    id: int
    imdb_id: Optional[str] = None
    title: str
    decision: FeedbackDecision
    note: Optional[str] = None
    created_at: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    model: str
    port: int
    cache_entries: int
    tmdb_enabled: bool = False


class RecentEvaluationItem(BaseModel):
    title: str
    year: int
    verdict: Verdict
    imdb_id: Optional[str] = None
    created_at: str
EOF_SCHEMAS

cat <<'EOF_DB' > "$APP_DIR/db.py"
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "triage.db"

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
        resolved = Path(db_path or env_path or DEFAULT_DB_PATH).expanduser().resolve()
        self.db_path = resolved

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
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
                    title TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('WATCH', 'MAYBE', 'SKIP')),
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_created_at
                    ON feedback (created_at DESC, id DESC);
                """
            )
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(evaluations)").fetchall()
            }
            if "quality_key" not in cols:
                conn.execute(
                    "ALTER TABLE evaluations ADD COLUMN quality_key TEXT NOT NULL DEFAULT ''"
                )
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

    def get_cached_evaluation(
        self,
        raw_text: str,
        *,
        hint_title: Optional[str] = None,
        hint_year: Optional[int] = None,
        extra_context: Optional[str] = None,
        quality_key: Optional[str] = None,
        ttl_hours: int = 72,
    ) -> Optional[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds"
        )
        norm_raw = normalize_raw_text(raw_text)
        imdb_id = extract_imdb_id(raw_text, extra_context)
        rel_key = normalize_release_key(raw_text, hint_title=hint_title, hint_year=hint_year)
        q_key = (
            quality_key
            if quality_key is not None
            else parse_release_quality(raw_text).cache_key
        )

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT response_json
                FROM evaluations
                WHERE raw_text_normalized = ? AND quality_key = ? AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (norm_raw, q_key, cutoff),
            ).fetchone()

            if row is None and imdb_id:
                row = conn.execute(
                    """
                    SELECT response_json
                    FROM evaluations
                    WHERE lower(imdb_id) = ? AND quality_key = ? AND created_at >= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (imdb_id.lower(), q_key, cutoff),
                ).fetchone()

            if row is None and rel_key:
                row = conn.execute(
                    """
                    SELECT response_json
                    FROM evaluations
                    WHERE release_key = ? AND quality_key = ? AND created_at >= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (rel_key, q_key, cutoff),
                ).fetchone()

        if row is None:
            return None

        return json.loads(row["response_json"])

    def save_evaluation(
        self,
        raw_text: str,
        payload: dict[str, Any],
        *,
        hint_title: Optional[str] = None,
        hint_year: Optional[int] = None,
        quality_key: Optional[str] = None,
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
        q_key = (
            quality_key
            if quality_key is not None
            else parse_release_quality(raw_text).cache_key
        )
        ts = created_at or utc_now_iso()
        serialized = json.dumps(payload, ensure_ascii=False)

        with self._connect() as conn:
            # Replace superseded entries for the same movie AND release quality tier
            conn.execute(
                "DELETE FROM evaluations WHERE raw_text_normalized = ? AND quality_key = ?",
                (norm_raw, q_key),
            )
            if imdb_id:
                conn.execute(
                    "DELETE FROM evaluations WHERE lower(imdb_id) = ? AND quality_key = ?",
                    (imdb_id, q_key),
                )
            elif rel_key:
                conn.execute(
                    "DELETE FROM evaluations WHERE release_key = ? AND quality_key = ?",
                    (rel_key, q_key),
                )

            conn.execute(
                """
                INSERT INTO evaluations (
                    raw_text_normalized,
                    release_key,
                    quality_key,
                    raw_text_original,
                    imdb_id,
                    canonical_title,
                    year,
                    verdict,
                    response_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm_raw,
                    rel_key,
                    q_key,
                    raw_text,
                    imdb_id,
                    canonical_title,
                    year,
                    verdict,
                    serialized,
                    ts,
                ),
            )

    def get_cache_entries_count(self, ttl_hours: int = 72) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds"
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM evaluations WHERE created_at >= ?",
                (cutoff,),
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
        title: str,
        decision: str,
        note: Optional[str],
        created_at: Optional[str] = None,
    ) -> int:
        ts = created_at or utc_now_iso()
        clean_imdb = imdb_id.strip().lower() if imdb_id and imdb_id.strip() else None
        clean_title = title.strip()
        clean_decision = decision.strip().upper()
        clean_note = note.strip() if note and note.strip() else None
        with self._connect() as conn:
            if clean_imdb:
                conn.execute(
                    "DELETE FROM feedback WHERE lower(imdb_id) = ?",
                    (clean_imdb,),
                )
            else:
                conn.execute(
                    "DELETE FROM feedback WHERE imdb_id IS NULL AND lower(trim(title)) = ?",
                    (clean_title.lower(),),
                )
            conn.execute(
                """
                INSERT INTO feedback (imdb_id, title, decision, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (clean_imdb, clean_title, clean_decision, clean_note, ts),
            )
            row = conn.execute("SELECT COUNT(*) AS cnt FROM feedback").fetchone()
            return int(row["cnt"]) if row else 0

    def get_recent_feedback(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, imdb_id, title, decision, note, created_at
                FROM feedback
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]
EOF_DB

cat <<'EOF_TMDB_CLIENT' > "$APP_DIR/tmdb_client.py"
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from db import RELEASE_STOP_TOKENS, YEAR_RE, extract_imdb_id
from schemas import EvaluateRequest, EvaluationPayload, Identification, TorrentInfo

logger = logging.getLogger(__name__)

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_TIMEOUT_SECONDS = 6.0

_TRACKER_SECRET_RE = re.compile(
    r"https?://\S+|magnet:\S+|www\.\S+"
    r"|\b(?:passkey|token|password|authorization|cookie|bearer)\b\s*[:=]?\s*\S+"
    r"|[A-Za-z0-9_-]{48,}",
    re.IGNORECASE,
)
_NFO_DURATION_HM_RE = re.compile(r"\bDuration\s*:\s*(\d{1,2})\s*h\s*(\d{1,2})\s*min\b", re.IGNORECASE)
_NFO_DURATION_M_RE = re.compile(r"\bDuration\s*:\s*(\d{1,4})\s*min\b", re.IGNORECASE)

_LANGUAGE_TO_ISO: dict[str, set[str]] = {
    "english": {"en"},
    "en": {"en"},
    "french": {"fr"},
    "fr": {"fr"},
    "italian": {"it"},
    "it": {"it"},
    "spanish": {"es"},
    "es": {"es"},
    "german": {"de"},
    "de": {"de"},
    "japanese": {"ja"},
    "ja": {"ja"},
    "korean": {"ko"},
    "ko": {"ko"},
    "chinese": {"zh", "cn"},
    "cantonese": {"zh", "cn"},
    "mandarin": {"zh", "cn"},
    "zh": {"zh", "cn"},
    "cn": {"zh", "cn"},
    "russian": {"ru"},
    "ru": {"ru"},
    "polish": {"pl"},
    "pl": {"pl"},
    "portuguese": {"pt"},
    "pt": {"pt"},
    "swedish": {"sv"},
    "sv": {"sv"},
    "danish": {"da"},
    "da": {"da"},
    "norwegian": {"no", "nb"},
    "finnish": {"fi"},
    "dutch": {"nl"},
    "hindi": {"hi"},
    "persian": {"fa"},
    "farsi": {"fa"},
    "arabic": {"ar"},
    "turkish": {"tr"},
    "greek": {"el"},
    "czech": {"cs"},
    "hungarian": {"hu"},
    "romanian": {"ro"},
    "thai": {"th"},
}


def sanitize_tracker_text(raw: Optional[str], *, max_chars: int = 900) -> str:
    """Scrub URLs, passkeys, cookies, and control chars from tracker text before LLM use."""
    if not raw:
        return ""
    scrubbed = _TRACKER_SECRET_RE.sub("[redacted]", raw)
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in scrubbed)
    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    if len(collapsed) > max_chars:
        return collapsed[: max_chars - 3].rstrip() + "..."
    return collapsed


def extract_nfo_runtime_minutes(nfo_text: Optional[str]) -> Optional[int]:
    """Extract video duration in minutes from MediaInfo/NFO text when unambiguous."""
    if not nfo_text:
        return None
    found: set[int] = set()
    for hours, mins in _NFO_DURATION_HM_RE.findall(nfo_text):
        h_val, m_val = int(hours), int(mins)
        if m_val < 60:
            total = h_val * 60 + m_val
            if 1 <= total <= 1440:
                found.add(total)
    if not found:
        for mins in _NFO_DURATION_M_RE.findall(nfo_text):
            total = int(mins)
            if 1 <= total <= 1440:
                found.add(total)
    if len(found) == 1:
        return next(iter(found))
    return None


def _audio_languages_match_iso(audio_languages: list[str], iso_lang: Optional[str]) -> bool:
    if not audio_languages or not iso_lang:
        return False
    target = iso_lang.strip().lower()
    for lang in audio_languages:
        codes = _LANGUAGE_TO_ISO.get(lang.strip().lower())
        if codes and target in codes:
            return True
    return False


def _has_non_latin_letters(text: Optional[str]) -> bool:
    if not text:
        return False
    for ch in text:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            if "LATIN" not in name:
                return True
    return False


def _normalize_title_for_compare(text: str) -> str:
    """Normalize a movie title for accent/punctuation-insensitive comparison."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    alnum_spaced = re.sub(r"[^0-9a-zA-Z]+", " ", stripped).strip().lower()
    return re.sub(r"\s+", " ", alnum_spaced)


def _token_jaccard(a: str, b: str) -> float:
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _parse_year(release_date: Optional[str]) -> Optional[int]:
    if not release_date or len(release_date) < 4:
        return None
    head = release_date[:4]
    if YEAR_RE.match(head):
        return int(head)
    return None


def extract_tmdb_query(
    raw_text: str,
    *,
    hint_title: Optional[str] = None,
    hint_year: Optional[int] = None,
) -> tuple[Optional[str], Optional[int]]:
    """
    Extract a clean (title, year) search query from a release string and optional hints.
    Preserves hyphenated titles (e.g. Spider-Man) while stripping trailing release groups
    when release tags or dots are present.
    """
    cleaned = raw_text.strip()
    has_release_markers = "." in cleaned or any(
        tok.lower() in RELEASE_STOP_TOKENS or YEAR_RE.match(tok)
        for tok in re.split(r"[\s._\-\[\]\(\)\{\}/\\,:;]+", cleaned)
        if tok
    )
    if has_release_markers:
        cleaned = re.sub(r"-[A-Za-z0-9]{2,20}$", "", cleaned)

    tokens = [t for t in re.split(r"[\s._\[\]\(\)\{\}/\\,:;]+", cleaned) if t]

    title_tokens: list[str] = []
    detected_year: Optional[int] = None

    for idx, token in enumerate(tokens):
        low = token.lower()
        if YEAR_RE.match(low) and idx > 0:
            detected_year = int(low)
            break
        if low in RELEASE_STOP_TOKENS and title_tokens:
            break
        if low.startswith("tt") and re.fullmatch(r"tt\d{7,10}", low):
            continue
        title_tokens.append(token)

    query_title = hint_title.strip() if hint_title and hint_title.strip() else " ".join(title_tokens).strip()
    query_year = hint_year if hint_year is not None else detected_year
    return (query_title or None), query_year


@dataclass(frozen=True)
class TmdbMovie:
    tmdb_id: int
    canonical_title: str
    original_title: Optional[str] = None
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    runtime_minutes: Optional[int] = None
    director: Optional[str] = None
    genres: list[str] = field(default_factory=list)
    original_language: Optional[str] = None

    def to_prompt_block(self) -> str:
        lines = [
            "Verified TMDB candidate metadata (use to anchor exact movie identity unless the release string clearly refers to a different film):",
            f"- TMDB ID: {self.tmdb_id}",
            f"- Canonical title: {self.canonical_title}",
        ]
        if self.original_title and self.original_title != self.canonical_title:
            lines.append(f"- Original title: {self.original_title}")
        if self.year is not None:
            lines.append(f"- Release year: {self.year}")
        if self.director:
            lines.append(f"- Director: {self.director}")
        if self.imdb_id:
            lines.append(f"- IMDb ID: {self.imdb_id}")
        if self.runtime_minutes:
            lines.append(f"- Runtime: {self.runtime_minutes} minutes")
        if self.genres:
            lines.append(f"- Genres: {', '.join(self.genres[:4])}")
        return "\n".join(lines)


def select_best_tmdb_candidate(
    results: list[dict[str, Any]],
    query_title: str,
    query_year: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    Score TMDB search results and return the top candidate only when confidence is high
    enough to avoid false-positive overrides (especially across remakes/different years).
    """
    norm_query = _normalize_title_for_compare(query_title)
    if not norm_query:
        return None

    best_score = -1.0
    best_item: Optional[dict[str, Any]] = None

    for item in results[:10]:
        title = str(item.get("title") or "")
        orig_title = str(item.get("original_title") or "")
        norm_title = _normalize_title_for_compare(title)
        norm_orig = _normalize_title_for_compare(orig_title)

        if norm_query in (norm_title, norm_orig):
            title_score = 100.0
        else:
            sim = max(
                _token_jaccard(norm_query, norm_title),
                _token_jaccard(norm_query, norm_orig),
            )
            if sim < 0.6:
                continue
            title_score = 65.0 * sim

        cand_year = _parse_year(item.get("release_date"))
        year_score = 0.0
        if query_year is not None:
            if cand_year == query_year:
                year_score = 50.0
            elif cand_year is not None and abs(cand_year - query_year) == 1:
                year_score = 25.0
            elif cand_year is not None:
                year_score = -80.0

        popularity = min(float(item.get("popularity") or 0.0), 30.0) * 0.5
        total = title_score + year_score + popularity
        if total > best_score:
            best_score = total
            best_item = item

    min_threshold = 75.0 if query_year is not None else 100.0
    if best_item is None or best_score < min_threshold:
        return None
    return best_item


def _directors_overlap(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    tokens_a = {t for t in _normalize_title_for_compare(a).split() if len(t) >= 3}
    tokens_b = {t for t in _normalize_title_for_compare(b).split() if len(t) >= 3}
    return bool(tokens_a & tokens_b)


def is_tmdb_compatible(ident: Identification, tmdb: TmdbMovie) -> bool:
    """Ensure TMDB metadata refers to the same film as Gemini's identification."""
    if ident.imdb_id and tmdb.imdb_id:
        return ident.imdb_id.strip().lower() == tmdb.imdb_id.strip().lower()

    if tmdb.year is not None and abs(ident.year - tmdb.year) > 1:
        return False

    ident_titles = [
        _normalize_title_for_compare(t)
        for t in (ident.canonical_title, ident.original_title or "")
        if t and t.strip()
    ]
    tmdb_titles = [
        _normalize_title_for_compare(t)
        for t in (tmdb.canonical_title, tmdb.original_title or "")
        if t and t.strip()
    ]

    title_match = any(
        it == tt or _token_jaccard(it, tt) >= 0.6
        for it in ident_titles
        for tt in tmdb_titles
    )
    director_match = _directors_overlap(ident.director, tmdb.director)
    return title_match or director_match


def apply_tmdb_verification(
    payload: EvaluationPayload,
    tmdb: Optional[TmdbMovie],
) -> EvaluationPayload:
    """
    Deterministically fill and verify movie IDs, runtime, original title, director,
    and genres using TMDB metadata when compatible with Gemini's identification.
    """
    if tmdb is None:
        return payload

    ident = payload.identification
    if not is_tmdb_compatible(ident, tmdb):
        logger.warning(
            "Skipping TMDB merge due to mismatch: gemini=(%r, %s, %s) vs tmdb=(%r, %s, %s)",
            ident.canonical_title,
            ident.year,
            ident.imdb_id,
            tmdb.canonical_title,
            tmdb.year,
            tmdb.imdb_id,
        )
        return payload

    verified_imdb = tmdb.imdb_id or (ident.imdb_id.strip().lower() if ident.imdb_id else None)
    verified_runtime = tmdb.runtime_minutes if tmdb.runtime_minutes else ident.runtime_minutes

    verified_original_title = ident.original_title
    if tmdb.original_title:
        if tmdb.original_title.casefold() != ident.canonical_title.casefold():
            verified_original_title = tmdb.original_title
        elif tmdb.original_title != ident.canonical_title:
            verified_original_title = tmdb.original_title

    verified_director = ident.director
    if tmdb.director and (not ident.director.strip() or _directors_overlap(ident.director, tmdb.director)):
        verified_director = tmdb.director

    verified_genres = ident.genres if ident.genres else tmdb.genres[:4]

    updated_ident = ident.model_copy(
        update={
            "tmdb_id": tmdb.tmdb_id,
            "imdb_id": verified_imdb,
            "runtime_minutes": verified_runtime,
            "original_title": verified_original_title,
            "director": verified_director,
            "genres": verified_genres,
        }
    )
    return payload.model_copy(update={"identification": updated_ident})


@dataclass(frozen=True)
class TmdbLookupResult:
    movie: Optional[TmdbMovie] = None
    tracker_conflict: Optional[str] = None
    trusted_for_cache: bool = False


def check_tracker_movie_consistency(
    movie: TmdbMovie,
    *,
    query_title: Optional[str],
    query_year: Optional[int],
    nfo_runtime: Optional[int] = None,
    audio_languages: Optional[list[str]] = None,
) -> Optional[str]:
    """
    Cross-check a tracker-supplied TMDB/IMDb candidate against the release string
    and MediaInfo facts. Returns a human-readable conflict description when the
    tracker appears to have mislinked the movie, or None when consistent.
    """
    reasons: list[str] = []

    if query_year is not None and movie.year is not None and abs(movie.year - query_year) > 1:
        reasons.append(f"year mismatch (release={query_year}, tracker={movie.year})")

    if (
        nfo_runtime is not None
        and movie.runtime_minutes is not None
        and abs(nfo_runtime - movie.runtime_minutes) > 15
    ):
        reasons.append(
            f"runtime mismatch (MediaInfo={nfo_runtime}m, tracker={movie.runtime_minutes}m)"
        )

    if query_title:
        norm_query = _normalize_title_for_compare(query_title)
        cand_norms = [
            _normalize_title_for_compare(t)
            for t in (movie.canonical_title, movie.original_title or "")
            if t and t.strip()
        ]
        title_matches = False
        for cn in cand_norms:
            if not cn:
                continue
            if norm_query == cn or _token_jaccard(norm_query, cn) >= 0.45:
                title_matches = True
                break
            if len(norm_query) >= 4 and len(cn) >= 4 and (norm_query in cn or cn in norm_query):
                title_matches = True
                break

        if not title_matches:
            # Allow romanized non-Latin release titles only when corroborated by runtime or audio language
            # (and no year/runtime conflict already detected).
            non_latin_corroborated = (
                not reasons
                and _has_non_latin_letters(movie.original_title)
                and (
                    (
                        nfo_runtime is not None
                        and movie.runtime_minutes is not None
                        and abs(nfo_runtime - movie.runtime_minutes) <= 10
                    )
                    or _audio_languages_match_iso(audio_languages or [], movie.original_language)
                )
            )
            if not non_latin_corroborated:
                reasons.append(
                    f"title mismatch (release='{query_title}', tracker='{movie.canonical_title}'"
                    + (f" / '{movie.original_title}'" if movie.original_title and movie.original_title != movie.canonical_title else "")
                    + ")"
                )

    if not reasons:
        return None

    return (
        f"Tracker-supplied ID resolved to '{movie.canonical_title}' ({movie.year}, "
        f"imdb={movie.imdb_id or 'unknown'}, runtime={movie.runtime_minutes or '?'}m), "
        f"which conflicts with release evidence: {'; '.join(reasons)}."
    )


def format_torrent_info_for_prompt(
    torrent_info: Optional[TorrentInfo],
    *,
    conflict_warning: Optional[str] = None,
) -> Optional[str]:
    """
    Format sanitized, bounded tracker metadata and MediaInfo/NFO excerpts for Gemini.
    Always frames tracker IDs and descriptions as fallible clues, while MediaInfo
    duration/audio reflect the actual media file.
    """
    if torrent_info is None and not conflict_warning:
        return None

    lines = [
        "TRACKER TORRENT INFO (corroborating context; tracker IDs/descriptions are uploader-supplied and can occasionally be wrong, whereas MediaInfo duration/audio reflect the actual file):"
    ]
    if conflict_warning:
        lines.append(
            f"- CONFLICT WARNING: {conflict_warning} "
            "Treat the tracker's ID and synopsis as UNTRUSTED; verify the true film from the release string, MediaInfo facts, and Google Search, and note the tracker mismatch in disambiguation_note."
        )

    if torrent_info is not None:
        meta_parts: list[str] = []
        if torrent_info.indexer:
            meta_parts.append(f"indexer={torrent_info.indexer}")
        if torrent_info.category:
            meta_parts.append(f"category={torrent_info.category}")
        if torrent_info.resolution:
            meta_parts.append(f"resolution={torrent_info.resolution}")
        if torrent_info.source:
            meta_parts.append(f"source={torrent_info.source}")
        if torrent_info.media_processing:
            meta_parts.append(f"processing={torrent_info.media_processing}")
        if torrent_info.dynamic_range:
            meta_parts.append("dynamic_range=" + "+".join(torrent_info.dynamic_range))
        if torrent_info.group:
            meta_parts.append(f"group={torrent_info.group}")
        if torrent_info.size_gb is not None:
            meta_parts.append(f"size={torrent_info.size_gb:.1f} GB")
        if torrent_info.freeleech is not None:
            meta_parts.append(f"freeleech={str(torrent_info.freeleech).lower()}")
        if meta_parts:
            lines.append("- Release metadata: " + ", ".join(meta_parts))

        effective_runtime = torrent_info.runtime_minutes or extract_nfo_runtime_minutes(
            torrent_info.nfo_text
        )
        if effective_runtime is not None:
            lines.append(f"- MediaInfo file duration: {effective_runtime} minutes")
        if torrent_info.audio_languages:
            lines.append("- MediaInfo audio languages: " + ", ".join(torrent_info.audio_languages))

        id_label = "untrusted/conflicting" if conflict_warning else "tracker-reported, cross-check"
        if torrent_info.imdb_id:
            lines.append(f"- Tracker IMDb ID ({id_label}): {torrent_info.imdb_id}")
        if torrent_info.tmdb_id is not None:
            lines.append(f"- Tracker TMDB ID ({id_label}): {torrent_info.tmdb_id}")

        clean_desc = sanitize_tracker_text(torrent_info.description, max_chars=900)
        if clean_desc:
            lines.append(f"- Tracker description excerpt: {clean_desc}")

        clean_nfo = sanitize_tracker_text(torrent_info.nfo_text, max_chars=900)
        if clean_nfo:
            lines.append(f"- MediaInfo / NFO excerpt: {clean_nfo}")

    if len(lines) <= 1:
        return None
    return "\n".join(lines)


class TmdbClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        timeout_seconds: float = TMDB_TIMEOUT_SECONDS,
        base_url: str = TMDB_API_BASE,
    ) -> None:
        raw_key = api_key if api_key is not None else os.getenv("TMDB_API_KEY", "")
        self.api_key = raw_key.strip()
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url.rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _is_bearer_token(self) -> bool:
        # TMDB v4 Read Access Tokens are JWTs (typically >100 chars starting with eyJ)
        return len(self.api_key) > 64 or self.api_key.startswith("eyJ")

    def _get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        query_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        headers = {"Accept": "application/json", "User-Agent": "movie-triage-api/1.0"}
        if self._is_bearer_token():
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            query_params["api_key"] = self.api_key

        url = f"{self.base_url}{path}"
        if query_params:
            url = f"{url}?{urllib.parse.urlencode(query_params)}"

        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _find_tmdb_id_by_imdb(self, imdb_id: str) -> Optional[int]:
        clean_imdb = imdb_id.strip().lower()
        data = self._get_json(
            f"/find/{urllib.parse.quote(clean_imdb, safe='')}",
            params={"external_source": "imdb_id"},
        )
        movie_results = data.get("movie_results") or []
        if not movie_results:
            return None
        movie_id = movie_results[0].get("id")
        return int(movie_id) if movie_id is not None else None

    def _search_results(self, title: str, year: Optional[int] = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": title, "include_adult": "false"}
        if year is not None:
            params["year"] = year

        data = self._get_json("/search/movie", params=params)
        results = data.get("results") or []

        # Retry without year filter to catch festival-premiere vs theatrical-release +/-1 year drift
        if not results and year is not None:
            data = self._get_json(
                "/search/movie",
                params={"query": title, "include_adult": "false"},
            )
            results = data.get("results") or []
        return results

    def _search_tmdb_id(self, title: str, year: Optional[int] = None) -> Optional[int]:
        results = self._search_results(title, year)
        best = select_best_tmdb_candidate(results, title, year)
        if best is None or best.get("id") is None:
            return None
        return int(best["id"])

    def _search_tmdb_movie(
        self,
        title: str,
        year: Optional[int] = None,
        *,
        nfo_runtime: Optional[int] = None,
        audio_languages: Optional[list[str]] = None,
    ) -> Optional[TmdbMovie]:
        results = self._search_results(title, year)
        if not results:
            return None

        # Score candidates; when MediaInfo runtime/language is available and >=2 candidates
        # are plausible, inspect up to top 3 via /movie/{id} to break ties deterministically.
        norm_query = _normalize_title_for_compare(title)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in results[:10]:
            cand_title = _normalize_title_for_compare(str(item.get("title") or ""))
            cand_orig = _normalize_title_for_compare(str(item.get("original_title") or ""))
            if norm_query in (cand_title, cand_orig):
                title_score = 100.0
            else:
                sim = max(
                    _token_jaccard(norm_query, cand_title),
                    _token_jaccard(norm_query, cand_orig),
                )
                if sim < 0.6:
                    continue
                title_score = 65.0 * sim

            cand_year = _parse_year(item.get("release_date"))
            year_score = 0.0
            if year is not None:
                if cand_year == year:
                    year_score = 50.0
                elif cand_year is not None and abs(cand_year - year) == 1:
                    year_score = 25.0
                elif cand_year is not None:
                    year_score = -80.0

            pop_score = min(float(item.get("popularity") or 0.0), 30.0) * 0.5
            scored.append((title_score + year_score + pop_score, item))

        if not scored:
            return None

        scored.sort(key=lambda pair: pair[0], reverse=True)
        min_threshold = 75.0 if year is not None else 100.0
        top_score = scored[0][0]
        if top_score < min_threshold:
            return None

        use_tiebreaker = (nfo_runtime is not None or bool(audio_languages)) and len(scored) >= 2
        close_candidates = [
            pair for pair in scored[:3] if pair[0] >= max(min_threshold - 10.0, top_score - 30.0)
        ]

        if not use_tiebreaker or len(close_candidates) < 2:
            top_id = scored[0][1].get("id")
            return self._fetch_movie_details(int(top_id)) if top_id is not None else None

        best_final = -1e9
        best_movie: Optional[TmdbMovie] = None
        for base_score, item in close_candidates:
            cid = item.get("id")
            if cid is None:
                continue
            movie = self._fetch_movie_details(int(cid))
            bonus = 0.0
            if nfo_runtime is not None and movie.runtime_minutes is not None:
                diff = abs(movie.runtime_minutes - nfo_runtime)
                if diff <= 8:
                    bonus += 35.0
                elif diff <= 15:
                    bonus += 15.0
                elif diff > 20:
                    bonus -= 30.0
            if audio_languages and _audio_languages_match_iso(audio_languages, movie.original_language):
                bonus += 15.0

            final_score = base_score + bonus
            if final_score > best_final:
                best_final = final_score
                best_movie = movie

        return best_movie

    def _fetch_movie_details(self, tmdb_id: int) -> TmdbMovie:
        data = self._get_json(
            f"/movie/{tmdb_id}",
            params={"append_to_response": "credits,external_ids"},
        )
        external_ids = data.get("external_ids") or {}
        raw_imdb = external_ids.get("imdb_id") or data.get("imdb_id")
        imdb_id = str(raw_imdb).strip().lower() if raw_imdb and str(raw_imdb).strip() else None

        raw_runtime = data.get("runtime")
        runtime_minutes = int(raw_runtime) if isinstance(raw_runtime, int) and raw_runtime > 0 else None

        crew = (data.get("credits") or {}).get("crew") or []
        directors: list[str] = []
        for member in crew:
            if member.get("job") == "Director" and member.get("name"):
                name = str(member["name"]).strip()
                if name and name not in directors:
                    directors.append(name)

        genres = [
            str(g["name"]).strip()
            for g in (data.get("genres") or [])
            if isinstance(g, dict) and g.get("name")
        ][:4]

        canonical_title = str(data.get("title") or data.get("original_title") or "").strip()
        raw_orig = data.get("original_title")
        original_title = str(raw_orig).strip() if raw_orig and str(raw_orig).strip() else None

        return TmdbMovie(
            tmdb_id=int(data["id"]),
            canonical_title=canonical_title,
            original_title=original_title,
            year=_parse_year(data.get("release_date")),
            imdb_id=imdb_id,
            runtime_minutes=runtime_minutes,
            director=", ".join(directors) if directors else None,
            genres=genres,
            original_language=str(data.get("original_language") or "").strip() or None,
        )

    def _lookup_with_diagnostics_sync(self, request: EvaluateRequest) -> TmdbLookupResult:
        query_title, query_year = extract_tmdb_query(
            request.raw_text,
            hint_title=request.hint_title,
            hint_year=request.hint_year,
        )
        tinfo = request.torrent_info
        nfo_runtime = (
            tinfo.runtime_minutes
            if tinfo is not None and tinfo.runtime_minutes is not None
            else (extract_nfo_runtime_minutes(tinfo.nfo_text) if tinfo is not None else None)
        )
        audio_languages = list(tinfo.audio_languages) if tinfo is not None else []

        # 1) Check caller-provided explicit IMDb ID in raw_text / extra_context (not tracker)
        direct_imdb = extract_imdb_id(request.raw_text, request.extra_context)
        if direct_imdb:
            direct_tmdb_id = self._find_tmdb_id_by_imdb(direct_imdb)
            if direct_tmdb_id is not None:
                return TmdbLookupResult(
                    movie=self._fetch_movie_details(direct_tmdb_id),
                    tracker_conflict=None,
                    trusted_for_cache=True,
                )

        # 2) Check tracker-supplied TMDB/IMDb ID — ALWAYS cross-checked against release & MediaInfo!
        tracker_conflict: Optional[str] = None
        if tinfo is not None and (tinfo.tmdb_id is not None or tinfo.imdb_id):
            tracker_tmdb_id: Optional[int] = tinfo.tmdb_id
            if tracker_tmdb_id is None and tinfo.imdb_id:
                tracker_tmdb_id = self._find_tmdb_id_by_imdb(tinfo.imdb_id)
            if tracker_tmdb_id is not None:
                tracker_movie = self._fetch_movie_details(tracker_tmdb_id)
                conflict = check_tracker_movie_consistency(
                    tracker_movie,
                    query_title=query_title,
                    query_year=query_year,
                    nfo_runtime=nfo_runtime,
                    audio_languages=audio_languages,
                )
                if conflict is None:
                    return TmdbLookupResult(
                        movie=tracker_movie,
                        tracker_conflict=None,
                        trusted_for_cache=True,
                    )
                logger.warning("Rejecting tracker-supplied TMDB candidate: %s", conflict)
                tracker_conflict = conflict

        # 3) Clean title + year search (with MediaInfo runtime/language tie-breaking)
        if query_title:
            searched = self._search_tmdb_movie(
                query_title,
                query_year,
                nfo_runtime=nfo_runtime,
                audio_languages=audio_languages,
            )
            if searched is not None:
                # If a tracker conflict occurred, still run Gemini with the conflict warning
                # rather than silently serving a cached entry without disambiguation.
                return TmdbLookupResult(
                    movie=searched,
                    tracker_conflict=tracker_conflict,
                    trusted_for_cache=tracker_conflict is None,
                )

        return TmdbLookupResult(
            movie=None,
            tracker_conflict=tracker_conflict,
            trusted_for_cache=False,
        )

    def _lookup_from_request_sync(self, request: EvaluateRequest) -> Optional[TmdbMovie]:
        return self._lookup_with_diagnostics_sync(request).movie

    def _verify_identification_sync(self, ident: Identification) -> Optional[TmdbMovie]:
        tmdb_id: Optional[int] = None
        if ident.imdb_id:
            tmdb_id = self._find_tmdb_id_by_imdb(ident.imdb_id)

        if tmdb_id is None and ident.canonical_title:
            tmdb_id = self._search_tmdb_id(ident.canonical_title, ident.year)

        if tmdb_id is None and ident.original_title and ident.original_title != ident.canonical_title:
            tmdb_id = self._search_tmdb_id(ident.original_title, ident.year)

        if tmdb_id is None:
            return None

        movie = self._fetch_movie_details(tmdb_id)
        if not is_tmdb_compatible(ident, movie):
            return None
        return movie

    async def lookup_with_diagnostics(self, request: EvaluateRequest) -> TmdbLookupResult:
        if not self.enabled:
            return TmdbLookupResult()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._lookup_with_diagnostics_sync, request),
                timeout=self.timeout_seconds + 4.0,
            )
        except (urllib.error.URLError, TimeoutError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            logger.warning("TMDB pre-lookup failed (falling back to Gemini-only): %s", exc)
            return TmdbLookupResult()
        except Exception as exc:
            logger.warning("Unexpected TMDB pre-lookup error: %s", exc)
            return TmdbLookupResult()

    async def lookup_from_request(self, request: EvaluateRequest) -> Optional[TmdbMovie]:
        result = await self.lookup_with_diagnostics(request)
        return result.movie

    async def verify_identification(self, ident: Identification) -> Optional[TmdbMovie]:
        if not self.enabled:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._verify_identification_sync, ident),
                timeout=self.timeout_seconds + 2.0,
            )
        except (urllib.error.URLError, TimeoutError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            logger.warning("TMDB post-verification failed: %s", exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected TMDB post-verification error: %s", exc)
            return None
EOF_TMDB_CLIENT

cat <<'EOF_GEMINI_CLIENT' > "$APP_DIR/gemini_client.py"
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from google import genai
from google.genai import types

from schemas import EvaluateRequest, EvaluationPayload

DEFAULT_TASTE_PROFILE_PATH = Path(__file__).resolve().parent / "taste_profile.txt"
DEFAULT_TASTE_PROFILE = (
    "No strong genre bias yet. Prioritize films with strong craft, originality, "
    "or notable festival/critical recognition. Flag derivative blockbusters, slow "
    "pacing with weak payoff, or ratings inflated by very low vote counts."
)
REQUEST_TIMEOUT_SECONDS = 75.0


class GeminiClientError(RuntimeError):
    """Raised when Gemini returns an invalid or upstream error response."""


class GeminiTimeoutError(GeminiClientError):
    """Raised when the Gemini API call exceeds the 75-second timeout."""


def load_taste_profile(path: Optional[Path | str] = None) -> str:
    """Reload taste_profile.txt from disk dynamically on every request."""
    target = Path(path or os.getenv("TASTE_PROFILE_PATH") or DEFAULT_TASTE_PROFILE_PATH)
    try:
        content = target.read_text(encoding="utf-8").strip()
        return content or DEFAULT_TASTE_PROFILE
    except OSError:
        return DEFAULT_TASTE_PROFILE


def build_system_instruction(
    taste_profile: str,
    recent_feedback: list[dict[str, Any]],
) -> str:
    lines = [
        "You are a fast, rigorous film triage assistant evaluating movie release alerts.",
        "Your goal is to help the user decide WATCH, MAYBE, or SKIP in under 10 seconds.",
        "",
        "STRICT RULES:",
        "1. Always use Google Search to identify the exact film from messy release strings, "
        "foreign titles, festival cuts, or remakes. Include a short disambiguation_note when "
        "multiple films share the title or when normalizing a non-English title.",
        "2. NEVER fabricate, estimate, or guess any rating. Only populate a rating field "
        "(rotten_tomatoes_critics, rotten_tomatoes_audience, metacritic, imdb, letterboxd) "
        "when you find an actual current score via Google Search. Set every unverified score "
        "to null and list its platform name in ratings.missing_sources.",
        "3. Keep every text field concise and high-signal for a sub-10-second decision: "
        "one_line_pitch must be 1 sentence, critic_consensus 1-2 short sentences, verdict_reason "
        "1-2 short sentences, awards_highlights max 4 items, similar_to 2-4 titles, red_flags "
        "concise caveats (or empty list if none).",
        "4. Return strictly valid JSON conforming to the provided JSON schema.",
        "5. Tracker-supplied metadata in TRACKER TORRENT INFO (IMDb/TMDB links, synopsis, category) "
        "is useful corroborating evidence, NOT infallible ground truth — uploaders occasionally "
        "link the wrong film or remake. MediaInfo technical facts (file duration, audio language) "
        "reflect the actual video file. If tracker metadata conflicts with the release title/year, "
        "MediaInfo duration, or Google Search, trust the release string + MediaInfo + Google Search, "
        "lower match_confidence if still ambiguous, and briefly note the tracker mismatch in "
        "disambiguation_note.",
        "6. Factor RELEASE QUALITY (Remux-2160p vs Remux-1080p, Dolby Vision Profile 7 FEL vs "
        "MEL / Profile 8 / HDR / SDR) and FAVORITE RELEASE GROUP (e.g., CiNEPHiLES) as soft "
        "edge-case tie-breakers per the user's taste profile: treat Remux-2160p with Dolby Vision "
        "Profile 7 FEL as a HUGE positive bonus (above P7 MEL / P8 / generic DV > HDR/SDR > "
        "Remux-1080p), and treat CiNEPHiLES as a strong positive signal for technical care and "
        "curated selection. When a 2160p UHD Remux shows Profile 7 (BL+EL+RPU) without explicit "
        "FEL/MEL in the NFO, you may check via Google Search whether the UHD Blu-ray disc is "
        "documented as a P7 FEL transfer (clearly note if disc-reported rather than NFO-confirmed; "
        "never guess if unverified). Never treat format or group as a hard rule — do NOT upgrade "
        "a clearly weak film to WATCH solely for 2160p DV P7 FEL or group, and do NOT downgrade a "
        "clearly great film to SKIP solely for 1080p SDR. Mention release quality / P7 FEL / group "
        "in verdict_reason or red_flags only when it meaningfully influences the decision.",
        "",
        "USER TASTE PROFILE:",
        taste_profile.strip(),
    ]

    if recent_feedback:
        lines.append("")
        lines.append("RECENT USER FEEDBACK HISTORY (most recent first):")
        for row in recent_feedback[:20]:
            imdb_part = f" [{row['imdb_id']}]" if row.get("imdb_id") else ""
            note_part = f" — {row['note']}" if row.get("note") else ""
            lines.append(
                f"- {row['decision']}: {row['title']}{imdb_part}{note_part}"
            )

    return "\n".join(lines)


def build_user_prompt(
    request: EvaluateRequest,
    tmdb_context: Optional[str] = None,
    torrent_context: Optional[str] = None,
    release_quality_context: Optional[str] = None,
) -> str:
    parts = [f"Release string / query: {request.raw_text}"]
    if request.hint_title:
        parts.append(f"Hint title: {request.hint_title}")
    if request.hint_year is not None:
        parts.append(f"Hint year: {request.hint_year}")
    if request.extra_context:
        parts.append(f"Extra context: {request.extra_context}")
    if release_quality_context:
        parts.append(release_quality_context)
    if torrent_context:
        parts.append("")
        parts.append(torrent_context)
    if tmdb_context:
        parts.append("")
        parts.append(tmdb_context)
    if torrent_context or tmdb_context:
        parts.append("")
    parts.append(
        "Identify the exact film, verify available critic/audience scores via Google Search, "
        "and return the structured triage JSON."
    )
    return "\n".join(parts)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


class GeminiTriageClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        taste_profile_path: Optional[Path | str] = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "").strip()
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip()
        self.taste_profile_path = (
            Path(taste_profile_path)
            if taste_profile_path is not None
            else DEFAULT_TASTE_PROFILE_PATH
        )
        self.timeout_seconds = timeout_seconds
        self._client: Optional[genai.Client] = None

    def _get_client(self) -> genai.Client:
        if not self.api_key:
            raise GeminiClientError("GEMINI_API_KEY is not configured in .env")
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _call_sync(
        self,
        request: EvaluateRequest,
        system_instruction: str,
        tmdb_context: Optional[str] = None,
        torrent_context: Optional[str] = None,
        release_quality_context: Optional[str] = None,
    ) -> EvaluationPayload:
        client = self._get_client()
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[{"google_search": {}}],
            response_mime_type="application/json",
            response_json_schema=EvaluationPayload.model_json_schema(),
        )
        response = client.models.generate_content(
            model=self.model,
            contents=build_user_prompt(
                request,
                tmdb_context=tmdb_context,
                torrent_context=torrent_context,
                release_quality_context=release_quality_context,
            ),
            config=config,
        )
        raw_text = getattr(response, "text", None)
        if not raw_text:
            raise GeminiClientError("Gemini returned an empty response payload")

        cleaned = _strip_json_fences(raw_text)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise GeminiClientError(f"Gemini returned non-JSON output: {exc}") from exc

        return EvaluationPayload.model_validate(parsed)

    async def evaluate(
        self,
        request: EvaluateRequest,
        recent_feedback: list[dict[str, Any]],
        *,
        tmdb_context: Optional[str] = None,
        torrent_context: Optional[str] = None,
        release_quality_context: Optional[str] = None,
    ) -> EvaluationPayload:
        # Dynamically reload taste_profile.txt on every single request
        taste_profile = load_taste_profile(self.taste_profile_path)
        system_instruction = build_system_instruction(taste_profile, recent_feedback)

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._call_sync,
                    request,
                    system_instruction,
                    tmdb_context,
                    torrent_context,
                    release_quality_context,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise GeminiTimeoutError(
                f"Gemini evaluation timed out after {int(self.timeout_seconds)} seconds"
            ) from exc
        except GeminiClientError:
            raise
        except Exception as exc:
            raise GeminiClientError(f"Gemini evaluation failed: {exc}") from exc
EOF_GEMINI_CLIENT

cat <<'EOF_MAIN' > "$APP_DIR/main.py"
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

from db import TriageDB, parse_release_quality
from gemini_client import GeminiClientError, GeminiTimeoutError, GeminiTriageClient
from schemas import (
    EvaluateRequest,
    EvaluateResponse,
    FeedbackItem,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    RecentEvaluationItem,
)
from tmdb_client import (
    TmdbClient,
    apply_tmdb_verification,
    format_torrent_info_for_prompt,
    is_tmdb_compatible,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen").strip()
PORT = int(os.getenv("PORT", "8000"))
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "72"))

db = TriageDB(BASE_DIR / "data" / "triage.db")
gemini_client = GeminiTriageClient(
    api_key=os.getenv("GEMINI_API_KEY", "").strip(),
    model=GEMINI_MODEL,
    taste_profile_path=BASE_DIR / "taste_profile.txt",
)
tmdb_client = TmdbClient(api_key=os.getenv("TMDB_API_KEY", "").strip())


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Movie Triage API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        model=GEMINI_MODEL,
        port=PORT,
        cache_entries=db.get_cache_entries_count(ttl_hours=CACHE_TTL_HOURS),
        tmdb_enabled=tmdb_client.enabled,
    )


@app.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(payload: EvaluateRequest) -> EvaluateResponse:
    tinfo = payload.torrent_info
    release_quality = parse_release_quality(
        payload.raw_text,
        resolution=tinfo.resolution if tinfo else None,
        source=tinfo.source if tinfo else None,
        dynamic_range=tinfo.dynamic_range if tinfo else None,
        media_processing=tinfo.media_processing if tinfo else None,
        group=tinfo.group if tinfo else None,
        nfo_text=tinfo.nfo_text if tinfo else None,
        description=tinfo.description if tinfo else None,
    )

    if not payload.force_refresh:
        cached = db.get_cached_evaluation(
            payload.raw_text,
            hint_title=payload.hint_title,
            hint_year=payload.hint_year,
            extra_context=payload.extra_context,
            quality_key=release_quality.cache_key,
            ttl_hours=CACHE_TTL_HOURS,
        )
        if cached is not None:
            return EvaluateResponse(cached=True, **cached)

    lookup = await tmdb_client.lookup_with_diagnostics(payload)
    tmdb_movie = lookup.movie

    # Cross-language/alias cache check ONLY when TMDB lookup had no tracker conflict
    # and the cached evaluation matches the incoming release quality tier.
    if (
        not payload.force_refresh
        and lookup.trusted_for_cache
        and tmdb_movie is not None
        and tmdb_movie.imdb_id
    ):
        cached_by_imdb = db.get_cached_evaluation(
            payload.raw_text,
            hint_title=tmdb_movie.canonical_title,
            hint_year=tmdb_movie.year,
            extra_context=tmdb_movie.imdb_id,
            quality_key=release_quality.cache_key,
            ttl_hours=CACHE_TTL_HOURS,
        )
        if cached_by_imdb is not None:
            return EvaluateResponse(cached=True, **cached_by_imdb)

    recent_feedback = db.get_recent_feedback(limit=20)
    tmdb_context = tmdb_movie.to_prompt_block() if tmdb_movie is not None else None
    torrent_context = format_torrent_info_for_prompt(
        payload.torrent_info,
        conflict_warning=lookup.tracker_conflict,
    )
    try:
        result = await gemini_client.evaluate(
            payload,
            recent_feedback,
            tmdb_context=tmdb_context,
            torrent_context=torrent_context,
            release_quality_context=release_quality.to_prompt_line(),
        )
    except GeminiTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except GeminiClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if tmdb_client.enabled and (
        tmdb_movie is None or not is_tmdb_compatible(result.identification, tmdb_movie)
    ):
        tmdb_movie = await tmdb_client.verify_identification(result.identification)

    verified_result = apply_tmdb_verification(result, tmdb_movie)
    result_dict = verified_result.model_dump()
    db.save_evaluation(
        payload.raw_text,
        result_dict,
        hint_title=payload.hint_title,
        hint_year=payload.hint_year,
        quality_key=release_quality.cache_key,
    )
    return EvaluateResponse(cached=False, **result_dict)


@app.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(payload: FeedbackRequest) -> FeedbackResponse:
    total = db.add_feedback(
        imdb_id=payload.imdb_id,
        title=payload.title,
        decision=payload.decision,
        note=payload.note,
    )
    return FeedbackResponse(
        status="saved",
        decision=payload.decision,
        total_feedback=total,
    )


@app.get("/feedback", response_model=list[FeedbackItem])
async def list_feedback(limit: int = Query(default=20, ge=1, le=50)) -> list[FeedbackItem]:
    rows = db.get_recent_feedback(limit=limit)
    return [FeedbackItem(**row) for row in rows]


@app.get("/recent", response_model=list[RecentEvaluationItem])
async def recent(limit: int = Query(default=20, ge=1, le=100)) -> list[RecentEvaluationItem]:
    rows = db.get_recent_evaluations(limit=limit)
    return [RecentEvaluationItem(**row) for row in rows]
EOF_MAIN

echo "[3/6] Writing locked-down .env (chmod 600)..."
cat <<EOF_ENV > "$APP_DIR/.env"
GEMINI_API_KEY=${GEMINI_API_KEY}
GEMINI_MODEL=gemini-4-hydrogen
HOST=127.0.0.1
PORT=${CHOSEN_PORT}
CACHE_TTL_HOURS=72
TMDB_API_KEY=${TMDB_API_KEY:-}
MDBLIST_API_KEY=
EOF_ENV
chmod 600 "$APP_DIR/.env"

echo "[4/6] Creating Python virtual environment and installing dependencies..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[5/6] Configuring and starting userland systemd unit..."
cat <<EOF_UNIT > "$SYSTEMD_DIR/$SERVICE_NAME"
[Unit]
Description=Local-only Movie Triage FastAPI Microservice
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/apps/movie-triage-api
EnvironmentFile=%h/apps/movie-triage-api/.env
ExecStart=%h/apps/movie-triage-api/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${CHOSEN_PORT}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF_UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"
sleep 2
systemctl --user status "$SERVICE_NAME" --no-pager

echo "[6/6] Running live smoke tests against http://127.0.0.1:${CHOSEN_PORT}..."
echo ""
echo "--- Smoke Test 1: GET /health ---"
curl -sS "http://127.0.0.1:${CHOSEN_PORT}/health" | python3 -m json.tool

echo ""
echo "--- Smoke Test 2: POST /evaluate (La Chimera, expect cached: false) ---"
curl -sS -X POST "http://127.0.0.1:${CHOSEN_PORT}/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "La.Chimera.2023.1080p.BluRay.x264-GROUP"}' | python3 -m json.tool

echo ""
echo "--- Smoke Test 3: Repeat POST /evaluate (La Chimera, expect cached: true) ---"
curl -sS -X POST "http://127.0.0.1:${CHOSEN_PORT}/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "La.Chimera.2023.1080p.BluRay.x264-GROUP"}' | python3 -m json.tool

echo ""
echo "--- Smoke Test 4: Disambiguation check (foreign/remake release string) ---"
curl -sS -X POST "http://127.0.0.1:${CHOSEN_PORT}/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "Anatomie.d.une.chute.2023.FRENCH.2160p.UHD.BluRay.HEVC-GROUP"}' | python3 -m json.tool

echo ""
echo "Deployment complete. Movie Triage API is listening on http://127.0.0.1:${CHOSEN_PORT}"
