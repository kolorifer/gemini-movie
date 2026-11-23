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
from schemas import EvaluateRequest, EvaluationPayload, Identification

logger = logging.getLogger(__name__)

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_TIMEOUT_SECONDS = 6.0


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

    def _search_tmdb_id(self, title: str, year: Optional[int] = None) -> Optional[int]:
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

        best = select_best_tmdb_candidate(results, title, year)
        if best is None or best.get("id") is None:
            return None
        return int(best["id"])

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

    def _lookup_from_request_sync(self, request: EvaluateRequest) -> Optional[TmdbMovie]:
        imdb_id = extract_imdb_id(request.raw_text, request.extra_context)
        tmdb_id: Optional[int] = None
        if imdb_id:
            tmdb_id = self._find_tmdb_id_by_imdb(imdb_id)

        if tmdb_id is None:
            query_title, query_year = extract_tmdb_query(
                request.raw_text,
                hint_title=request.hint_title,
                hint_year=request.hint_year,
            )
            if query_title:
                tmdb_id = self._search_tmdb_id(query_title, query_year)

        if tmdb_id is None:
            return None
        return self._fetch_movie_details(tmdb_id)

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

    async def lookup_from_request(self, request: EvaluateRequest) -> Optional[TmdbMovie]:
        if not self.enabled:
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._lookup_from_request_sync, request),
                timeout=self.timeout_seconds + 2.0,
            )
        except (urllib.error.URLError, TimeoutError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            logger.warning("TMDB pre-lookup failed (falling back to Gemini-only): %s", exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected TMDB pre-lookup error: %s", exc)
            return None

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
