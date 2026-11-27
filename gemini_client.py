from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

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
            clean_title = sanitize_tracker_text(row.get("title"), max_chars=200) or "Unknown"
            clean_note = sanitize_tracker_text(row.get("note"), max_chars=200)
            note_part = f" — {clean_note}" if clean_note else ""
            lines.append(
                f"- {row['decision']}: {clean_title}{id_part}{note_part}"
            )

    return "\n".join(lines)


def build_user_prompt(
    request: EvaluateRequest,
    tmdb_context: Optional[str] = None,
    torrent_context: Optional[str] = None,
    release_quality_context: Optional[str] = None,
) -> str:
    clean_raw = sanitize_tracker_text(request.raw_text, max_chars=512)
    parts = [f"Release string / query: {clean_raw}"]
    if request.hint_title:
        clean_hint = sanitize_tracker_text(request.hint_title, max_chars=240)
        if clean_hint:
            parts.append(f"Hint title: {clean_hint}")
    if request.hint_year is not None:
        parts.append(f"Hint year: {request.hint_year}")
    if request.extra_context:
        clean_extra = sanitize_tracker_text(request.extra_context, max_chars=500)
        if clean_extra:
            parts.append(f"Extra context: {clean_extra}")
    if release_quality_context:
        clean_rq = sanitize_tracker_text(release_quality_context, max_chars=400)
        if clean_rq:
            parts.append(clean_rq)
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


_RATING_PLATFORM_SPECS: dict[str, tuple[str, float, float, bool, tuple[str, ...]]] = {
    # key: (display_label, min_val, max_val, is_int, domains)
    "rotten_tomatoes_critics": (
        "Rotten Tomatoes Critics",
        0.0,
        100.0,
        True,
        ("rottentomatoes.com",),
    ),
    "rotten_tomatoes_audience": (
        "Rotten Tomatoes Audience",
        0.0,
        100.0,
        True,
        ("rottentomatoes.com",),
    ),
    "metacritic": (
        "Metacritic",
        0.0,
        100.0,
        True,
        ("metacritic.com",),
    ),
    "imdb": (
        "IMDb",
        0.0,
        10.0,
        False,
        ("imdb.com",),
    ),
    "letterboxd": (
        "Letterboxd",
        0.0,
        5.0,
        False,
        ("letterboxd.com",),
    ),
}

_BARE_HOSTNAME_RE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.IGNORECASE)
_IMDB_TITLE_PATH_RE = re.compile(r"^/title/(tt\d{7,10})(?:/|$)", re.IGNORECASE)
_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "in", "on", "to", "for", "at", "by"})


def _normalize_domain(host: str) -> str:
    low = host.strip().lower().rstrip(".")
    if low.startswith("www."):
        low = low[4:]
    return low


def _extract_attr_or_key(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict):
            if name in obj and obj[name] is not None:
                return obj[name]
        else:
            val = getattr(obj, name, None)
            if val is not None:
                return val
    return None


def extract_grounding_sources(response: Any) -> list[dict[str, Any]]:
    """
    Extract normalized web grounding sources (domain, uri, path, title, snippet,
    and linked grounding_supports segments) from a Gemini GenerateContentResponse.
    """
    sources: list[dict[str, Any]] = []
    candidates = _extract_attr_or_key(response, "candidates")
    if not candidates:
        return sources

    for cand in candidates:
        gm = _extract_attr_or_key(cand, "grounding_metadata", "groundingMetadata")
        if gm is None:
            continue
        chunks = _extract_attr_or_key(gm, "grounding_chunks", "groundingChunks") or []
        cand_sources: list[dict[str, Any]] = []
        for chunk in chunks:
            web = _extract_attr_or_key(chunk, "web")
            if web is None:
                cand_sources.append({})
                continue
            uri = str(_extract_attr_or_key(web, "uri") or "").strip()
            title = str(_extract_attr_or_key(web, "title") or "").strip()
            explicit_domain = str(_extract_attr_or_key(web, "domain") or "").strip()
            snippet = str(
                _extract_attr_or_key(web, "snippet", "text")
                or _extract_attr_or_key(chunk, "snippet", "text")
                or ""
            ).strip()
            domain = ""
            path = ""
            is_redirect = False
            if uri:
                parsed_uri = urlparse(uri)
                if parsed_uri.scheme.lower() in ("http", "https") and parsed_uri.hostname:
                    domain = _normalize_domain(parsed_uri.hostname)
                    path = parsed_uri.path or ""
                    if domain.endswith("vertexaisearch.cloud.google.com"):
                        is_redirect = True
                        if explicit_domain and _BARE_HOSTNAME_RE.fullmatch(explicit_domain):
                            domain = _normalize_domain(explicit_domain)
                            path = ""
                        elif _BARE_HOSTNAME_RE.fullmatch(title):
                            domain = _normalize_domain(title)
                            path = ""
            if not domain and explicit_domain and _BARE_HOSTNAME_RE.fullmatch(explicit_domain):
                domain = _normalize_domain(explicit_domain)
            entry: dict[str, Any] = {
                "domain": domain,
                "uri": uri,
                "path": path,
                "title": title,
                "snippet": snippet,
                "is_redirect": is_redirect,
                "supported_segments": [],
            }
            cand_sources.append(entry)

        supports = _extract_attr_or_key(gm, "grounding_supports", "groundingSupports") or []
        for sup in supports:
            seg = _extract_attr_or_key(sup, "segment")
            seg_text = str(_extract_attr_or_key(seg, "text") or "").strip() if seg is not None else ""
            if not seg_text:
                continue
            indices = _extract_attr_or_key(sup, "grounding_chunk_indices", "groundingChunkIndices") or []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(cand_sources) and cand_sources[idx]:
                    cand_sources[idx]["supported_segments"].append(seg_text)

        for entry in cand_sources:
            if entry and (entry.get("domain") or entry.get("title")):
                sources.append(entry)
    return sources


