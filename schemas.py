from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


MatchConfidence = Literal["high", "medium", "low"]
Verdict = Literal["WATCH", "MAYBE", "SKIP"]


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    raw_text: str = Field(..., min_length=1, description="Messy release string or movie title")
    hint_title: Optional[str] = Field(default=None, description="Optional title hint")
    hint_year: Optional[int] = Field(default=None, ge=1888, le=2100, description="Optional release year hint")
    extra_context: Optional[str] = Field(default=None, description="Optional tracker, IMDb ID, or release context")
    force_refresh: bool = Field(default=False, description="Bypass SQLite cache and query Gemini fresh")


class Identification(BaseModel):
    canonical_title: str = Field(..., description="Primary English or widely recognized title")
    original_title: Optional[str] = Field(default=None, description="Original language title if different")
    year: int = Field(..., description="Theatrical or festival release year")
    director: str = Field(..., description="Director full name(s)")
    imdb_id: Optional[str] = Field(default=None, description="Verified IMDb ID in ttNNNNNNN format or null")
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
    title: str = Field(..., min_length=1, description="Movie title")
    decision: Verdict = Field(..., description="User decision: WATCH, MAYBE, or SKIP")
    note: Optional[str] = Field(default=None, description="Optional personal note explaining why")


class FeedbackResponse(BaseModel):
    status: Literal["saved"] = "saved"
    total_feedback: int


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    model: str
    port: int
    cache_entries: int


class RecentEvaluationItem(BaseModel):
    title: str
    year: int
    verdict: Verdict
    imdb_id: Optional[str] = None
    created_at: str
