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
from tmdb_client import sanitize_tracker_text

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
            if row.get("imdb_id"):
                id_part = f" [{row['imdb_id']}]"
            elif row.get("tmdb_id") is not None:
                id_part = f" [tmdb:{row['tmdb_id']}]"
            else:
                id_part = ""
            clean_note = sanitize_tracker_text(row.get("note"), max_chars=200)
            note_part = f" — {clean_note}" if clean_note else ""
            lines.append(
                f"- {row['decision']}: {row['title']}{id_part}{note_part}"
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
        clean_extra = sanitize_tracker_text(request.extra_context, max_chars=500)
        if clean_extra:
            parts.append(f"Extra context: {clean_extra}")
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


_RATING_PLATFORM_SPECS: dict[str, tuple[str, float, float, bool, tuple[str, ...], tuple[str, ...]]] = {
    # key: (display_label, min_val, max_val, is_int, domains, title_keywords)
    "rotten_tomatoes_critics": (
        "Rotten Tomatoes Critics",
        0.0,
        100.0,
        True,
        ("rottentomatoes.com",),
        ("rotten tomatoes", "rottentomatoes"),
    ),
    "rotten_tomatoes_audience": (
        "Rotten Tomatoes Audience",
        0.0,
        100.0,
        True,
        ("rottentomatoes.com",),
        ("rotten tomatoes", "rottentomatoes"),
    ),
    "metacritic": (
        "Metacritic",
        0.0,
        100.0,
        True,
        ("metacritic.com",),
        ("metacritic",),
    ),
    "imdb": (
        "IMDb",
        0.0,
        10.0,
        False,
        ("imdb.com",),
        ("imdb",),
    ),
    "letterboxd": (
        "Letterboxd",
        0.0,
        5.0,
        False,
        ("letterboxd.com",),
        ("letterboxd",),
    ),
}


def extract_grounding_sources(response: Any) -> list[dict[str, str]]:
    """Extract normalized web grounding sources (domain + title) from a Gemini GenerateContentResponse."""
    sources: list[dict[str, str]] = []
    candidates = getattr(response, "candidates", None)
    if candidates is None and isinstance(response, dict):
        candidates = response.get("candidates")
    if not candidates:
        return sources

    for cand in candidates:
        gm = getattr(cand, "grounding_metadata", None)
        if gm is None and isinstance(cand, dict):
            gm = cand.get("grounding_metadata") or cand.get("groundingMetadata")
        if gm is None:
            continue
        chunks = getattr(gm, "grounding_chunks", None)
        if chunks is None and isinstance(gm, dict):
            chunks = gm.get("grounding_chunks") or gm.get("groundingChunks")
        if not chunks:
            continue
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            if web is None and isinstance(chunk, dict):
                web = chunk.get("web")
            if web is None:
                continue
            uri = str(getattr(web, "uri", None) or (web.get("uri") if isinstance(web, dict) else "") or "").strip()
            title = str(getattr(web, "title", None) or (web.get("title") if isinstance(web, dict) else "") or "").strip()
            domain = ""
            if uri:
                m = re.match(r"^https?://([^/\s:?#]+)", uri, flags=re.IGNORECASE)
                if m:
                    domain = m.group(1).lower().lstrip("www.")
            if domain or title:
                sources.append({"domain": domain, "title": title})
    return sources


def _find_supporting_grounding_source(
    rating_key: str,
    grounding_sources: list[dict[str, str]],
) -> Optional[str]:
    spec = _RATING_PLATFORM_SPECS.get(rating_key)
    if spec is None:
        return None
    _label, _lo, _hi, _is_int, domains, keywords = spec
    for src in grounding_sources:
        dom = (src.get("domain") or "").lower()
        title = (src.get("title") or "").lower()
        for expected_dom in domains:
            if dom == expected_dom or dom.endswith("." + expected_dom):
                return expected_dom
        for kw in keywords:
            if kw in title or kw in dom:
                return domains[0]
    return None


def validate_and_ground_ratings(
    raw_ratings: Any,
    *,
    grounding_sources: list[dict[str, str]],
) -> dict[str, Any]:
    """
    Validate platform-specific numeric score bounds and verify that every displayed rating
    is backed by an actual retrieved Google Search grounding source for that platform.
    Out-of-range or ungrounded scores are coerced to None (unknown) and listed in missing_sources.
    """
    ratings_in = dict(raw_ratings) if isinstance(raw_ratings, dict) else {}
    existing_missing = ratings_in.get("missing_sources")
    missing: list[str] = [
        str(m).strip()
        for m in (existing_missing if isinstance(existing_missing, list) else [])
        if str(m).strip()
    ]
    verified_sources: dict[str, str] = {}
    cleaned: dict[str, Any] = {}

    def _add_missing(label: str) -> None:
        if not any(m.lower() == label.lower() for m in missing):
            missing.append(label)

    for key, (label, lo, hi, is_int, _domains, _keywords) in _RATING_PLATFORM_SPECS.items():
        val = ratings_in.get(key)
        if val is None:
            cleaned[key] = None
            continue

        # Validate numeric type and platform-specific bounds (reject bool/NaN/Inf/out-of-range)
        valid_num: Optional[int | float] = None
        if not isinstance(val, bool) and isinstance(val, (int, float)):
            fval = float(val)
            if fval == fval and fval not in (float("inf"), float("-inf")) and lo <= fval <= hi:
                if is_int:
                    if float(int(round(fval))) == fval:
                        valid_num = int(round(fval))
                else:
                    valid_num = round(fval, 2)

        if valid_num is None:
            cleaned[key] = None
            _add_missing(label)
            continue

        # Verify actual grounding source support for this rating platform
        supporting_src = _find_supporting_grounding_source(key, grounding_sources)
        if supporting_src is None:
            cleaned[key] = None
            _add_missing(label)
            continue

        cleaned[key] = valid_num
        verified_sources[key] = supporting_src

    cleaned["sources"] = verified_sources
    cleaned["missing_sources"] = missing
    return cleaned


def _build_gemini_response_schema() -> dict[str, Any]:
    schema = EvaluationPayload.model_json_schema()
    props = schema.get("properties")
    if isinstance(props, dict):
        props.pop("model_identification", None)
        props.pop("catalogue_identification", None)
    return schema


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
            response_json_schema=_build_gemini_response_schema(),
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
        if not isinstance(parsed, dict):
            raise GeminiClientError("Gemini returned non-object JSON payload")

        grounding_sources = extract_grounding_sources(response)
        parsed["ratings"] = validate_and_ground_ratings(
            parsed.get("ratings"),
            grounding_sources=grounding_sources,
        )
        # Never allow model output to pre-populate server-owned verification snapshots
        parsed.pop("model_identification", None)
        parsed.pop("catalogue_identification", None)

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
        # Dynamically reload taste_profile.txt off the event loop on every request
        taste_profile = await asyncio.to_thread(load_taste_profile, self.taste_profile_path)
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
            redacted = sanitize_tracker_text(str(exc), max_chars=400) or exc.__class__.__name__
            raise GeminiClientError(f"Gemini evaluation failed: {redacted}") from exc
