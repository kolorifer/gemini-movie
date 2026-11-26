from __future__ import annotations

import asyncio
import hashlib
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
from schemas import (
    CatalogueIdentification,
    EvaluateRequest,
    EvaluationPayload,
    Identification,
    ModelIdentificationSnapshot,
    TorrentInfo,
)

logger = logging.getLogger(__name__)

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_TIMEOUT_SECONDS = 6.0

_TRACKER_SECRET_RE = re.compile(
    r"https?://\S+|magnet:\S+|www\.\S+"
    r"|/webhook/[^/\s?\"']+"
    r"|/action/[^/\s?\"']+"
    r"|bot\d{5,15}:[A-Za-z0-9_-]{20,}"
    r"|\b\d{5,15}:[A-Za-z0-9_-]{25,}\b"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]*)?"
    r"|\b(?:passkey|authkey|api[_-]?key|apikey|secret|token|password|passwd|authorization|cookie|set-cookie|x-internal-token|x-api-key|key)\s*[:=]\s*[^\s\"'&;,]+"
    r"|\b(?:passkey|authkey|api[_-]?key|apikey|x-internal-token|x-api-key)\s+[^\s\"'&;,]+"
    r"|\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|\b[A-Fa-f0-9]{32,}\b"
    r"|\b[A-Za-z0-9_]{48,}\b",
    re.IGNORECASE,
)
_IMDB_ID_STRICT_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)
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


def sanitize_tracker_text(raw: Optional[str], *, max_chars: Optional[int] = 900) -> str:
    """Scrub URLs, passkeys, cookies, API keys, and control chars from text."""
    if not raw:
        return ""
    scrubbed = _TRACKER_SECRET_RE.sub("[redacted]", raw)
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in scrubbed)
    collapsed = re.sub(r"\s+", " ", cleaned).strip()
    if max_chars is not None and len(collapsed) > max_chars:
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
    alternative_titles: tuple[str, ...] = ()

    def all_titles(self) -> list[str]:
        out = [self.canonical_title]
        if self.original_title and self.original_title not in out:
            out.append(self.original_title)
        for alt in self.alternative_titles:
            if alt and alt not in out:
                out.append(alt)
        return out

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


_ROMAN_TO_INT: dict[str, int] = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
    "xi": 11,
    "xii": 12,
}
_WORD_TO_INT: dict[str, int] = {
    "one": 1,
    "first": 1,
    "two": 2,
    "second": 2,
    "three": 3,
    "third": 3,
    "four": 4,
    "fourth": 4,
    "five": 5,
    "fifth": 5,
    "six": 6,
    "sixth": 6,
    "seven": 7,
    "seventh": 7,
    "eight": 8,
    "eighth": 8,
    "nine": 9,
    "ninth": 9,
    "ten": 10,
    "tenth": 10,
}
_INSTALLMENT_PREFIXES = frozenset({"part", "pt", "chapter", "ch", "vol", "volume", "book", "act", "episode", "ep"})
_SEQUEL_SUBTITLE_TOKENS = frozenset(
    {
        "part",
        "pt",
        "chapter",
        "vol",
        "volume",
        "book",
        "sequel",
        "returns",
        "reloaded",
        "revolutions",
        "resurrection",
        "resurrections",
        "begins",
        "forever",
        "revenge",
        "rising",
        "reborn",
        "awakens",
        "origins",
        "redux",
        "final",
    }
)


def _extract_installment_signature(norm_title: str) -> tuple[int, ...]:
    tokens = norm_title.split()
    if not tokens:
        return ()
    markers: list[int] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in _INSTALLMENT_PREFIXES and idx + 1 < len(tokens):
            nxt = tokens[idx + 1]
            if nxt.isdigit():
                markers.append(int(nxt))
                idx += 2
                continue
            if nxt in _ROMAN_TO_INT:
                markers.append(_ROMAN_TO_INT[nxt])
                idx += 2
                continue
            if nxt in _WORD_TO_INT:
                markers.append(_WORD_TO_INT[nxt])
                idx += 2
                continue
        if tok.isdigit():
            markers.append(int(tok))
        elif tok in _ROMAN_TO_INT and (tok != "i" or (len(tokens) >= 2 and idx == len(tokens) - 1)):
            markers.append(_ROMAN_TO_INT[tok])
        idx += 1
    return tuple(markers)