_FOUR_DIGIT_YEAR_RE = re.compile(r"(?<![a-z0-9])((?:188[89]|189\d|19\d\d|20\d\d|2100))(?![a-z0-9])")
_PAREN_YEAR_RE = re.compile(r"\(\s*((?:188[89]|189\d|19\d\d|20\d\d|2100))\s*\)")
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:[;,\n]+|\.\s+|\b(?:and|while|whereas|but|versus|vs\.?)\b|\s+/\s+|\s*\|\s*)",
    re.IGNORECASE,
)
_METRIC_CUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "rotten_tomatoes_critics",
        re.compile(
            r"\b(?:rotten_tomatoes_critics|tomatometer|critics?(?:\s+(?:score|rating|consensus))?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "rotten_tomatoes_audience",
        re.compile(
            r"\b(?:rotten_tomatoes_audience|popcornmeter|verified\s+audience|audience(?:\s+(?:score|rating))?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "metacritic",
        re.compile(r"\b(?:metacritic(?:\s+(?:score|rating))?|metascore)\b", re.IGNORECASE),
    ),
    (
        "imdb",
        re.compile(r"\bimdb(?:\s+(?:score|rating))?\b", re.IGNORECASE),
    ),
    (
        "letterboxd",
        re.compile(r"\bletterboxd(?:\s+(?:score|rating|average))?\b", re.IGNORECASE),
    ),
    (
        "rt_generic",
        re.compile(r"\b(?:rotten\s+tomatoes|rt)\b", re.IGNORECASE),
    ),
)
_RATING_NUM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.])(\d{1,3}(?:\.\d{1,2})?)(\s*%|\s*/\s*100|\s*/\s*10|\s*out\s+of\s+10|\s*/\s*5|\s*out\s+of\s+5)?(?![A-Za-z0-9.])",
    re.IGNORECASE,
)


