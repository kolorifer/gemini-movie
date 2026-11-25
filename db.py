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


@dataclass(frozen=True)
class ReleaseQuality:
    resolution: Optional[str] = None
    is_remux: bool = False
    dolby_vision: bool = False
    hdr: bool = False
    hdr_label: str = "unknown"
    format_label: str = "unknown"
    release_group: Optional[str] = None
    is_favorite_group: bool = False
    cache_key: str = ""

    def to_prompt_line(self) -> Optional[str]:
        if not self.cache_key and not self.release_group:
            return None
        if self.resolution == "2160p" and self.dolby_vision:
            tier_note = "top preferred tier: 2160p + Dolby Vision bonus (positive tie-breaker on edge cases)"
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
) -> ReleaseQuality:
    """Deterministically parse release resolution, remux status, Dolby Vision/HDR, and release group."""
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

    has_dv = bool(_DV_TAG_RE.search(tag_text) or _DV_NFO_RE.search(nfo_str))
    has_hdr10plus = bool(_HDR10PLUS_RE.search(tag_text) or _HDR10PLUS_RE.search(nfo_str))
    has_hdr = has_hdr10plus or bool(_HDR_RE.search(tag_text) or _HDR_RE.search(nfo_str))

    if has_dv and has_hdr10plus:
        hdr_label = "Dolby Vision + HDR10+"
    elif has_dv and has_hdr:
        hdr_label = "Dolby Vision + HDR"
    elif has_dv:
        hdr_label = "Dolby Vision"
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
        dr_key = "dv" if has_dv else ("hdr" if has_hdr else "sdr")
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