def _canonicalize_installment_tokens(norm_title: str) -> str:
    tokens = norm_title.split()
    out: list[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in _INSTALLMENT_PREFIXES and idx + 1 < len(tokens):
            nxt = tokens[idx + 1]
            if nxt.isdigit():
                out.extend([tok, str(int(nxt))])
                idx += 2
                continue
            if nxt in _ROMAN_TO_INT:
                out.extend([tok, str(_ROMAN_TO_INT[nxt])])
                idx += 2
                continue
            if nxt in _WORD_TO_INT:
                out.extend([tok, str(_WORD_TO_INT[nxt])])
                idx += 2
                continue
        if tok in _ROMAN_TO_INT and (tok != "i" or (len(tokens) >= 2 and idx == len(tokens) - 1)):
            out.append(str(_ROMAN_TO_INT[tok]))
        else:
            out.append(tok)
        idx += 1
    return " ".join(out)


def _titles_compatible(norm_a: str, norm_b: str) -> bool:
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    if _extract_installment_signature(norm_a) != _extract_installment_signature(norm_b):
        return False
    canon_a = _canonicalize_installment_tokens(norm_a)
    canon_b = _canonicalize_installment_tokens(norm_b)
    if canon_a == canon_b:
        return True
    ta = set(canon_a.split())
    tb = set(canon_b.split())
    if (ta ^ tb) & _SEQUEL_SUBTITLE_TOKENS:
        return False
    return _token_jaccard(canon_a, canon_b) >= 0.6


def select_best_tmdb_candidate(
    results: list[dict[str, Any]],
    query_title: str,
    query_year: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """
    Score TMDB search results and return the top candidate only when confidence is high
    enough to avoid false-positive overrides (especially across remakes/different years/sequels).
    """
    norm_query = _normalize_title_for_compare(query_title)
    if not norm_query:
        return None
    query_sig = _extract_installment_signature(norm_query)

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
            compat_scores: list[float] = []
            for cand_norm in (norm_title, norm_orig):
                if not cand_norm:
                    continue
                if _extract_installment_signature(cand_norm) != query_sig:
                    continue
                if (set(norm_query.split()) ^ set(cand_norm.split())) & _SEQUEL_SUBTITLE_TOKENS:
                    continue
                compat_scores.append(_token_jaccard(norm_query, cand_norm))
            sim = max(compat_scores) if compat_scores else 0.0
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
    """
    Compare full director names (split on co-director separators).
    NEVER match two different directors who share only a first name (e.g. 'David Fincher' vs 'David Lynch').
    """
    if not a or not b:
        return False

    def _parse_people(raw: str) -> list[list[str]]:
        people: list[list[str]] = []
        for part in re.split(r"\s*(?:,|&|\band\b|/)\s*", raw, flags=re.IGNORECASE):
            norm = _normalize_title_for_compare(part)
            toks = [t for t in norm.split() if t]
            if toks and norm not in ("unknown", "n a", "na", "none"):
                people.append(toks)
        return people

    people_a = _parse_people(a)
    people_b = _parse_people(b)
    if not people_a or not people_b:
        return False

    for ta in people_a:
        for tb in people_b:
            if ta == tb:
                return True
            if len(ta) >= 2 and len(tb) >= 2 and ta[-1] == tb[-1] and len(ta[-1]) >= 2:
                if ta[0] == tb[0] or (
                    (len(ta[0]) == 1 or len(tb[0]) == 1) and ta[0][0] == tb[0][0]
                ):
                    return True
    return False


def _tmdb_incompatibility_reason(ident: Identification, tmdb: TmdbMovie) -> Optional[str]:
    """Return None if TMDB record is compatible with model identification, else a specific conflict reason."""
    if ident.imdb_id and tmdb.imdb_id:
        if ident.imdb_id.strip().lower() != tmdb.imdb_id.strip().lower():
            return f"contradictory IMDb IDs (model={ident.imdb_id.strip().lower()}, tmdb={tmdb.imdb_id.strip().lower()})"

    if ident.tmdb_id is not None and ident.tmdb_id != tmdb.tmdb_id:
        return f"contradictory TMDB IDs (model={ident.tmdb_id}, tmdb={tmdb.tmdb_id})"

    if tmdb.year is not None and abs(ident.year - tmdb.year) > 1:
        return f"year mismatch (model={ident.year}, tmdb={tmdb.year})"

    ident_titles = [
        _normalize_title_for_compare(t)
        for t in (ident.canonical_title, ident.original_title or "")
        if t and t.strip()
    ]
    tmdb_titles = [
        _normalize_title_for_compare(t)
        for t in tmdb.all_titles()
        if t and t.strip()
    ]
    title_match = any(
        _titles_compatible(it, tt)
        for it in ident_titles
        for tt in tmdb_titles
    )

    ident_dir_clean = (ident.director or "").strip()
    tmdb_dir_clean = (tmdb.director or "").strip()
    both_have_directors = bool(
        ident_dir_clean
        and ident_dir_clean.lower() not in ("unknown", "n/a", "none")
        and tmdb_dir_clean
        and tmdb_dir_clean.lower() not in ("unknown", "n/a", "none")
    )
    director_match = _directors_overlap(ident_dir_clean, tmdb_dir_clean)

    # Matching IMDb IDs confirm identity unless contradictory TMDB ID or completely unrelated title+director
    if ident.imdb_id and tmdb.imdb_id and ident.imdb_id.strip().lower() == tmdb.imdb_id.strip().lower():
        if both_have_directors and not director_match and not title_match:
            return (
                f"title and director mismatch despite IMDb match "
                f"(model='{ident.canonical_title}' by '{ident.director}', tmdb='{tmdb.canonical_title}' by '{tmdb.director}')"
            )
        return None

    # Without matching comparable IMDb IDs, require meaningful title/alias match AND compatible full director
    if not title_match:
        return (
            f"title mismatch (model='{ident.canonical_title}', tmdb='{tmdb.canonical_title}')"
        )
    if both_have_directors and not director_match:
        return (
            f"director mismatch (model='{ident.director}', tmdb='{tmdb.director}')"
        )
    return None


def is_tmdb_compatible(ident: Identification, tmdb: TmdbMovie) -> bool:
    """Ensure TMDB metadata refers to the same film as Gemini's identification."""
    return _tmdb_incompatibility_reason(ident, tmdb) is None


def apply_tmdb_verification(
    payload: EvaluationPayload,
    tmdb: Optional[TmdbMovie],
) -> EvaluationPayload:
    """
    Preserve the model's original identification separately from catalogue identification.
    Deterministically fill verified movie fields ONLY when TMDB metadata is strictly compatible;
    when incompatible, keep model fields intact, record an explicit catalogue conflict, and
    never substitute another film's IDs onto the model's assessment.
    """
    ident = payload.identification
    model_snap = payload.model_identification or ModelIdentificationSnapshot(
        canonical_title=ident.canonical_title,
        original_title=ident.original_title,
        year=ident.year,
        director=ident.director,
        imdb_id=ident.imdb_id,
        tmdb_id=ident.tmdb_id,
    )

    if tmdb is None:
        return payload.model_copy(update={"model_identification": model_snap})

    incompat_reason = _tmdb_incompatibility_reason(ident, tmdb)
    if incompat_reason is not None:
        logger.warning(
            "Skipping TMDB merge due to conflict (%s): gemini=(%r, %s, %s, tmdb=%s, dir=%r) vs tmdb=(%r, %s, %s, tmdb=%s, dir=%r)",
            incompat_reason,
            ident.canonical_title,
            ident.year,
            ident.imdb_id,
            ident.tmdb_id,
            ident.director,
            tmdb.canonical_title,
            tmdb.year,
            tmdb.imdb_id,
            tmdb.tmdb_id,
            tmdb.director,
        )
        conflict_note = f"Catalogue verification conflict: {incompat_reason}"
        existing_note = (ident.disambiguation_note or "").strip()
        merged_note = f"{existing_note}; {conflict_note}" if existing_note else conflict_note
        updated_ident = ident.model_copy(
            update={
                "match_confidence": "low" if ident.match_confidence == "high" else ident.match_confidence,
                "disambiguation_note": merged_note,
            }
        )
        cat_ident = CatalogueIdentification(
            canonical_title=tmdb.canonical_title,
            original_title=tmdb.original_title,
            year=tmdb.year,
            director=tmdb.director,
            imdb_id=tmdb.imdb_id,
            tmdb_id=tmdb.tmdb_id,
            runtime_minutes=tmdb.runtime_minutes,
            compatible=False,
            conflict_reason=incompat_reason,
        )
        return payload.model_copy(
            update={
                "identification": updated_ident,
                "model_identification": model_snap,
                "catalogue_identification": cat_ident,
            }
        )

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
    cat_ident = CatalogueIdentification(
        canonical_title=tmdb.canonical_title,
        original_title=tmdb.original_title,
        year=tmdb.year,
        director=tmdb.director,
        imdb_id=tmdb.imdb_id,
        tmdb_id=tmdb.tmdb_id,
        runtime_minutes=tmdb.runtime_minutes,
        compatible=True,
        conflict_reason=None,
    )
    return payload.model_copy(
        update={
            "identification": updated_ident,
            "model_identification": model_snap,
            "catalogue_identification": cat_ident,
        }
    )


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
        query_sig = _extract_installment_signature(norm_query)
        cand_norms = [
            _normalize_title_for_compare(t)
            for t in movie.all_titles()
            if t and t.strip()
        ]
        title_matches = False
        for cn in cand_norms:
            if not cn:
                continue
            if norm_query == cn:
                title_matches = True
                break
            if _extract_installment_signature(cn) != query_sig:
                continue
            if (set(norm_query.split()) ^ set(cn.split())) & _SEQUEL_SUBTITLE_TOKENS:
                continue
            if _token_jaccard(norm_query, cn) >= 0.45:
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
        clean_warn = sanitize_tracker_text(conflict_warning, max_chars=400)
        if clean_warn:
            lines.append(
                f"- CONFLICT WARNING: {clean_warn} "
                "Treat the tracker's ID and synopsis as UNTRUSTED; verify the true film from the release string, MediaInfo facts, and Google Search, and note the tracker mismatch in disambiguation_note."
            )

    if torrent_info is not None:
        meta_parts: list[str] = []
        if torrent_info.indexer:
            v = sanitize_tracker_text(torrent_info.indexer, max_chars=80)
            if v:
                meta_parts.append(f"indexer={v}")
        if torrent_info.category:
            v = sanitize_tracker_text(torrent_info.category, max_chars=120)
            if v:
                meta_parts.append(f"category={v}")
        if torrent_info.resolution:
            v = sanitize_tracker_text(torrent_info.resolution, max_chars=40)
            if v:
                meta_parts.append(f"resolution={v}")
        if torrent_info.source:
            v = sanitize_tracker_text(torrent_info.source, max_chars=60)
            if v:
                meta_parts.append(f"source={v}")
        if torrent_info.media_processing:
            v = sanitize_tracker_text(torrent_info.media_processing, max_chars=60)
            if v:
                meta_parts.append(f"processing={v}")
        if torrent_info.dynamic_range:
            dr_clean = [
                sanitize_tracker_text(d, max_chars=40)
                for d in torrent_info.dynamic_range
            ]
            dr_clean = [d for d in dr_clean if d]
            if dr_clean:
                meta_parts.append("dynamic_range=" + "+".join(dr_clean))
        if torrent_info.group:
            v = sanitize_tracker_text(torrent_info.group, max_chars=80)
            if v:
                meta_parts.append(f"group={v}")
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
            clean_langs = [
                sanitize_tracker_text(lang, max_chars=40)
                for lang in torrent_info.audio_languages
            ]
            clean_langs = [lang for lang in clean_langs if lang]
            if clean_langs:
                lines.append("- MediaInfo audio languages: " + ", ".join(clean_langs))

        id_label = "untrusted/conflicting" if conflict_warning else "tracker-reported, cross-check"
        if torrent_info.imdb_id:
            clean_imdb = torrent_info.imdb_id.strip().lower()
            if _IMDB_ID_STRICT_RE.fullmatch(clean_imdb):
                lines.append(f"- Tracker IMDb ID ({id_label}): {clean_imdb}")
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


def compute_evidence_key(request: EvaluateRequest) -> str:
    """
    Build a deterministic, secret-free cache scope digest covering all evidence
    fields beyond raw_text/quality_key (hints, extra_context, and torrent_info).
    Returns '' when no supplemental evidence is provided so plain release lookups
    preserve cross-language/alias cache sharing.
    """
    evidence: dict[str, Any] = {}

    if request.hint_title and request.hint_title.strip():
        evidence["hint_title"] = re.sub(r"\s+", " ", request.hint_title.strip().lower())
    if request.hint_year is not None:
        evidence["hint_year"] = int(request.hint_year)
    if request.extra_context and request.extra_context.strip():
        clean_extra = sanitize_tracker_text(request.extra_context, max_chars=None).lower()
        if clean_extra:
            evidence["extra_context"] = clean_extra

    tinfo = request.torrent_info
    if tinfo is not None:
        t_dict: dict[str, Any] = {}
        if tinfo.indexer and tinfo.indexer.strip():
            t_dict["indexer"] = tinfo.indexer.strip().lower()
        if tinfo.category and tinfo.category.strip():
            t_dict["category"] = tinfo.category.strip().lower()
        if tinfo.imdb_id and tinfo.imdb_id.strip():
            t_dict["imdb_id"] = tinfo.imdb_id.strip().lower()
        if tinfo.tmdb_id is not None:
            t_dict["tmdb_id"] = int(tinfo.tmdb_id)
        effective_runtime = tinfo.runtime_minutes or extract_nfo_runtime_minutes(tinfo.nfo_text)
        if effective_runtime is not None:
            t_dict["runtime_minutes"] = int(effective_runtime)
        if tinfo.audio_languages:
            langs = sorted({lang.strip().lower() for lang in tinfo.audio_languages if lang.strip()})
            if langs:
                t_dict["audio_languages"] = langs
        if tinfo.resolution and tinfo.resolution.strip():
            t_dict["resolution"] = tinfo.resolution.strip().lower()
        if tinfo.source and tinfo.source.strip():
            t_dict["source"] = tinfo.source.strip().lower()
        if tinfo.dynamic_range:
            dr_tags = sorted({tag.strip().lower() for tag in tinfo.dynamic_range if tag.strip()})
            if dr_tags:
                t_dict["dynamic_range"] = dr_tags
        if tinfo.media_processing and tinfo.media_processing.strip():
            t_dict["media_processing"] = tinfo.media_processing.strip().lower()
        if tinfo.group and tinfo.group.strip():
            t_dict["group"] = tinfo.group.strip().lower()
        if tinfo.size_gb is not None:
            t_dict["size_gb"] = round(float(tinfo.size_gb), 2)
        if tinfo.freeleech is not None:
            t_dict["freeleech"] = bool(tinfo.freeleech)
        clean_desc = sanitize_tracker_text(tinfo.description, max_chars=None)
        if clean_desc:
            t_dict["description"] = clean_desc
        clean_nfo = sanitize_tracker_text(tinfo.nfo_text, max_chars=None)
        if clean_nfo:
            t_dict["nfo_text"] = clean_nfo
        if t_dict:
            evidence["torrent_info"] = t_dict

    if not evidence:
        return ""

    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "ev1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


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
            params={"append_to_response": "credits,external_ids,alternative_titles"},
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

        alt_titles: list[str] = []
        alt_container = data.get("alternative_titles")
        if isinstance(alt_container, dict):
            titles_arr = alt_container.get("titles") or alt_container.get("results") or []
            if isinstance(titles_arr, list):
                for item in titles_arr:
                    if isinstance(item, dict) and isinstance(item.get("title"), str) and item["title"].strip():
                        alt_titles.append(item["title"].strip())

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
            alternative_titles=tuple(alt_titles[:30]),
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

        # 1) Check caller-provided explicit IMDb ID in raw_text / extra_context
        # Cross-check against release title/year/MediaInfo AND any tracker-supplied IDs.
        tracker_conflict: Optional[str] = None
        direct_imdb = extract_imdb_id(request.raw_text, request.extra_context)
        if direct_imdb:
            direct_tmdb_id = self._find_tmdb_id_by_imdb(direct_imdb)
            if direct_tmdb_id is not None:
                direct_movie = self._fetch_movie_details(direct_tmdb_id)
                id_disagreements: list[str] = []
                if direct_movie.imdb_id and direct_movie.imdb_id.lower() != direct_imdb.lower():
                    id_disagreements.append(
                        f"direct IMDb {direct_imdb} resolved to TMDB {direct_movie.tmdb_id} whose IMDb is {direct_movie.imdb_id}"
                    )
                if tinfo is not None and tinfo.imdb_id and tinfo.imdb_id.lower() != direct_imdb.lower():
                    id_disagreements.append(
                        f"direct IMDb {direct_imdb} conflicts with tracker IMDb {tinfo.imdb_id}"
                    )
                if tinfo is not None and tinfo.tmdb_id is not None and tinfo.tmdb_id != direct_movie.tmdb_id:
                    id_disagreements.append(
                        f"direct IMDb {direct_imdb} (TMDB {direct_movie.tmdb_id}) conflicts with tracker TMDB {tinfo.tmdb_id}"
                    )
                consistency_conflict = check_tracker_movie_consistency(
                    direct_movie,
                    query_title=query_title,
                    query_year=query_year,
                    nfo_runtime=nfo_runtime,
                    audio_languages=audio_languages,
                )
                if not id_disagreements and consistency_conflict is None:
                    return TmdbLookupResult(
                        movie=direct_movie,
                        tracker_conflict=None,
                        trusted_for_cache=True,
                    )
                conflict_msg = "; ".join(
                    p for p in (*id_disagreements, consistency_conflict) if p
                )
                logger.warning("Rejecting conflicting direct IMDb TMDB candidate: %s", conflict_msg)
                tracker_conflict = conflict_msg

        # 2) Check tracker-supplied TMDB/IMDb IDs — when BOTH are supplied, resolve AND compare BOTH!
        if tinfo is not None and (tinfo.tmdb_id is not None or tinfo.imdb_id):
            supplied_tmdb_id: Optional[int] = tinfo.tmdb_id
            supplied_imdb_id: Optional[str] = tinfo.imdb_id.strip().lower() if tinfo.imdb_id else None

            if supplied_tmdb_id is not None and supplied_imdb_id:
                movie_by_tmdb = self._fetch_movie_details(supplied_tmdb_id)
                imdb_resolved_tmdb_id = self._find_tmdb_id_by_imdb(supplied_imdb_id)
                movie_by_imdb: Optional[TmdbMovie] = None
                if imdb_resolved_tmdb_id is not None:
                    movie_by_imdb = (
                        movie_by_tmdb
                        if imdb_resolved_tmdb_id == supplied_tmdb_id
                        else self._fetch_movie_details(imdb_resolved_tmdb_id)
                    )

                pair_conflicts: list[str] = []
                if movie_by_tmdb.imdb_id and movie_by_tmdb.imdb_id.lower() != supplied_imdb_id:
                    pair_conflicts.append(
                        f"tracker tmdb_id={supplied_tmdb_id} ('{movie_by_tmdb.canonical_title}', {movie_by_tmdb.year}) "
                        f"links to imdb={movie_by_tmdb.imdb_id}, contradicting tracker imdb_id={supplied_imdb_id}"
                    )
                if imdb_resolved_tmdb_id is None:
                    pair_conflicts.append(
                        f"tracker imdb_id={supplied_imdb_id} could not be resolved to tracker tmdb_id={supplied_tmdb_id}"
                    )
                elif imdb_resolved_tmdb_id != supplied_tmdb_id:
                    other_desc = (
                        f"'{movie_by_imdb.canonical_title}' ({movie_by_imdb.year})"
                        if movie_by_imdb is not None
                        else f"tmdb_id={imdb_resolved_tmdb_id}"
                    )
                    pair_conflicts.append(
                        f"tracker imdb_id={supplied_imdb_id} resolved to tmdb_id={imdb_resolved_tmdb_id} ({other_desc}), "
                        f"contradicting tracker tmdb_id={supplied_tmdb_id} ('{movie_by_tmdb.canonical_title}', {movie_by_tmdb.year})"
                    )

                if pair_conflicts:
                    conflict_msg = "Conflicting tracker IDs: " + "; ".join(pair_conflicts)
                    logger.warning("Rejecting conflicting tracker ID pair: %s", conflict_msg)
                    tracker_conflict = (
                        f"{tracker_conflict}; {conflict_msg}" if tracker_conflict else conflict_msg
                    )
                else:
                    consistency_conflict = check_tracker_movie_consistency(
                        movie_by_tmdb,
                        query_title=query_title,
                        query_year=query_year,
                        nfo_runtime=nfo_runtime,
                        audio_languages=audio_languages,
                    )
                    if consistency_conflict is None and tracker_conflict is None:
                        return TmdbLookupResult(
                            movie=movie_by_tmdb,
                            tracker_conflict=None,
                            trusted_for_cache=True,
                        )
                    if consistency_conflict is not None:
                        logger.warning("Rejecting tracker-supplied TMDB candidate: %s", consistency_conflict)
                        tracker_conflict = (
                            f"{tracker_conflict}; {consistency_conflict}"
                            if tracker_conflict
                            else consistency_conflict
                        )
            else:
                tracker_tmdb_id: Optional[int] = supplied_tmdb_id
                if tracker_tmdb_id is None and supplied_imdb_id:
                    tracker_tmdb_id = self._find_tmdb_id_by_imdb(supplied_imdb_id)
                if tracker_tmdb_id is not None:
                    tracker_movie = self._fetch_movie_details(tracker_tmdb_id)
                    id_mismatch = (
                        f"tracker imdb_id={supplied_imdb_id} resolved to tmdb_id={tracker_movie.tmdb_id} whose IMDb is {tracker_movie.imdb_id}"
                        if (
                            supplied_imdb_id
                            and tracker_movie.imdb_id
                            and tracker_movie.imdb_id.lower() != supplied_imdb_id
                        )
                        else None
                    )
                    consistency_conflict = check_tracker_movie_consistency(
                        tracker_movie,
                        query_title=query_title,
                        query_year=query_year,
                        nfo_runtime=nfo_runtime,
                        audio_languages=audio_languages,
                    )
                    if id_mismatch is None and consistency_conflict is None and tracker_conflict is None:
                        return TmdbLookupResult(
                            movie=tracker_movie,
                            tracker_conflict=None,
                            trusted_for_cache=True,
                        )
                    conflict_msg = "; ".join(p for p in (id_mismatch, consistency_conflict) if p)
                    if conflict_msg:
                        logger.warning("Rejecting tracker-supplied TMDB candidate: %s", conflict_msg)
                        tracker_conflict = (
                            f"{tracker_conflict}; {conflict_msg}" if tracker_conflict else conflict_msg
                        )

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
            if ident.tmdb_id is not None and tmdb_id is not None and tmdb_id != ident.tmdb_id:
                return None

        if tmdb_id is None and ident.tmdb_id is not None:
            tmdb_id = ident.tmdb_id

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
            logger.warning(
                "TMDB pre-lookup failed (falling back to Gemini-only): %s",
                sanitize_tracker_text(str(exc), max_chars=240),
            )
            return TmdbLookupResult()
        except Exception as exc:
            logger.warning(
                "Unexpected TMDB pre-lookup error: %s",
                sanitize_tracker_text(str(exc), max_chars=240),
            )
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
            logger.warning(
                "TMDB post-verification failed: %s",
                sanitize_tracker_text(str(exc), max_chars=240),
            )
            return None
        except Exception as exc:
            logger.warning(
                "Unexpected TMDB post-verification error: %s",
                sanitize_tracker_text(str(exc), max_chars=240),
            )
            return None
