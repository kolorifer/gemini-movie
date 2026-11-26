#!/usr/bin/env python3
"""
Pre-cutover 5-Case Shadow Verification Runner (docs/ARCHITECTURE.md §8).

Wires the REAL `movie-intake-api`, REAL `movie-actions-api`, and REAL `movie-triage-api`
(over in-process ASGI HTTP transports) against isolated 0700/0600 shadow state and
zero-mutation stubbed external boundaries (Radarr, Telegram, TMDB, TorrentLeech, Gemini).

Produces ZERO external mutations and verifies all 5 mandatory pre-cutover shadow cases
plus file permissions and zero-secret SQLite hygiene.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db as triage_db_mod
import main as triage_main
from services.movie_actions_api.app import create_app as create_actions_app
from services.movie_actions_api.clients import (
    FeedbackPostResult,
    IntakePushClient,
    RadarrReadResult,
    RadarrWriteResult,
    ShadowStubRadarrClient,
    ShadowStubTelegramClient,
    TelegramSendResult,
    TriageFeedbackClient,
)
from services.movie_actions_api.config import ActionsConfig
from services.movie_actions_api.db import ActionsDB, NewActionRecord, generate_uuid7, utc_now, utc_now_iso
from services.movie_actions_api.security import hash_token
from services.movie_intake_api.app import create_app as create_intake_app
from services.movie_intake_api.clients import ActionsApiClient, TriageApiClient, TriageEvalResult
from services.movie_intake_api.config import IntakeConfig
from services.movie_intake_api.tmdb_verifier import TmdbIndependentClient, TmdbMovieRecord
from services.movie_intake_api.torrentleech import TorrentLeechResult, TorrentLeechScraper


def _is_within_dir(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _extract_tokens(reply_markup: dict[str, Any]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for row in reply_markup.get("inline_keyboard") or []:
        for btn in row:
            label = str(btn.get("text") or "")
            tok = urlparse(str(btn.get("url") or "")).path.rsplit("/", 1)[-1]
            if "Add & Grab" in label:
                tokens["add_grab"] = tok
            elif "Grab Anyway" in label:
                tokens["alternative_grab"] = tok
            elif "Watched – Good" in label:
                tokens["watched_good"] = tok
            elif "Watched – Bad" in label:
                tokens["watched_bad"] = tok
            elif "Skip" in label:
                tokens["skip"] = tok
    return tokens


class _ShadowRadarr(ShadowStubRadarrClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_reads = False
        self.push_queue: list[RadarrWriteResult] = []
        self.readback_calls = 0

    async def get_tags(self) -> RadarrReadResult:
        if self.fail_reads:
            return RadarrReadResult(ok=False, status_code=503, error_redacted="radarr_http_503")
        return await super().get_tags()

    async def get_movies_by_tmdb(self, tmdb_id: int) -> RadarrReadResult:
        if self.fail_reads:
            return RadarrReadResult(ok=False, status_code=503, error_redacted="radarr_http_503")
        return await super().get_movies_by_tmdb(tmdb_id)

    async def get_movies_by_imdb(self, imdb_id: str) -> RadarrReadResult:
        if self.fail_reads:
            return RadarrReadResult(ok=False, status_code=503, error_redacted="radarr_http_503")
        return await super().get_movies_by_imdb(imdb_id)

    async def get_movie_by_id(self, movie_id: int) -> RadarrReadResult:
        self.readback_calls += 1
        return await super().get_movie_by_id(movie_id)

    async def push_release(self, push_payload: dict[str, Any]) -> RadarrWriteResult:
        if self.push_queue:
            self.push_calls.append(dict(push_payload))
            return self.push_queue.pop(0)
        return await super().push_release(push_payload)


class _ShadowTelegram(ShadowStubTelegramClient):
    def __init__(self) -> None:
        super().__init__()
        self.photo_queue: list[TelegramSendResult] = []

    async def send_photo(self, **kwargs: Any) -> TelegramSendResult:
        if self.photo_queue:
            self.sent_photos.append(dict(kwargs))
            return self.photo_queue.pop(0)
        return await super().send_photo(**kwargs)


class _ShadowFeedbackClient(TriageFeedbackClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.force_http_502 = False

    async def post_feedback(
        self,
        *,
        title: str,
        imdb_id: Optional[str],
        tmdb_id: Optional[int],
        decision: str,
        note: str,
    ) -> FeedbackPostResult:
        if self.force_http_502:
            return FeedbackPostResult(saved=False, error_redacted="triage_feedback_http_502")
        return await super().post_feedback(
            title=title,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            decision=decision,
            note=note,
        )


class _ShadowTmdb(TmdbIndependentClient):
    def __init__(self, cache_path: Path, movies: dict[int, TmdbMovieRecord]) -> None:
        super().__init__(api_token="shadow-unused", cache_path=cache_path)
        self._movies = movies

    async def get_movie_by_tmdb_id(self, tmdb_id: int) -> Optional[TmdbMovieRecord]:
        return self._movies.get(tmdb_id)

    async def find_movies_by_imdb_id(self, imdb_id: str) -> Optional[list[int]]:
        low = imdb_id.lower()
        return [m.tmdb_id for m in self._movies.values() if (m.imdb_id or "").lower() == low]


class _ShadowTl(TorrentLeechScraper):
    def __init__(self, cookie_file: Path, outcomes: dict[str, TorrentLeechResult]) -> None:
        super().__init__(db=None, cookie_file=cookie_file, enabled=True)  # type: ignore[arg-type]
        self._outcomes = outcomes
        self.calls: list[str] = []

    async def fetch_listing(
        self, *, external_id: str, exact_release_name: str, **kwargs: Any
    ) -> TorrentLeechResult:
        self.calls.append(external_id)
        return self._outcomes.get(external_id, TorrentLeechResult(outcome="listing_id_missing"))


class _ShadowEval(TriageApiClient):
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        super().__init__(base_url="http://shadow-eval.invalid", internal_token="")
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def evaluate(self, request_body: dict[str, Any]) -> TriageEvalResult:
        self.calls.append(dict(request_body))
        raw = str(request_body.get("raw_text") or "")
        for key, payload in self._responses.items():
            if key in raw:
                return TriageEvalResult(ok=True, payload=payload)
        return TriageEvalResult(
            ok=True,
            payload={
                "identification": {
                    "canonical_title": "Unknown",
                    "year": 2025,
                    "director": "Unknown",
                    "imdb_id": None,
                    "tmdb_id": None,
                    "match_confidence": "low",
                },
                "ratings": {},
                "recognition": {"awards_highlights": [], "critic_consensus": "Unverified."},
                "decision_aid": {
                    "one_line_pitch": "Unverified release.",
                    "verdict": "MAYBE",
                    "verdict_reason": "Needs review.",
                    "similar_to": [],
                    "red_flags": [],
                },
                "cached": False,
            },
        )


@dataclass
class ShadowSuiteResult:
    passed: bool
    cases: dict[str, dict[str, Any]]


async def run_shadow_suite(state_root: Path) -> ShadowSuiteResult:
    """Execute all 5 mandatory pre-cutover shadow cases across real wired services."""
    default_prod_state = Path.home() / ".local" / "state" / "movie-intel"
    default_prod_config = Path.home() / ".config" / "movie-intel"
    if _is_within_dir(state_root, default_prod_state) or _is_within_dir(state_root, default_prod_config):
        raise ValueError("Refusing to run shadow verification inside production ~/.local/state or ~/.config/movie-intel")

    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)

    internal_secret = "shadow-suite-internal-secret"
    webhook_secret = "shadow-suite-webhook-secret"
    synthetic_passkey = "SYNTHETIC_SHADOW_PASSKEY_DO_NOT_LEAK_98765"

    # Isolate real movie-triage-api SQLite DB
    orig_triage_db = triage_main.db
    orig_triage_secret = triage_main.INTERNAL_API_SECRET
    triage_db_path = state_root / "triage.db"
    triage_main.db = triage_db_mod.TriageDB(triage_db_path)
    triage_main.INTERNAL_API_SECRET = internal_secret
    triage_main.db.init_db()

    cases: dict[str, dict[str, Any]] = {}
    all_raw_action_tokens: list[str] = []

    try:
        tmdb_records = {
            100100: TmdbMovieRecord(
                tmdb_id=100100,
                imdb_id="tt1001000",
                title="Example Film",
                original_title="Example Film",
                year=2024,
                runtime_minutes=120,
                alternative_titles=(),
                poster_path="/example_film.jpg",
            ),
            2024999: TmdbMovieRecord(
                tmdb_id=2024999,
                imdb_id="tt2024999",
                title="True Film",
                original_title="True Film",
                year=2024,
                runtime_minutes=124,
                alternative_titles=(),
                poster_path="/true_film.jpg",
            ),
            99999: TmdbMovieRecord(
                tmdb_id=99999,
                imdb_id="tt0099999",
                title="Wrong Unrelated Film",
                original_title="Wrong Unrelated Film",
                year=2018,
                runtime_minutes=89,
                alternative_titles=(),
                poster_path="/wrong.jpg",
            ),
            3025001: TmdbMovieRecord(
                tmdb_id=3025001,
                imdb_id="tt3025001",
                title="Festival Gem",
                original_title="Festival Gem",
                year=2025,
                runtime_minutes=114,
                alternative_titles=(),
                poster_path="/festival_gem.jpg",
            ),
            3025002: TmdbMovieRecord(
                tmdb_id=3025002,
                imdb_id="tt3025002",
                title="Long Epic",
                original_title="Long Epic",
                year=2025,
                runtime_minutes=168,
                alternative_titles=(),
                poster_path="/long_epic.jpg",
            ),
            400401: TmdbMovieRecord(
                tmdb_id=400401,
                imdb_id="tt4004010",
                title="Already Watched",
                original_title="Already Watched",
                year=2024,
                runtime_minutes=110,
                alternative_titles=(),
                poster_path="/watched.jpg",
            ),
            400402: TmdbMovieRecord(
                tmdb_id=400402,
                imdb_id="tt4004020",
                title="Monitored Pending",
                original_title="Monitored Pending",
                year=2024,
                runtime_minutes=105,
                alternative_titles=(),
                poster_path="/monitored.jpg",
            ),
            400403: TmdbMovieRecord(
                tmdb_id=400403,
                imdb_id="tt4004030",
                title="Already Owned",
                original_title="Already Owned",
                year=2024,
                runtime_minutes=115,
                alternative_titles=(),
                poster_path="/owned.jpg",
            ),
        }
        tl_outcomes = {
            "9001001": TorrentLeechResult(
                outcome="listing_id_found",
                imdb_id="tt1001000",
                file_runtime_minutes=120,
                audio_languages=("English",),
                freeleech_badge=True,
            ),
            "9001002": TorrentLeechResult(
                outcome="listing_id_found",
                imdb_id="tt1001000",
                file_runtime_minutes=120,
                audio_languages=("English",),
            ),
            "9001003": TorrentLeechResult(
                outcome="listing_id_found",
                imdb_id="tt1001000",
                file_runtime_minutes=120,
                audio_languages=("English",),
            ),
            "9001004": TorrentLeechResult(outcome="login_required"),
            "9002001": TorrentLeechResult(
                outcome="listing_id_found",
                imdb_id="tt0099999",
                file_runtime_minutes=124,
                audio_languages=("English",),
                sanitized_nfo="General\nDuration : 2 h 4 min\nAudio\nLanguage : English\nText\nLanguage : English\nText #2\nLanguage : French",
            ),
            "9003001": TorrentLeechResult(
                outcome="listing_id_missing",
                file_runtime_minutes=112,
                audio_languages=("English",),
            ),
            "9003002": TorrentLeechResult(outcome="login_required"),
            "9003003": TorrentLeechResult(
                outcome="listing_id_missing",
                file_runtime_minutes=112,
                audio_languages=("English",),
            ),
            "9004001": TorrentLeechResult(outcome="listing_id_found", imdb_id="tt4004010", file_runtime_minutes=110),
            "9004002": TorrentLeechResult(outcome="listing_id_found", imdb_id="tt4004020", file_runtime_minutes=105),
            "9004003": TorrentLeechResult(outcome="listing_id_found", imdb_id="tt4004030", file_runtime_minutes=115),
            "9004004": TorrentLeechResult(outcome="listing_id_found", imdb_id="tt1001000", file_runtime_minutes=120),
        }

        def _make_eval(title: str, year: int, imdb_id: str, tmdb_id: int, conf: str = "high") -> dict[str, Any]:
            return {
                "identification": {
                    "canonical_title": title,
                    "year": year,
                    "director": "Shadow Director",
                    "imdb_id": imdb_id,
                    "tmdb_id": tmdb_id,
                    "match_confidence": conf,
                },
                "ratings": {"rotten_tomatoes_critics": 90, "imdb": 8.0},
                "recognition": {"awards_highlights": ["Festival Official Selection"], "critic_consensus": "Acclaimed."},
                "decision_aid": {
                    "one_line_pitch": "Compelling cinema.",
                    "verdict": "WATCH",
                    "verdict_reason": "Matches profile.",
                    "similar_to": [],
                    "red_flags": [],
                },
                "cached": False,
            }

        eval_stub = _ShadowEval(
            {
                "Example.Film": _make_eval("Example Film", 2024, "tt1001000", 100100),
                "True.Film": _make_eval("True Film", 2024, "tt2024999", 2024999),
                "Festival.Gem": _make_eval("Festival Gem", 2025, "tt3025001", 3025001),
                "Long.Epic": _make_eval("Long Epic", 2025, "tt3025002", 3025002),
                "Monitored.Pending": _make_eval("Monitored Pending", 2024, "tt4004020", 400402),
            }
        )
        radarr_stub = _ShadowRadarr()
        tg_stub = _ShadowTelegram()
        tl_stub = _ShadowTl(state_root / "unused-cookies.json", tl_outcomes)
        tmdb_stub = _ShadowTmdb(state_root / "intake-tmdb-cache.json", tmdb_records)

        # Seed Radarr states for Case 4
        radarr_stub.movies = {
            41: {"id": 41, "tmdbId": 400401, "imdbId": "tt4004010", "monitored": False, "hasFile": False, "tags": [7]},
            42: {"id": 42, "tmdbId": 400402, "imdbId": "tt4004020", "monitored": True, "hasFile": False, "tags": []},
            43: {
                "id": 43,
                "tmdbId": 400403,
                "imdbId": "tt4004030",
                "monitored": True,
                "hasFile": True,
                "movieFile": {"id": 430, "quality": {"quality": {"name": "Remux-2160p"}}},
                "tags": [],
            },
        }

        actions_cfg = ActionsConfig(
            host="127.0.0.1",
            port=40721,
            public_base_url="https://shadow.invalid/movie-intel",
            internal_api_secret=internal_secret,
            radarr_url="http://shadow-radarr.invalid",
            radarr_api_key="SHADOW_RADARR_KEY",
            telegram_bot_token="111111111:SHADOW_TELEGRAM_BOT_TOKEN_DO_NOT_LEAK",
            telegram_chat_id="-100111222333",
            telegram_topic_id=10,
            telegram_add_grab_topic_id=20,
            telegram_grab_anyway_topic_id=30,
            action_ttl_seconds=3600,
            standalone_skip_enabled=True,
            alternative_grab_config_path=None,
            actions_db_path=state_root / "actions.db",
            intake_api_url="http://intake.local",
            triage_api_url="http://triage.local",
            shadow_replay_enabled=False,
        )
        intake_cfg = IntakeConfig(
            host="127.0.0.1",
            port=40720,
            webhook_secret=webhook_secret,
            internal_api_secret=internal_secret,
            tl_metadata_enabled=True,
            tl_cookie_file=state_root / "unused-cookies.json",
            tmdb_api_token="shadow-unused",
            tmdb_cache_path=state_root / "intake-tmdb-cache.json",
            webhook_archive_dir=state_root / "webhook-archive",
            intake_db_path=state_root / "intake.db",
            triage_api_url="http://triage.local",
            actions_api_url="http://actions.local",
            shadow_replay_enabled=False,
        )

        actions_holder: dict[str, Any] = {}
        intake_holder: dict[str, Any] = {}
        triage_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=triage_main.app), base_url="http://triage.local"
        )
        feedback_client = _ShadowFeedbackClient(
            base_url="http://triage.local", internal_token=internal_secret, http_client=triage_http
        )

        async def _call_intake(request: httpx.Request) -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=intake_holder["app"]), base_url="http://intake.local"
            ) as c:
                return await c.request(request.method, str(request.url), headers=request.headers, content=request.content)

        async def _call_actions(request: httpx.Request) -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=actions_holder["app"]), base_url="http://actions.local"
            ) as c:
                return await c.request(request.method, str(request.url), headers=request.headers, content=request.content)

        i2a_http = httpx.AsyncClient(transport=httpx.MockTransport(_call_actions), base_url="http://actions.local")
        a2i_http = httpx.AsyncClient(transport=httpx.MockTransport(_call_intake), base_url="http://intake.local")

        actions_app = create_actions_app(
            actions_cfg,
            process_instance_id="shadow-proc-1",
            radarr_client=radarr_stub,
            telegram_client=tg_stub,
            intake_push_client=IntakePushClient(
                base_url="http://intake.local", internal_token=internal_secret, http_client=a2i_http
            ),
            triage_feedback_client=feedback_client,
        )
        actions_holder["app"] = actions_app

        intake_app = create_intake_app(
            intake_cfg,
            tl_scraper=tl_stub,
            tmdb_client=tmdb_stub,
            triage_client=eval_stub,
            actions_client=ActionsApiClient(
                base_url="http://actions.local", internal_token=internal_secret, http_client=i2a_http
            ),
            start_worker=False,
        )
        intake_holder["app"] = intake_app

        def _wh(
            *,
            indexer: str,
            ext_id: str,
            name: str,
            title: str,
            year: int,
            meta_imdb: Optional[str] = None,
            meta_tmdb: Optional[int] = None,
        ) -> dict[str, Any]:
            rel: dict[str, Any] = {
                "external_id": ext_id,
                "name": name,
                "title": title,
                "year": year,
                "type": "movie",
                "category": "Movies :: 4K",
                "download_url": f"https://shadow-tracker.invalid/dl/{ext_id}/{synthetic_passkey}/file.torrent",
                "size": 50_000_000_000,
            }
            if meta_imdb:
                rel["meta_imdb"] = meta_imdb
            if meta_tmdb is not None:
                rel["meta_tmdb"] = meta_tmdb
            return {"action": {"rejected": False}, "data": {"indexer": indexer, "release": rel}}

        async with actions_app.router.lifespan_context(actions_app):
            async with intake_app.router.lifespan_context(intake_app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=intake_app), base_url="http://intake.local"
                ) as ic, httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=actions_app), base_url="http://actions.local"
                ) as ac:
                    # ---------------------------------------------------------
                    # CASE 1: Duplicate burst (1a), mid-flight restart (1b),
                    # late duplicate (1c), non-collapse (1d), verified correction (1e)
                    # ---------------------------------------------------------
                    rel1_name = "Example.Film.2024.2160p.UHD.BluRay.REMUX.DV.HDR10-CiNEPHiLES"
                    p1 = _wh(
                        indexer="TorrentLeech",
                        ext_id="9001001",
                        name=rel1_name,
                        title="Example Film",
                        year=2024,
                        meta_imdb="tt1001000",
                        meta_tmdb=100100,
                    )
                    r1a, r1b, r1c = await asyncio.gather(
                        ic.post(f"/webhook/{webhook_secret}", json=p1),
                        ic.post(f"/webhook/{webhook_secret}", json=p1),
                        ic.post(f"/webhook/{webhook_secret}", json=p1),
                    )
                    assert r1a.status_code == 200 and r1b.status_code == 200 and r1c.status_code == 200
                    rel1_id = r1a.json()["release_id"]
                    assert rel1_id == r1b.json()["release_id"] == r1c.json()["release_id"]
                    assert len({r1a.json()["occurrence_id"], r1b.json()["occurrence_id"], r1c.json()["occurrence_id"]}) == 3
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert await intake_app.state.worker.process_next_queued() is False
                    assert len(tg_stub.sent_photos) == 1
                    assert len(tg_stub.sent_messages) == 1  # evaluation reply anchored to card

                    # 1b: Mid-flight worker crash & expired-lease restart recovery
                    p1_crash = _wh(
                        indexer="TorrentLeech",
                        ext_id="9001003",
                        name="Example.Film.2024.2160p.UHD.BluRay.REMUX.DV-CRASH",
                        title="Example Film",
                        year=2024,
                        meta_imdb="tt1001000",
                        meta_tmdb=100100,
                    )
                    r1_crash = await ic.post(f"/webhook/{webhook_secret}", json=p1_crash)
                    crash_rel_id = r1_crash.json()["release_id"]
                    claimed = intake_app.state.db.claim_next_queued_release(worker_id="dead-worker", lease_seconds=-10)
                    assert claimed is not None and claimed["release_id"] == crash_rel_id
                    assert claimed["state"] == "enriching"
                    recovered_n = intake_app.state.db.recover_orphaned_leases(
                        active_worker_id=intake_app.state.worker.worker_id, recover_dead_owners=False
                    )
                    assert recovered_n >= 1
                    photos_before_1b = len(tg_stub.sent_photos)
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert len(tg_stub.sent_photos) == photos_before_1b + 1
                    all_raw_action_tokens.extend(_extract_tokens(tg_stub.sent_photos[-1]["reply_markup"]).values())

                    # 1c: Late duplicate after completed -> 0 new TL/eval/Telegram calls
                    tl_before_late = len(tl_stub.calls)
                    tg_before_late = len(tg_stub.sent_photos)
                    r1_late = await ic.post(f"/webhook/{webhook_secret}", json=p1)
                    assert r1_late.json()["release_id"] == rel1_id
                    assert await intake_app.state.worker.process_next_queued() is False
                    assert len(tl_stub.calls) == tl_before_late and len(tg_stub.sent_photos) == tg_before_late

                    # 1d: Non-collapse across different encode and different tracker
                    p1_enc = _wh(
                        indexer="TorrentLeech",
                        ext_id="9001002",
                        name="Example.Film.2024.1080p.BluRay.REMUX.AVC-FraMeSToR",
                        title="Example Film",
                        year=2024,
                        meta_imdb="tt1001000",
                    )
                    p1_trk = _wh(
                        indexer="OtherTracker",
                        ext_id="555",
                        name=rel1_name,
                        title="Example Film",
                        year=2024,
                        meta_imdb="tt1001000",
                    )
                    r_enc = await ic.post(f"/webhook/{webhook_secret}", json=p1_enc)
                    r_trk = await ic.post(f"/webhook/{webhook_secret}", json=p1_trk)
                    assert len({rel1_id, r_enc.json()["release_id"], r_trk.json()["release_id"]}) == 3
                    while await intake_app.state.worker.process_next_queued():
                        pass
                    for photo in tg_stub.sent_photos:
                        all_raw_action_tokens.extend(_extract_tokens(photo["reply_markup"]).values())

                    # 1e: Verified correction after initial evidence_review -> card_revision 2, edit bound card, force_refresh
                    p1e_init = _wh(
                        indexer="TorrentLeech",
                        ext_id="9001004",
                        name="Example.Film.2024.2160p.UHD.BluRay.REMUX-CORR",
                        title="Example Film",
                        year=2024,
                    )
                    r1e_init = await ic.post(f"/webhook/{webhook_secret}", json=p1e_init)
                    rel1e_id = r1e_init.json()["release_id"]
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert intake_app.state.db.get_release(rel1e_id)["card_revision"] == 1
                    edits_before_1e = len(tg_stub.edited_texts)
                    tl_outcomes["9001004"] = TorrentLeechResult(
                        outcome="listing_id_found",
                        imdb_id="tt1001000",
                        file_runtime_minutes=120,
                        audio_languages=("English",),
                    )
                    p1e_corr = _wh(
                        indexer="TorrentLeech",
                        ext_id="9001004",
                        name="Example.Film.2024.2160p.UHD.BluRay.REMUX-CORR",
                        title="Example Film",
                        year=2024,
                        meta_imdb="tt1001000",
                        meta_tmdb=100100,
                    )
                    r1e_corr = await ic.post(f"/webhook/{webhook_secret}", json=p1e_corr)
                    assert r1e_corr.json()["release_id"] == rel1e_id
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert intake_app.state.db.get_release(rel1e_id)["card_revision"] == 2
                    assert len(tg_stub.edited_texts) == edits_before_1e + 1
                    assert eval_stub.calls[-1].get("force_refresh") is True
                    all_raw_action_tokens.extend(_extract_tokens(tg_stub.edited_texts[-1]["reply_markup"]).values())
                    cases["case_1_duplicate_burst_and_non_collapse"] = {
                        "passed": True,
                        "distinct_releases": 3,
                        "subcases": ["1a", "1b", "1c", "1d", "1e"],
                    }

                    # ---------------------------------------------------------
                    # CASE 2: Wrong tracker IMDb -> evidence_review only
                    # ---------------------------------------------------------
                    photos_before_c2 = len(tg_stub.sent_photos)
                    msgs_before_c2 = len(tg_stub.sent_messages)
                    p2 = _wh(
                        indexer="TorrentLeech",
                        ext_id="9002001",
                        name="True.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                        title="True Film",
                        year=2024,
                        meta_imdb="tt0099999",
                    )
                    r2 = await ic.post(f"/webhook/{webhook_secret}", json=p2)
                    assert r2.status_code == 200
                    assert await intake_app.state.worker.process_next_queued() is True
                    # Evidence review card has ZERO buttons and ZERO poster; eval reply bound to persisted receipt
                    assert len(tg_stub.sent_photos) == photos_before_c2
                    assert len(tg_stub.sent_messages) >= msgs_before_c2 + 2
                    c2_card = tg_stub.sent_messages[msgs_before_c2]
                    c2_reply = tg_stub.sent_messages[msgs_before_c2 + 1]
                    assert c2_card["reply_markup"] == {"inline_keyboard": []}
                    c2_delivery = actions_app.state.db.get_delivery(r2.json()["release_id"])
                    assert c2_reply["reply_to_message_id"] == c2_delivery["ack_message_id"]
                    c2_eval = eval_stub.calls[-1]
                    assert c2_eval.get("force_refresh") is True
                    assert c2_eval["torrent_info"]["runtime_minutes"] == 124
                    assert c2_eval["torrent_info"]["audio_languages"] == ["English"]
                    cases["case_2_wrong_tracker_imdb"] = {"passed": True, "buttons": 0, "force_refresh": True}

                    # ---------------------------------------------------------
                    # CASE 3: Unidentified movie gate pass (3a) + controls (3b, 3c)
                    # ---------------------------------------------------------
                    photos_before_c3 = len(tg_stub.sent_photos)
                    p3a = _wh(
                        indexer="TorrentLeech",
                        ext_id="9003001",
                        name="Festival.Gem.2025.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                        title="Festival Gem",
                        year=2025,
                    )
                    await ic.post(f"/webhook/{webhook_secret}", json=p3a)
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert len(tg_stub.sent_photos) == photos_before_c3 + 1
                    assert tg_stub.sent_photos[-1]["thread_id"] == 20  # TELEGRAM_ADD_GRAB_TOPIC_ID
                    c3a_tokens = _extract_tokens(tg_stub.sent_photos[-1]["reply_markup"])
                    assert set(c3a_tokens.keys()) == {"add_grab", "watched_good", "watched_bad", "skip"}
                    all_raw_action_tokens.extend(c3a_tokens.values())

                    # 3b: failed TL (login_required) -> evidence_review ONLY
                    photos_before_3b = len(tg_stub.sent_photos)
                    p3b = _wh(
                        indexer="TorrentLeech",
                        ext_id="9003002",
                        name="Festival.Gem.2025.1080p.BluRay.REMUX-OTHER",
                        title="Festival Gem",
                        year=2025,
                    )
                    await ic.post(f"/webhook/{webhook_secret}", json=p3b)
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert len(tg_stub.sent_photos) == photos_before_3b

                    # 3c: runtime mismatch (112m vs 168m) -> evidence_review ONLY
                    p3c = _wh(
                        indexer="TorrentLeech",
                        ext_id="9003003",
                        name="Long.Epic.2025.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                        title="Long Epic",
                        year=2025,
                    )
                    await ic.post(f"/webhook/{webhook_secret}", json=p3c)
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert len(tg_stub.sent_photos) == photos_before_3b
                    cases["case_3_unidentified_gate_and_controls"] = {"passed": True, "subcases": ["3a", "3b", "3c"]}

                    # ---------------------------------------------------------
                    # CASE 4: Radarr suppression matrix (4a-4d)
                    # ---------------------------------------------------------
                    evals_before_4a = len(eval_stub.calls)
                    photos_before_4a = len(tg_stub.sent_photos)
                    # 4a: watched + unmonitored -> suppressed (0 Telegram, 0 Gemini!)
                    r4a = await ic.post(
                        f"/webhook/{webhook_secret}",
                        json=_wh(
                            indexer="TorrentLeech",
                            ext_id="9004001",
                            name="Already.Watched.2024.2160p.REMUX-GRP",
                            title="Already Watched",
                            year=2024,
                            meta_imdb="tt4004010",
                            meta_tmdb=400401,
                        ),
                    )
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert len(eval_stub.calls) == evals_before_4a
                    assert len(tg_stub.sent_photos) == photos_before_4a
                    assert intake_app.state.db.get_release(r4a.json()["release_id"])["suppression_reason"] == "watched_unmonitored"

                    # 4b: monitored no file -> NOT suppressed, labeled explicitly
                    await ic.post(
                        f"/webhook/{webhook_secret}",
                        json=_wh(
                            indexer="TorrentLeech",
                            ext_id="9004002",
                            name="Monitored.Pending.2024.2160p.REMUX-GRP",
                            title="Monitored Pending",
                            year=2024,
                            meta_imdb="tt4004020",
                            meta_tmdb=400402,
                        ),
                    )
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert len(tg_stub.sent_photos) == photos_before_4a + 1
                    assert "Monitored in Radarr (no file yet)" in tg_stub.sent_photos[-1]["caption_html"]
                    c4b_tokens = _extract_tokens(tg_stub.sent_photos[-1]["reply_markup"])
                    all_raw_action_tokens.extend(c4b_tokens.values())

                    # 4c: already owned (no Alternative Grab) -> suppressed
                    photos_before_4c = len(tg_stub.sent_photos)
                    r4c = await ic.post(
                        f"/webhook/{webhook_secret}",
                        json=_wh(
                            indexer="TorrentLeech",
                            ext_id="9004003",
                            name="Already.Owned.2024.2160p.REMUX-GRP",
                            title="Already Owned",
                            year=2024,
                            meta_imdb="tt4004030",
                            meta_tmdb=400403,
                        ),
                    )
                    assert await intake_app.state.worker.process_next_queued() is True
                    assert len(tg_stub.sent_photos) == photos_before_4c
                    assert intake_app.state.db.get_release(r4c.json()["release_id"])["suppression_reason"] == "already_owned"

                    # 4d: Radarr 503 lookup_failed -> end-to-end webhook lands in evidence_review (0 buttons)
                    photos_before_4d = len(tg_stub.sent_photos)
                    msgs_before_4d = len(tg_stub.sent_messages)
                    radarr_stub.fail_reads = True
                    r4d_wh = await ic.post(
                        f"/webhook/{webhook_secret}",
                        json=_wh(
                            indexer="TorrentLeech",
                            ext_id="9004004",
                            name="Example.Film.2024.2160p.UHD.BluRay.REMUX-R503",
                            title="Example Film",
                            year=2024,
                            meta_imdb="tt1001000",
                            meta_tmdb=100100,
                        ),
                    )
                    assert await intake_app.state.worker.process_next_queued() is True
                    radarr_stub.fail_reads = False
                    r4d_rel_id = r4d_wh.json()["release_id"]
                    assert actions_app.state.db.get_delivery(r4d_rel_id)["delivery_kind"] == "evidence_review"
                    assert len(tg_stub.sent_photos) == photos_before_4d + 1
                    assert tg_stub.sent_photos[-1]["reply_markup"] == {"inline_keyboard": []}
                    assert "Radarr status unverified" in tg_stub.sent_photos[-1]["caption_html"]
                    assert intake_app.state.db.get_release(r4d_rel_id)["suppression_reason"] is None
                    cases["case_4_radarr_suppression_matrix"] = {"passed": True, "subcases": ["4a", "4b", "4c", "4d"]}

                    # ---------------------------------------------------------
                    # CASE 5: Lost acks (5a/5b), rejected push (5c), watched/feedback 502 (5d),
                    # poster fallback (5e), restart recovery + reconciliation (5f)
                    # ---------------------------------------------------------
                    # 5b: Lost Radarr push ack + delayed grab -> 1st reconcile absent (0 2nd push), 2nd confirms & posts real /feedback!
                    radarr_stub.push_queue.append(
                        RadarrWriteResult(outcome="ambiguous", error_redacted="radarr_push_ambiguous:ReadTimeout")
                    )
                    r5b_init = await ac.post(
                        f"/action/{c3a_tokens['add_grab']}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    assert r5b_init.status_code == 503 and len(radarr_stub.push_calls) == 1
                    r5b_rec1 = await ac.post(f"/action/{c3a_tokens['add_grab']}", data={"confirm": "1", "reconcile": "1"})
                    assert r5b_rec1.status_code == 200 and len(radarr_stub.push_calls) == 1
                    added_mid = next(m["id"] for m in radarr_stub.movies.values() if m["tmdbId"] == 3025001)
                    radarr_stub.history.append(
                        {
                            "movieId": added_mid,
                            "eventType": "grabbed",
                            "sourceTitle": "Festival.Gem.2025.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                            "date": utc_now_iso(),
                        }
                    )
                    r5b_rec2 = await ac.post(f"/action/{c3a_tokens['add_grab']}", data={"confirm": "1", "reconcile": "1"})
                    assert r5b_rec2.status_code == 200 and "evaluator rating saved (WATCH)" in r5b_rec2.text
                    assert len(radarr_stub.push_calls) == 1
                    assert len(triage_main.db.get_recent_feedback()) == 1

                    # 5c: HTTP 200 rejected Radarr push -> rejected_push, 0 /feedback calls
                    feedback_before_5c = len(triage_main.db.get_recent_feedback())
                    radarr_stub.push_queue.append(
                        RadarrWriteResult(
                            outcome="rejected",
                            status_code=200,
                            data={"approved": False, "rejected": True, "rejections": ["Quality cutoff met"]},
                            error_redacted="Radarr rejected release push: Quality cutoff met",
                        )
                    )
                    r5c = await ac.post(f"/action/{c4b_tokens['add_grab']}", data={"confirm": "1"})
                    assert r5c.status_code == 200 and "Quality cutoff met" in r5c.text and "rejected_push" in r5c.text
                    assert len(triage_main.db.get_recent_feedback()) == feedback_before_5c
                    assert (
                        actions_app.state.db.get_action_group_readonly(hash_token(c4b_tokens["add_grab"])).action["status"]
                        == "rejected_push"
                    )

                    # 5d: Concurrent watched_good + watched_bad shared completion_group + /feedback HTTP 502 isolation
                    updates_before_5d = len(radarr_stub.update_calls)
                    readbacks_before_5d = radarr_stub.readback_calls
                    feedback_client.force_http_502 = True
                    r5d_good, r5d_bad = await asyncio.gather(
                        ac.post(f"/action/{c4b_tokens['watched_good']}", data={"confirm": "1"}),
                        ac.post(f"/action/{c4b_tokens['watched_bad']}", data={"confirm": "1"}),
                    )
                    feedback_client.force_http_502 = False
                    assert len(radarr_stub.update_calls) == updates_before_5d + 1
                    assert radarr_stub.readback_calls >= readbacks_before_5d + 1
                    assert radarr_stub.movies[42]["monitored"] is False
                    assert 7 in radarr_stub.movies[42]["tags"]
                    assert any("not saved" in resp.text.lower() for resp in (r5d_good, r5d_bad))
                    assert any("watched" in resp.text.lower() for resp in (r5d_good, r5d_bad))

                    # 5e: Poster 400 -> immediate text fallback preserving buttons
                    tg_stub.photo_queue.append(
                        TelegramSendResult(outcome="failed_definitive", error_redacted="telegram_http_400:bad_photo")
                    )
                    r5e = await ac.post(
                        "/internal/deliver/card",
                        json={
                            "release_id": "shadow-5e",
                            "card_revision": 1,
                            "source_occurrence_id": "occ-5e",
                            "delivery_kind": "actionable_grab",
                            "identity_gate_status": "verified",
                            "verified_tmdb_id": 100100,
                            "verified_imdb_id": "tt1001000",
                            "verified_title": "Example Film",
                            "verified_year": 2024,
                            "poster_path": "/example_film.jpg",
                            "exact_release_name": rel1_name,
                            "indexer": "TorrentLeech",
                        },
                        headers={"X-Internal-Token": internal_secret},
                    )
                    assert r5e.status_code == 200 and r5e.json()["status"] == "delivered"
                    assert actions_app.state.db.get_delivery("shadow-5e")["used_poster"] == 0
                    all_raw_action_tokens.extend(_extract_tokens(tg_stub.sent_messages[-1]["reply_markup"]).values())

                    # 5a: Lost Telegram ack -> ambiguous_delivery, unanchored eval refused 409
                    tg_stub.photo_queue.append(
                        TelegramSendResult(outcome="ambiguous", error_redacted="telegram_transport_ambiguous:ReadTimeout")
                    )
                    r5a = await ac.post(
                        "/internal/deliver/card",
                        json={
                            "release_id": "shadow-5a",
                            "card_revision": 1,
                            "source_occurrence_id": "occ-5a",
                            "delivery_kind": "actionable_grab",
                            "identity_gate_status": "verified",
                            "verified_tmdb_id": 100100,
                            "verified_imdb_id": "tt1001000",
                            "verified_title": "Example Film",
                            "verified_year": 2024,
                            "poster_path": "/example_film.jpg",
                            "exact_release_name": rel1_name,
                            "indexer": "TorrentLeech",
                        },
                        headers={"X-Internal-Token": internal_secret},
                    )
                    assert r5a.json()["status"] == "ambiguous_delivery"
                    r5a_eval = await ac.post(
                        "/internal/deliver/evaluation",
                        json={"release_id": "shadow-5a", "card_revision": 1, "error_redacted": "err"},
                        headers={"X-Internal-Token": internal_secret},
                    )
                    assert r5a_eval.status_code == 409

                    # 5f: Seed orphaned sending delivery + orphaned radarr_watched & radarr_release_push attempts,
                    # then shut down actions_app and boot a FRESH create_actions_app() with a new process_instance_id
                    # so the real ASGI lifespan performs startup orphan recovery and clean reconciliation (never 409 forever).
                    dead_watched_tok = "shadow-5f-orphan-watched-token"
                    dead_grab_tok = "shadow-5f-orphan-grab-token"
                    all_raw_action_tokens.extend([dead_watched_tok, dead_grab_tok])
                    orphan_watched_id = generate_uuid7()
                    orphan_grab_id = generate_uuid7()
                    orphan_grab_release = "Example.Film.2024.2160p.UHD.BluRay.REMUX.DV-5F-ORPHAN"

                    # Seed Radarr state representing already-applied Grab for Example Film (tmdb 100100)
                    radarr_stub.movies[44] = {
                        "id": 44,
                        "tmdbId": 100100,
                        "imdbId": "tt1001000",
                        "monitored": True,
                        "hasFile": False,
                        "tags": [],
                    }
                    radarr_stub.history.append(
                        {
                            "movieId": 44,
                            "eventType": "grabbed",
                            "sourceTitle": orphan_grab_release,
                            "date": utc_now_iso(),
                        }
                    )

                    # Orphan 1: deliveries.status = 'sending' left mid-flight by crashed process
                    actions_app.state.db.begin_card_delivery(
                        release_id="shadow-5f-sending",
                        card_revision=1,
                        delivery_kind="actionable_grab",
                        target_chat_id="-100111222333",
                        target_thread_id=20,
                        process_instance_id="shadow-proc-1",
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=300)),
                        new_actions=[],
                    )

                    # Orphan 2: radarr_watched attempt left 'attempting' after Radarr already applied watched+unmonitored
                    actions_app.state.db.begin_card_delivery(
                        release_id="shadow-5f-watched",
                        card_revision=1,
                        delivery_kind="actionable_grab",
                        target_chat_id="-100111222333",
                        target_thread_id=20,
                        process_instance_id="shadow-proc-1",
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=300)),
                        new_actions=[
                            NewActionRecord(
                                action_id=orphan_watched_id,
                                token_sha256=hash_token(dead_watched_tok),
                                release_id="shadow-5f-watched",
                                card_revision=1,
                                source_occurrence_id="occ-5f-watched",
                                action_type="watched_good",
                                completion_group="watched:shadow-5f-watched:400401",
                                bound_tmdb_id=400401,
                                bound_imdb_id="tt4004010",
                                bound_title="Already Watched",
                                bound_year=2024,
                                bound_release_name="Already.Watched.2024.REMUX",
                                bound_existing_movie_id=None,
                                bound_existing_file_id=None,
                                bound_existing_file_quality=None,
                                bound_rejection_class=None,
                                expires_at=utc_now_iso(utc_now() + timedelta(hours=1)),
                            )
                        ],
                    )
                    actions_app.state.db.finish_card_delivery_success(
                        "shadow-5f-watched",
                        card_revision=1,
                        delivery_kind="actionable_grab",
                        ack_chat_id="-100111222333",
                        ack_message_id=9501,
                        ack_thread_id=20,
                        used_poster=False,
                        is_edit=False,
                    )
                    actions_app.state.db.begin_write_attempt(
                        action_id=orphan_watched_id,
                        completion_group="watched:shadow-5f-watched:400401",
                        phase="radarr_watched",
                        process_instance_id="shadow-proc-1",
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=300)),
                        request_fingerprint='{"tmdb_id":400401}',
                        allowed_action_statuses=frozenset({"active"}),
                    )

                    # Orphan 3: radarr_release_push attempt left 'attempting' after Radarr already grabbed release
                    actions_app.state.db.begin_card_delivery(
                        release_id="shadow-5f-grab",
                        card_revision=1,
                        delivery_kind="actionable_grab",
                        target_chat_id="-100111222333",
                        target_thread_id=20,
                        process_instance_id="shadow-proc-1",
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=300)),
                        new_actions=[
                            NewActionRecord(
                                action_id=orphan_grab_id,
                                token_sha256=hash_token(dead_grab_tok),
                                release_id="shadow-5f-grab",
                                card_revision=1,
                                source_occurrence_id="occ-5f-grab",
                                action_type="add_grab",
                                completion_group="grab:shadow-5f-grab:100100",
                                bound_tmdb_id=100100,
                                bound_imdb_id="tt1001000",
                                bound_title="Example Film",
                                bound_year=2024,
                                bound_release_name=orphan_grab_release,
                                bound_existing_movie_id=None,
                                bound_existing_file_id=None,
                                bound_existing_file_quality=None,
                                bound_rejection_class=None,
                                expires_at=utc_now_iso(utc_now() + timedelta(hours=1)),
                            )
                        ],
                    )
                    actions_app.state.db.finish_card_delivery_success(
                        "shadow-5f-grab",
                        card_revision=1,
                        delivery_kind="actionable_grab",
                        ack_chat_id="-100111222333",
                        ack_message_id=9502,
                        ack_thread_id=20,
                        used_poster=False,
                        is_edit=False,
                    )
                    actions_app.state.db.begin_write_attempt(
                        action_id=orphan_grab_id,
                        completion_group="grab:shadow-5f-grab:100100",
                        phase="radarr_release_push",
                        process_instance_id="shadow-proc-1",
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=300)),
                        request_fingerprint='{"tmdb_id":100100,"release":"' + orphan_grab_release + '"}',
                        allowed_action_statuses=frozenset({"active"}),
                    )

        # Boot a brand-new movie-actions-api instance with a new process_instance_id against the same actions.db
        restarted_actions_app = create_actions_app(
            actions_cfg,
            process_instance_id="shadow-proc-2-restarted",
            radarr_client=radarr_stub,
            telegram_client=tg_stub,
            intake_push_client=IntakePushClient(
                base_url="http://intake.local", internal_token=internal_secret, http_client=a2i_http
            ),
            triage_feedback_client=feedback_client,
        )
        async with restarted_actions_app.router.lifespan_context(restarted_actions_app):
            # Startup lifespan MUST have automatically promoted all 3 orphaned records (no manual DB call!)
            assert restarted_actions_app.state.db.get_delivery("shadow-5f-sending")["status"] == "ambiguous_delivery"
            assert (
                restarted_actions_app.state.db.get_action_group_readonly(hash_token(dead_watched_tok)).action["status"]
                == "ambiguous_radarr"
            )
            assert (
                restarted_actions_app.state.db.get_action_group_readonly(hash_token(dead_grab_tok)).action["status"]
                == "ambiguous_radarr"
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted_actions_app), base_url="http://actions.local"
            ) as rac:
                updates_before_5f = len(radarr_stub.update_calls)
                pushes_before_5f = len(radarr_stub.push_calls)
                feedback_before_5f = len(triage_main.db.get_recent_feedback())

                # Reconcile applied Watched via fresh restarted process -> 0 new Radarr updates, 1 /feedback
                r5f_watched = await rac.post(f"/action/{dead_watched_tok}", data={"confirm": "1", "reconcile": "1"})
                assert r5f_watched.status_code == 200
                assert len(radarr_stub.update_calls) == updates_before_5f
                assert (
                    restarted_actions_app.state.db.get_action_group_readonly(hash_token(dead_watched_tok)).action["status"]
                    == "completed_all"
                )

                # Reconcile applied Grab via fresh restarted process -> 0 new Radarr pushes, 1 /feedback
                r5f_grab = await rac.post(f"/action/{dead_grab_tok}", data={"confirm": "1", "reconcile": "1"})
                assert r5f_grab.status_code == 200
                assert len(radarr_stub.push_calls) == pushes_before_5f
                assert (
                    restarted_actions_app.state.db.get_action_group_readonly(hash_token(dead_grab_tok)).action["status"]
                    == "completed_all"
                )
                assert len(triage_main.db.get_recent_feedback()) == feedback_before_5f + 2

        cases["case_5_lost_acks_and_recovery"] = {
            "passed": True,
            "subcases": ["5a", "5b", "5c", "5d", "5e", "5f"],
            "restarted_process_verified": True,
        }

        # ---------------------------------------------------------
        # ZERO-SECRET & PERMISSION HYGIENE AUDIT (SQLite + Archive)
        # ---------------------------------------------------------
        archive_files = list(intake_cfg.webhook_archive_dir.rglob("*.json"))
        assert len(archive_files) >= 10
        for af in archive_files:
            assert (os.lstat(af).st_mode & 0o077) == 0, f"Unsafe archive mode on {af}"
        for db_path in (intake_cfg.intake_db_path, actions_cfg.actions_db_path, triage_db_path):
            assert (os.lstat(db_path).st_mode & 0o077) == 0, f"Unsafe mode on {db_path}"
            raw = db_path.read_bytes()
            assert synthetic_passkey.encode("utf-8") not in raw
            assert b"SHADOW_RADARR_KEY" not in raw
            assert b"SHADOW_TELEGRAM_BOT_TOKEN_DO_NOT_LEAK" not in raw
            for tok in all_raw_action_tokens:
                assert tok.encode("utf-8") not in raw
        cases["zero_secret_and_permission_audit"] = {
            "passed": True,
            "checked_tokens": len(all_raw_action_tokens),
            "checked_archives": len(archive_files),
        }

        await triage_http.aclose()
        await i2a_http.aclose()
        await a2i_http.aclose()
        return ShadowSuiteResult(passed=all(c["passed"] for c in cases.values()), cases=cases)
    finally:
        triage_main.db = orig_triage_db
        triage_main.INTERNAL_API_SECRET = orig_triage_secret


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run 5-case pre-cutover shadow verification suite")
    parser.add_argument("--state-dir", default="", help="Optional isolated absolute state directory")
    parser.add_argument("--json", action="store_true", help="Output structured JSON report")
    args = parser.parse_args(argv)

    if args.state_dir:
        target = Path(args.state_dir)
        if not target.is_absolute():
            print("Error: --state-dir must be an absolute path", file=sys.stderr)
            return 1
        result = asyncio.run(run_shadow_suite(target))
    else:
        with tempfile.TemporaryDirectory(prefix="movie-intel-shadow-") as tmpdir:
            result = asyncio.run(run_shadow_suite(Path(tmpdir)))

    if args.json:
        print(json.dumps({"passed": result.passed, "cases": result.cases}, indent=2))
    else:
        for name, info in result.cases.items():
            mark = "PASS" if info.get("passed") else "FAIL"
            print(f"[{mark}] {name}: {json.dumps(info)}")
        print(f"\nShadow Verification Overall: {'PASS' if result.passed else 'FAIL'}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
