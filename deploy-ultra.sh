#!/usr/bin/env bash
set -euo pipefail

# Ultra.cc Non-Root Movie Triage FastAPI Installer & Live Smoke Test
# Deploys to ~/apps/movie-triage-api with userland systemd service bound strictly to 127.0.0.1

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Error: Do not run this script as root or with sudo on Ultra.cc." >&2
  exit 1
fi

APP_DIR="$HOME/apps/movie-triage-api"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="movie-triage-api.service"

echo "[1/6] Finding an unassigned Ultra.cc port via app-ports free..."
if [[ -n "${PORT:-}" ]]; then
  CHOSEN_PORT="$PORT"
else
  if ! command -v app-ports >/dev/null 2>&1; then
    echo "Error: 'app-ports' command not found. Run this on your Ultra.cc seedbox or pass PORT=<port>." >&2
    exit 1
  fi
  CHOSEN_PORT="$(app-ports free | grep -Eo '[0-9]{4,5}' | head -n 1 || true)"
fi

if [[ -z "$CHOSEN_PORT" ]]; then
  echo "Error: Could not parse a free port from 'app-ports free'." >&2
  exit 1
fi
echo "Selected local port: $CHOSEN_PORT"

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  read -r -s -p "Enter your GEMINI_API_KEY (input hidden): " GEMINI_API_KEY
  echo ""
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "Error: GEMINI_API_KEY is required." >&2
  exit 1
fi

echo "[2/6] Creating project directory and writing microservice files..."
mkdir -p "$APP_DIR/data" "$SYSTEMD_DIR"

cat <<'EOF_REQUIREMENTS' > "$APP_DIR/requirements.txt"
fastapi
uvicorn[standard]
google-genai
pydantic
python-dotenv
EOF_REQUIREMENTS

cat <<'EOF_TASTE_PROFILE' > "$APP_DIR/taste_profile.txt"
No strong genre bias yet. Prioritize films with strong craft, originality, or notable festival/critical recognition. Flag derivative blockbusters, slow pacing with weak payoff, or ratings inflated by very low vote counts.
EOF_TASTE_PROFILE

cat <<'EOF_SCHEMAS' > "$APP_DIR/schemas.py"
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
EOF_SCHEMAS

cat <<'EOF_DB' > "$APP_DIR/db.py"
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
EOF_DB

cat <<'EOF_GEMINI_CLIENT' > "$APP_DIR/gemini_client.py"
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
REQUEST_TIMEOUT_SECONDS = 45.0


class GeminiClientError(RuntimeError):
    """Raised when Gemini returns an invalid or upstream error response."""


class GeminiTimeoutError(GeminiClientError):
    """Raised when the Gemini API call exceeds the 45-second timeout."""


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


def build_user_prompt(request: EvaluateRequest) -> str:
    parts = [f"Release string / query: {request.raw_text}"]
    if request.hint_title:
        parts.append(f"Hint title: {request.hint_title}")
    if request.hint_year is not None:
        parts.append(f"Hint year: {request.hint_year}")
    if request.extra_context:
        parts.append(f"Extra context: {request.extra_context}")
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
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
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
            contents=build_user_prompt(request),
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
    ) -> EvaluationPayload:
        # Dynamically reload taste_profile.txt on every single request
        taste_profile = load_taste_profile(self.taste_profile_path)
        system_instruction = build_system_instruction(taste_profile, recent_feedback)

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._call_sync, request, system_instruction),
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
EOF_GEMINI_CLIENT

cat <<'EOF_MAIN' > "$APP_DIR/main.py"
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

from db import TriageDB
from gemini_client import GeminiClientError, GeminiTimeoutError, GeminiTriageClient
from schemas import (
    EvaluateRequest,
    EvaluateResponse,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    RecentEvaluationItem,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
PORT = int(os.getenv("PORT", "8000"))
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "72"))

db = TriageDB(BASE_DIR / "data" / "triage.db")
gemini_client = GeminiTriageClient(
    api_key=os.getenv("GEMINI_API_KEY", "").strip(),
    model=GEMINI_MODEL,
    taste_profile_path=BASE_DIR / "taste_profile.txt",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Movie Triage API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        model=GEMINI_MODEL,
        port=PORT,
        cache_entries=db.get_cache_entries_count(ttl_hours=CACHE_TTL_HOURS),
    )


