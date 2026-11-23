from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "triage.db"

IMDB_ID_RE = re.compile(r"\b(tt\d{7,10})\b", re.IGNORECASE)
YEAR_RE = re.compile(r"^(18[89]\d|19\d\d|20\d\d)$")

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

    def get_cached_evaluation(
        self,
        raw_text: str,
        *,
        hint_title: Optional[str] = None,
        hint_year: Optional[int] = None,
        extra_context: Optional[str] = None,
        ttl_hours: int = 72,
    ) -> Optional[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds"
        )
        norm_raw = normalize_raw_text(raw_text)
        imdb_id = extract_imdb_id(raw_text, extra_context)
        rel_key = normalize_release_key(raw_text, hint_title=hint_title, hint_year=hint_year)

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT response_json
                FROM evaluations
                WHERE raw_text_normalized = ? AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (norm_raw, cutoff),
            ).fetchone()

            if row is None and imdb_id:
                row = conn.execute(
                    """
                    SELECT response_json
                    FROM evaluations
                    WHERE lower(imdb_id) = ? AND created_at >= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (imdb_id.lower(), cutoff),
                ).fetchone()

            if row is None and rel_key:
                row = conn.execute(
                    """
                    SELECT response_json
                    FROM evaluations
                    WHERE release_key = ? AND created_at >= ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (rel_key, cutoff),
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
        ts = created_at or utc_now_iso()
        serialized = json.dumps(payload, ensure_ascii=False)

        with self._connect() as conn:
            # Replace superseded entries for the exact normalized string or IMDb ID
            conn.execute(
                "DELETE FROM evaluations WHERE raw_text_normalized = ?",
                (norm_raw,),
            )
            if imdb_id:
                conn.execute(
                    "DELETE FROM evaluations WHERE lower(imdb_id) = ?",
                    (imdb_id,),
                )
            elif rel_key:
                conn.execute(
                    "DELETE FROM evaluations WHERE release_key = ?",
                    (rel_key,),
                )

            conn.execute(
                """
                INSERT INTO evaluations (
                    raw_text_normalized,
                    release_key,
                    raw_text_original,
                    imdb_id,
                    canonical_title,
                    year,
                    verdict,
                    response_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm_raw,
                    rel_key,
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
        clean_note = note.strip() if note and note.strip() else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback (imdb_id, title, decision, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (clean_imdb, title.strip(), decision, clean_note, ts),
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
