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
    auth_enabled: bool = False


class RecentEvaluationItem(BaseModel):
    title: str
    year: int
    verdict: Verdict
    imdb_id: Optional[str] = None
    created_at: str
