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
