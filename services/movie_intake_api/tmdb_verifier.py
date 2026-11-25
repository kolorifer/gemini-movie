from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from .security import (
    PrivateFileError,
    atomic_write_private_file,
    read_owner_private_regular_file,
    redact_secrets,
)
from .torrentleech import TL_FAILED_OUTCOMES, TL_INTEGRITY_CONFLICT_OUTCOMES

logger = logging.getLogger(__name__)

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_CACHE_TTL_SECONDS = 7 * 24 * 3600
MAX_TMDB_CACHE_BYTES = 4_000_000

_IMDB_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^(18[89]\d|19\d\d|20\d\d)$")
_STOP_TOKENS = frozenset(
    {
        "2160p", "1080p", "1080i", "720p", "576p", "480p", "4k", "uhd",
        "bluray", "blu", "bdrip", "brrip", "remux", "web", "webdl", "webrip",
        "hdtv", "dvdrip", "x264", "x265", "h264", "h265", "hevc", "avc", "av1",
        "10bit", "8bit", "hdr", "hdr10", "hdr10plus", "dv", "dovi", "dolby",
        "atmos", "dts", "dtshd", "truehd", "aac", "ac3", "eac3", "flac", "lpcm",
        "proper", "repack", "internal", "extended", "unrated", "remastered",
        "restored", "imax", "hybrid", "multi", "dual", "criterion",
    }
)


