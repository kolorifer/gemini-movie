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