def _title_tokens_for_match(title: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", title.lower())
    significant = [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]
    return significant if significant else tokens


def _extract_source_film_years(src: dict[str, Any], film_titles: list[str]) -> set[int]:
    """Extract explicit film years from chunk title, URL slug, and parenthesized segment titles."""
    title_embedded_years: set[int] = set()
    for ft in film_titles:
        for m in _FOUR_DIGIT_YEAR_RE.finditer(ft):
            title_embedded_years.add(int(m.group(1)))

    years: set[int] = set()
    for text in (str(src.get("title") or ""), str(src.get("path") or "")):
        for m in _FOUR_DIGIT_YEAR_RE.finditer(text):
            y = int(m.group(1))
            if y not in title_embedded_years:
                years.add(y)
    for text in [str(src.get("snippet") or ""), *(str(s) for s in (src.get("supported_segments") or []))]:
        for m in _PAREN_YEAR_RE.finditer(text):
            y = int(m.group(1))
            if y not in title_embedded_years:
                years.add(y)
    return years


def _chunk_matches_film(
    src: dict[str, Any],
    *,
    expected_domain: str,
    film_titles: list[str],
    film_imdb_id: Optional[str],
    film_year: Optional[int] = None,
) -> bool:
    path = str(src.get("path") or "").strip()
    path_low = path.lower()
    is_redirect = bool(src.get("is_redirect"))

    # 1. Verify platform movie-entity URL path shape when direct path is present
    if path_low and not is_redirect:
        if expected_domain == "imdb.com":
            m = _IMDB_TITLE_PATH_RE.match(path_low)
            if not m:
                return False
            if film_imdb_id and m.group(1).lower() != film_imdb_id.lower():
                return False
        elif expected_domain == "rottentomatoes.com":
            if not path_low.startswith("/m/"):
                return False
        elif expected_domain == "metacritic.com":
            if not path_low.startswith("/movie/"):
                return False
        elif expected_domain == "letterboxd.com":
            if not path_low.startswith("/film/"):
                return False

    # 2. Reject wrong-year remakes when the source explicitly specifies a film year
    combined = " ".join(
        [
            str(src.get("title") or ""),
            path,
            str(src.get("snippet") or ""),
            " ".join(str(s) for s in (src.get("supported_segments") or [])),
        ]
    ).lower()
    imdb_matched = bool(
        film_imdb_id
        and re.search(rf"(?<![a-z0-9]){re.escape(film_imdb_id.lower())}(?![a-z0-9])", combined)
    )
    if film_year is not None and not imdb_matched:
        source_years = _extract_source_film_years(src, film_titles)
        if source_years and not any(abs(y - film_year) <= 1 for y in source_years):
            return False

    # 3. Verify the chunk actually pertains to this specific film (IMDb ID or title tokens)
    if imdb_matched:
        return True

    combined_tokens = set(re.findall(r"[a-z0-9]+", combined))
    for ft in film_titles:
        req_tokens = _title_tokens_for_match(ft)
        if req_tokens and all(tok in combined_tokens for tok in req_tokens):
            return True
    return False


def _numeric_matches_target(num_str: str, value: int | float, *, is_int: bool) -> bool:
    try:
        parsed = float(num_str)
    except ValueError:
        return False
    if is_int:
        if "." in num_str and abs(parsed - round(parsed)) > 1e-6:
            return False
        return int(round(parsed)) == int(round(float(value)))
    return abs(parsed - float(value)) < 1e-6


def _extract_clause_metric_bindings(clause: str) -> list[tuple[str, str]]:
    """
    Bind each metric cue in a single clause to its directly associated numeric value
    so swapped scores (e.g. 'Tomatometer of 42% and Popcornmeter of 89%') never cross-validate.
    """
    cleaned = re.sub(r"\btt\d{7,10}\b", " ", clause, flags=re.IGNORECASE)
    cleaned = _PAREN_YEAR_RE.sub(" ", cleaned)

    cue_events: list[tuple[int, int, str]] = []
    for metric_key, pattern in _METRIC_CUE_PATTERNS:
        for m in pattern.finditer(cleaned):
            cue_events.append((m.start(), m.end(), metric_key))
    # Drop generic RT cue if a specific critics or audience cue appears in the same clause
    has_specific_rt = any(
        k in ("rotten_tomatoes_critics", "rotten_tomatoes_audience") for _, _, k in cue_events
    )
    filtered_cues: list[tuple[int, int, str]] = []
    for s, e, k in sorted(cue_events, key=lambda item: (item[0], -(item[1] - item[0]))):
        if k == "rt_generic":
            if has_specific_rt:
                continue
            k = "rotten_tomatoes_critics"
        if filtered_cues and s < filtered_cues[-1][1]:
            continue
        filtered_cues.append((s, e, k))

    events: list[tuple[int, str, Any]] = [(s, "cue", k) for s, _e, k in filtered_cues]
    direct_bindings: list[tuple[str, str]] = []

    for m in _RATING_NUM_TOKEN_RE.finditer(cleaned):
        num_str = m.group(1)
        suffix = (m.group(2) or "").strip().lower()
        # Skip overlaps with cue spans
        if any(cs <= m.start() < ce for cs, ce, _ in filtered_cues):
            continue
        if suffix and ("/10" in suffix.replace(" ", "") or "out of 10" in re.sub(r"\s+", " ", suffix)):
            direct_bindings.append(("imdb", num_str))
        elif suffix and ("/5" in suffix.replace(" ", "") or "out of 5" in re.sub(r"\s+", " ", suffix)):
            direct_bindings.append(("letterboxd", num_str))
        events.append((m.start(), "num", num_str))

    events.sort(key=lambda item: item[0])
    used_nums: set[int] = set()
    bindings: list[tuple[str, str]] = list(direct_bindings)

    for idx, (_pos, kind, payload) in enumerate(events):
        if kind != "cue":
            continue
        # Prefer immediately following unused number (e.g. 'Tomatometer of 42%', 'IMDb 7.5/10')
        if idx + 1 < len(events) and events[idx + 1][1] == "num" and (idx + 1) not in used_nums:
            used_nums.add(idx + 1)
            bindings.append((str(payload), str(events[idx + 1][2])))
        # Otherwise accept immediately preceding unused number (e.g. '42% Tomatometer', '89% audience')
        elif idx - 1 >= 0 and events[idx - 1][1] == "num" and (idx - 1) not in used_nums:
            used_nums.add(idx - 1)
            bindings.append((str(payload), str(events[idx - 1][2])))

    return bindings


def _text_binds_metric_to_value(
    text: str,
    rating_key: str,
    rating_value: int | float,
    *,
    is_int: bool,
) -> bool:
    for clause in _CLAUSE_SPLIT_RE.split(text):
        clause = clause.strip()
        if not clause:
            continue
        for bound_key, num_str in _extract_clause_metric_bindings(clause):
            if bound_key == rating_key and _numeric_matches_target(num_str, rating_value, is_int=is_int):
                return True
    return False


def _find_supporting_grounding_source(
    rating_key: str,
    rating_value: int | float,
    grounding_sources: list[dict[str, Any]],
    *,
    is_int: bool,
    film_titles: list[str],
    film_imdb_id: Optional[str],
    film_year: Optional[int] = None,
) -> Optional[str]:
    spec = _RATING_PLATFORM_SPECS.get(rating_key)
    if spec is None:
        return None
    _label, _lo, _hi, _is_int, domains = spec
    for src in grounding_sources:
        dom = _normalize_domain(str(src.get("domain") or ""))
        if not dom:
            continue
        matched_dom: Optional[str] = None
        for expected_dom in domains:
            if dom == expected_dom or dom.endswith("." + expected_dom):
                matched_dom = expected_dom
                break
        if matched_dom is None:
            continue

        if not _chunk_matches_film(
            src,
            expected_domain=matched_dom,
            film_titles=film_titles,
            film_imdb_id=film_imdb_id,
            film_year=film_year,
        ):
            continue

        evidence_texts: list[str] = []
        for seg in src.get("supported_segments") or []:
            if isinstance(seg, str) and seg.strip():
                evidence_texts.append(seg)
        snippet = str(src.get("snippet") or "").strip()
        if snippet:
            evidence_texts.append(snippet)
        title = str(src.get("title") or "").strip()
        if title:
            evidence_texts.append(title)

        for ev_text in evidence_texts:
            if _text_binds_metric_to_value(ev_text, rating_key, rating_value, is_int=is_int):
                return matched_dom
    return None


def validate_and_ground_ratings(
    raw_ratings: Any,
    *,
    grounding_sources: list[dict[str, Any]],
    identification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Validate platform-specific numeric score bounds and verify that every displayed rating
    is backed by an authoritative platform grounding source matching the actual film (including
    year when supplied), metric, and bound numeric value. Out-of-range or ungrounded scores are
    coerced to None (unknown) and listed in missing_sources.
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

    film_titles: list[str] = []
    film_imdb_id: Optional[str] = None
    film_year: Optional[int] = None
    if isinstance(identification, dict):
        for field in ("canonical_title", "original_title"):
            val = identification.get(field)
            if isinstance(val, str) and val.strip():
                film_titles.append(val.strip())
        raw_imdb = identification.get("imdb_id")
        if isinstance(raw_imdb, str) and raw_imdb.strip():
            film_imdb_id = raw_imdb.strip().lower()
        raw_year = identification.get("year")
        if isinstance(raw_year, int) and not isinstance(raw_year, bool) and 1888 <= raw_year <= 2100:
            film_year = raw_year

    def _add_missing(label: str) -> None:
        if not any(m.lower() == label.lower() for m in missing):
            missing.append(label)

    for key, (label, lo, hi, is_int, _domains) in _RATING_PLATFORM_SPECS.items():
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

        # Verify actual authoritative grounding source support for this film, metric, and value
        supporting_src = _find_supporting_grounding_source(
            key,
            valid_num,
            grounding_sources,
            is_int=is_int,
            film_titles=film_titles,
            film_imdb_id=film_imdb_id,
            film_year=film_year,
        )
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
            identification=parsed.get("identification") if isinstance(parsed.get("identification"), dict) else None,
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