def normalize_title(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    spaced = re.sub(r"[^0-9a-zA-Z]+", " ", stripped).strip().lower()
    return re.sub(r"\s+", " ", spaced)


def parse_release_title_year(exact_release_name: str) -> tuple[Optional[str], Optional[int]]:
    cleaned = (exact_release_name or "").strip()
    if not cleaned:
        return None, None
    cleaned = re.sub(r"\.(?:mkv|mp4|m2ts|iso|torrent)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"-[A-Za-z0-9]{2,24}$", "", cleaned)
    tokens = [t for t in re.split(r"[\s._\[\]\(\)\{\}/\\,:;]+", cleaned) if t]

    title_tokens: list[str] = []
    year: Optional[int] = None
    for idx, tok in enumerate(tokens):
        low = tok.lower()
        if _YEAR_RE.match(low) and idx > 0:
            year = int(low)
            break
        if low in _STOP_TOKENS and title_tokens:
            break
        if _IMDB_RE.fullmatch(low):
            continue
        title_tokens.append(tok)

    title = " ".join(title_tokens).strip() or None
    return title, year


def _token_jaccard(a: str, b: str) -> float:
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _titles_match(release_title: str, candidate_titles: list[str]) -> bool:
    norm_rel = normalize_title(release_title)
    if not norm_rel:
        return False
    rel_tokens = norm_rel.split()
    for cand in candidate_titles:
        norm_cand = normalize_title(cand)
        if not norm_cand:
            continue
        if norm_rel == norm_cand:
            return True
        if _token_jaccard(norm_rel, norm_cand) >= 0.75:
            return True
        # Handle subtitle/article drops when >= 2 significant tokens match completely
        cand_tokens = norm_cand.split()
        if len(rel_tokens) >= 2 and len(cand_tokens) >= 2:
            shorter, longer = (
                (rel_tokens, cand_tokens)
                if len(rel_tokens) <= len(cand_tokens)
                else (cand_tokens, rel_tokens)
            )
            if set(shorter).issubset(set(longer)) and len(shorter) / len(longer) >= 0.65:
                return True
    return False


@dataclass(frozen=True)
class TmdbMovieRecord:
    tmdb_id: int
    title: str
    original_title: Optional[str] = None
    alternative_titles: tuple[str, ...] = ()
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    runtime_minutes: Optional[int] = None
    poster_path: Optional[str] = None

    def all_titles(self) -> list[str]:
        out = [self.title]
        if self.original_title and self.original_title not in out:
            out.append(self.original_title)
        for alt in self.alternative_titles:
            if alt and alt not in out:
                out.append(alt)
        return out


@dataclass(frozen=True)
class IdentityGateResult:
    status: str  # verified | evidence_review
    verified_movie: Optional[TmdbMovieRecord] = None
    conflict_reason: Optional[str] = None
    is_genuine_conflict: bool = False
    checks_passed: tuple[str, ...] = ()
    checks_failed: tuple[str, ...] = ()


class TmdbIndependentClient:
    """
    Independent TMDB verifier client owned by movie-intake-api with an owner-private
    0600 JSON cache (TMDB_CACHE_PATH). Performs bidirectional IMDb <-> TMDB movie checks.
    """

    def __init__(
        self,
        *,
        api_token: str,
        cache_path: Path,
        base_url: str = TMDB_API_BASE,
        timeout_seconds: float = 8.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_token = (api_token or "").strip()
        self.cache_path = cache_path
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client
        self._cache_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.api_token)

    def _is_bearer_token(self) -> bool:
        return len(self.api_token) > 64 or self.api_token.startswith("eyJ")

    def _load_cache_sync(self) -> dict[str, Any]:
        if not self.cache_path.exists():
            return {}
        try:
            raw = read_owner_private_regular_file(self.cache_path, max_bytes=MAX_TMDB_CACHE_BYTES)
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (PrivateFileError, UnicodeDecodeError, json.JSONDecodeError, OSError):
            return {}

    def _save_cache_sync(self, cache: dict[str, Any]) -> None:
        # Prune expired entries to keep cache lean on Ultra.cc
        now = time.time()
        pruned = {
            k: v
            for k, v in cache.items()
            if isinstance(v, dict) and float(v.get("ts") or 0) + TMDB_CACHE_TTL_SECONDS > now
        }
        payload = json.dumps(pruned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            atomic_write_private_file(self.cache_path, payload)
        except Exception as exc:
            logger.warning("Unable to persist TMDB cache: %s", redact_secrets(str(exc), max_chars=160))

    async def _cached_get(self, cache_key: str) -> Optional[dict[str, Any]]:
        async with self._cache_lock:
            cache = await asyncio.to_thread(self._load_cache_sync)
            entry = cache.get(cache_key)
            if isinstance(entry, dict):
                ts = float(entry.get("ts") or 0)
                if ts + TMDB_CACHE_TTL_SECONDS > time.time() and isinstance(entry.get("data"), dict):
                    return entry["data"]
        return None

    async def _cached_set(self, cache_key: str, data: dict[str, Any]) -> None:
        async with self._cache_lock:
            cache = await asyncio.to_thread(self._load_cache_sync)
            cache[cache_key] = {"ts": time.time(), "data": data}
            await asyncio.to_thread(self._save_cache_sync, cache)

    async def _get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        query = {k: str(v) for k, v in (params or {}).items() if v is not None and str(v) != ""}
        headers = {"Accept": "application/json", "User-Agent": "movie-intake-api/1.0"}
        if self._is_bearer_token():
            headers["Authorization"] = f"Bearer {self.api_token}"
        else:
            query["api_key"] = self.api_token

        url = f"{self.base_url}{path}"
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.get(url, params=query, headers=headers)
            if resp.status_code != 200:
                logger.warning("TMDB lookup returned HTTP %s for %s", resp.status_code, path)
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("TMDB request failed for %s: %s", path, redact_secrets(str(exc), max_chars=200))
            return None
        finally:
            if owns_client:
                await client.aclose()

    async def get_movie_by_tmdb_id(self, tmdb_id: int) -> Optional[TmdbMovieRecord]:
        cache_key = f"movie:{tmdb_id}"
        data = await self._cached_get(cache_key)
        if data is None:
            data = await self._get_json(
                f"/movie/{tmdb_id}",
                params={"append_to_response": "external_ids,alternative_titles"},
            )
            if data is not None:
                await self._cached_set(cache_key, data)
        if not data or not isinstance(data.get("id"), int):
            return None

        title = str(data.get("title") or data.get("original_title") or "").strip()
        if not title:
            return None
        orig = str(data.get("original_title") or "").strip() or None

        rel_date = str(data.get("release_date") or "")
        year = int(rel_date[:4]) if len(rel_date) >= 4 and _YEAR_RE.match(rel_date[:4]) else None

        ext_ids = data.get("external_ids")
        ext_imdb = ext_ids.get("imdb_id") if isinstance(ext_ids, dict) else None
        raw_imdb = str(ext_imdb or data.get("imdb_id") or "").strip().lower()
        imdb_id = raw_imdb if _IMDB_RE.fullmatch(raw_imdb) else None

        runtime_raw = data.get("runtime")
        runtime_minutes = (
            int(runtime_raw)
            if isinstance(runtime_raw, int) and not isinstance(runtime_raw, bool) and 1 <= runtime_raw <= 1440
            else None
        )

        alt_titles_list: list[str] = []
        alt_container = data.get("alternative_titles")
        if isinstance(alt_container, dict):
            titles_arr = alt_container.get("titles") or alt_container.get("results") or []
            if isinstance(titles_arr, list):
                for item in titles_arr:
                    if isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip():
                        alt_titles_list.append(item["title"].strip())

        poster_path = str(data.get("poster_path") or "").strip() or None
        return TmdbMovieRecord(
            tmdb_id=int(data["id"]),
            title=title,
            original_title=orig,
            alternative_titles=tuple(alt_titles_list[:30]),
            year=year,
            imdb_id=imdb_id,
            runtime_minutes=runtime_minutes,
            poster_path=poster_path,
        )

    async def find_movies_by_imdb_id(self, imdb_id: str) -> Optional[list[int]]:
        """
        Return list of TMDB movie IDs matching imdb_id via /find/{imdb_id}?external_source=imdb_id,
        or None if TMDB lookup failed.
        """
        clean = (imdb_id or "").strip().lower()
        if not _IMDB_RE.fullmatch(clean):
            return []
        cache_key = f"find:{clean}"
        data = await self._cached_get(cache_key)
        if data is None:
            data = await self._get_json(f"/find/{clean}", params={"external_source": "imdb_id"})
            if data is not None:
                await self._cached_set(cache_key, data)
        if data is None:
            return None
        movie_results = data.get("movie_results")
        if not isinstance(movie_results, list):
            return []
        ids: list[int] = []
        for item in movie_results:
            if isinstance(item, dict) and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool):
                if int(item["id"]) not in ids:
                    ids.append(int(item["id"]))
        return ids


async def run_independent_identity_gate(
    *,
    tmdb_client: TmdbIndependentClient,
    exact_release_name: str,
    proposed_imdb_id: Optional[str],
    proposed_tmdb_id: Optional[int],
    proposal_confidence: Optional[str],
    from_evaluator: bool,
    tl_applicable: bool,
    tl_outcome: str,
    observed_meta_imdb: Optional[str],
    observed_meta_tmdb: Optional[int],
    observed_tl_imdb: Optional[str],
    file_runtime_minutes: Optional[int],
) -> IdentityGateResult:
    """
    Independent Identity Verification Gate (docs/ARCHITECTURE.md §5.2):
    All 6 checks must pass before ordinary Grab/Watched buttons may be attached.
    """
    passed: list[str] = []
    failed: list[str] = []

    # Check 1: Source / TorrentLeech integrity
    if tl_applicable and tl_outcome in TL_FAILED_OUTCOMES:
        failed.append(f"tl_integrity_failed:{tl_outcome}")
        is_conflict = tl_outcome in TL_INTEGRITY_CONFLICT_OUTCOMES
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"TorrentLeech lookup outcome '{tl_outcome}' cannot authorize ordinary actions",
            is_genuine_conflict=is_conflict,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )
    passed.append("source_tl_integrity")

    # Check 2: Evaluator confidence & canonical IDs
    if from_evaluator and (proposal_confidence or "").strip().lower() != "high":
        failed.append(f"low_evaluator_confidence:{proposal_confidence or 'none'}")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"Evaluator match confidence is '{proposal_confidence or 'unknown'}' (requires 'high')",
            is_genuine_conflict=False,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )

    clean_imdb = (
        proposed_imdb_id.strip().lower()
        if proposed_imdb_id and _IMDB_RE.fullmatch(proposed_imdb_id.strip())
        else None
    )
    clean_tmdb = (
        int(proposed_tmdb_id)
        if isinstance(proposed_tmdb_id, int) and not isinstance(proposed_tmdb_id, bool) and proposed_tmdb_id > 0
        else None
    )
    if not clean_imdb and clean_tmdb is None:
        failed.append("missing_canonical_ids")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason="No canonical IMDb or TMDB ID available for independent verification",
            is_genuine_conflict=False,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )
    passed.append("confidence_and_id_format")

    # Check 3: Independent bidirectional IMDb <-> TMDB movie agreement
    if not tmdb_client.enabled:
        failed.append("tmdb_client_disabled")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason="Independent TMDB verification unavailable (TMDB_API_TOKEN not configured)",
            is_genuine_conflict=False,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )

    resolved_tmdb_id = clean_tmdb
    if resolved_tmdb_id is None and clean_imdb:
        found_ids = await tmdb_client.find_movies_by_imdb_id(clean_imdb)
        if found_ids is None:
            failed.append("tmdb_find_failed")
            return IdentityGateResult(
                status="evidence_review",
                conflict_reason="TMDB /find lookup failed",
                is_genuine_conflict=False,
                checks_passed=tuple(passed),
                checks_failed=tuple(failed),
            )
        if len(found_ids) != 1:
            failed.append(f"tmdb_find_ambiguous_or_empty:{len(found_ids)}")
            return IdentityGateResult(
                status="evidence_review",
                conflict_reason=f"IMDb {clean_imdb} did not resolve uniquely to one TMDB movie (found {len(found_ids)})",
                is_genuine_conflict=len(found_ids) > 1,
                checks_passed=tuple(passed),
                checks_failed=tuple(failed),
            )
        resolved_tmdb_id = found_ids[0]

    assert resolved_tmdb_id is not None
    movie = await tmdb_client.get_movie_by_tmdb_id(resolved_tmdb_id)
    if movie is None or not movie.imdb_id:
        failed.append("tmdb_movie_lookup_failed_or_missing_imdb")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"TMDB movie {resolved_tmdb_id} could not be independently verified with an IMDb ID",
            is_genuine_conflict=False,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )

    if clean_imdb and movie.imdb_id != clean_imdb:
        failed.append("imdb_tmdb_mismatch")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"TMDB movie {movie.tmdb_id} links to {movie.imdb_id}, contradicting proposed {clean_imdb}",
            is_genuine_conflict=True,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )

    # Confirm reverse /find/{imdb_id} agrees uniquely with movie.tmdb_id
    reverse_ids = await tmdb_client.find_movies_by_imdb_id(movie.imdb_id)
    if reverse_ids is None:
        failed.append("tmdb_reverse_find_failed")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"Reverse TMDB lookup for {movie.imdb_id} failed transiently",
            is_genuine_conflict=False,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )
    if len(reverse_ids) != 1 or reverse_ids[0] != movie.tmdb_id:
        failed.append("imdb_reverse_find_disagreement")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"Reverse TMDB lookup for {movie.imdb_id} did not uniquely match TMDB {movie.tmdb_id}",
            is_genuine_conflict=True,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )
    passed.append("imdb_tmdb_bidirectional_agreement")

    # Check 4: Independent release title & year check against exact_release_name
    release_title, release_year = parse_release_title_year(exact_release_name)
    if not release_title or not _titles_match(release_title, movie.all_titles()):
        failed.append("release_title_mismatch")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"Release title '{release_title or ''}' does not match TMDB '{movie.title}'",
            is_genuine_conflict=True,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )
    if release_year is not None and movie.year is not None and abs(release_year - movie.year) > 1:
        failed.append("release_year_mismatch")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason=f"Release year {release_year} conflicts with TMDB year {movie.year} for '{movie.title}'",
            is_genuine_conflict=True,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )
    passed.append("release_title_year_match")

    # Check 5: Actual MediaInfo file runtime check (never catalogue runtime)
    if file_runtime_minutes is not None and movie.runtime_minutes is not None:
        tolerance = max(12, round(0.12 * movie.runtime_minutes))
        if abs(file_runtime_minutes - movie.runtime_minutes) > tolerance:
            failed.append("mediainfo_runtime_mismatch")
            return IdentityGateResult(
                status="evidence_review",
                conflict_reason=(
                    f"MediaInfo file runtime {file_runtime_minutes}m conflicts with "
                    f"TMDB runtime {movie.runtime_minutes}m (tolerance +/-{tolerance}m)"
                ),
                is_genuine_conflict=True,
                checks_passed=tuple(passed),
                checks_failed=tuple(failed),
            )
    passed.append("mediainfo_runtime_check")

    # Check 6: Local conflict checks across observed autobrr & TL source IDs
    conflict_reasons: list[str] = []
    if observed_meta_imdb and observed_meta_imdb.lower() != movie.imdb_id:
        conflict_reasons.append(
            f"autobrr meta_imdb {observed_meta_imdb.lower()} != verified {movie.imdb_id}"
        )
    if observed_meta_tmdb is not None and observed_meta_tmdb != movie.tmdb_id:
        conflict_reasons.append(
            f"autobrr meta_tmdb {observed_meta_tmdb} != verified {movie.tmdb_id}"
        )
    if observed_tl_imdb and observed_tl_imdb.lower() != movie.imdb_id:
        conflict_reasons.append(
            f"TorrentLeech listing IMDb {observed_tl_imdb.lower()} != verified {movie.imdb_id}"
        )
    if observed_meta_imdb and observed_tl_imdb and observed_meta_imdb.lower() != observed_tl_imdb.lower():
        conflict_reasons.append(
            f"autobrr meta_imdb {observed_meta_imdb.lower()} != TL listing IMDb {observed_tl_imdb.lower()}"
        )

    if conflict_reasons:
        failed.append("local_source_id_conflict")
        return IdentityGateResult(
            status="evidence_review",
            conflict_reason="; ".join(conflict_reasons),
            is_genuine_conflict=True,
            checks_passed=tuple(passed),
            checks_failed=tuple(failed),
        )
    passed.append("local_conflict_check")

    return IdentityGateResult(
        status="verified",
        verified_movie=movie,
        conflict_reason=None,
        is_genuine_conflict=False,
        checks_passed=tuple(passed),
        checks_failed=(),
    )