@app.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(payload: EvaluateRequest) -> EvaluateResponse:
    if not payload.force_refresh:
        cached = db.get_cached_evaluation(
            payload.raw_text,
            hint_title=payload.hint_title,
            hint_year=payload.hint_year,
            extra_context=payload.extra_context,
            ttl_hours=CACHE_TTL_HOURS,
        )
        if cached is not None:
            return EvaluateResponse(cached=True, **cached)

    recent_feedback = db.get_recent_feedback(limit=20)
    try:
        result = await gemini_client.evaluate(payload, recent_feedback)
    except GeminiTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except GeminiClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result_dict = result.model_dump()
    db.save_evaluation(
        payload.raw_text,
        result_dict,
        hint_title=payload.hint_title,
        hint_year=payload.hint_year,
    )
    return EvaluateResponse(cached=False, **result_dict)


@app.post("/feedback", response_model=FeedbackResponse)
async def submit_feedback(payload: FeedbackRequest) -> FeedbackResponse:
    total = db.add_feedback(
        imdb_id=payload.imdb_id,
        title=payload.title,
        decision=payload.decision,
        note=payload.note,
    )
    return FeedbackResponse(status="saved", total_feedback=total)


@app.get("/recent", response_model=list[RecentEvaluationItem])
async def recent(limit: int = Query(default=20, ge=1, le=100)) -> list[RecentEvaluationItem]:
    rows = db.get_recent_evaluations(limit=limit)
    return [RecentEvaluationItem(**row) for row in rows]
EOF_MAIN

echo "[3/6] Writing locked-down .env (chmod 600)..."
cat <<EOF_ENV > "$APP_DIR/.env"
GEMINI_API_KEY=${GEMINI_API_KEY}
GEMINI_MODEL=gemini-3.8-flash
HOST=127.0.0.1
PORT=${CHOSEN_PORT}
CACHE_TTL_HOURS=72
TMDB_API_KEY=
MDBLIST_API_KEY=
EOF_ENV
chmod 600 "$APP_DIR/.env"

echo "[4/6] Creating Python virtual environment and installing dependencies..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[5/6] Configuring and starting userland systemd unit..."
cat <<EOF_UNIT > "$SYSTEMD_DIR/$SERVICE_NAME"
[Unit]
Description=Local-only Movie Triage FastAPI Microservice
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/apps/movie-triage-api
EnvironmentFile=%h/apps/movie-triage-api/.env
ExecStart=%h/apps/movie-triage-api/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${CHOSEN_PORT}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF_UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"
sleep 2
systemctl --user status "$SERVICE_NAME" --no-pager

echo "[6/6] Running live smoke tests against http://127.0.0.1:${CHOSEN_PORT}..."
echo ""
echo "--- Smoke Test 1: GET /health ---"
curl -sS "http://127.0.0.1:${CHOSEN_PORT}/health" | python3 -m json.tool

echo ""
echo "--- Smoke Test 2: POST /evaluate (La Chimera, expect cached: false) ---"
curl -sS -X POST "http://127.0.0.1:${CHOSEN_PORT}/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "La.Chimera.2023.1080p.BluRay.x264-GROUP"}' | python3 -m json.tool

echo ""
echo "--- Smoke Test 3: Repeat POST /evaluate (La Chimera, expect cached: true) ---"
curl -sS -X POST "http://127.0.0.1:${CHOSEN_PORT}/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "La.Chimera.2023.1080p.BluRay.x264-GROUP"}' | python3 -m json.tool

echo ""
echo "--- Smoke Test 4: Disambiguation check (foreign/remake release string) ---"
curl -sS -X POST "http://127.0.0.1:${CHOSEN_PORT}/evaluate" \
  -H "Content-Type: application/json" \
  -d '{"raw_text": "Anatomie.d.une.chute.2023.FRENCH.2160p.UHD.BluRay.HEVC-GROUP"}' | python3 -m json.tool

echo ""
echo "Deployment complete. Movie Triage API is listening on http://127.0.0.1:${CHOSEN_PORT}"
