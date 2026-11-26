from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

import db as triage_db_mod
import main as triage_main
from services.movie_actions_api.alternative_grab import (
    QBittorrentClient,
    QbitAddResult,
    ShadowStubQbitClient,
    ShadowStubTorrentFetcher,
    TorrentFetcher,
    TorrentMetainfoError,
    _build_synthetic_v1_torrent,
    build_movie_file_snapshot,
    compare_movie_file_snapshots,
    is_canonical_tl_download_url,
    parse_v1_torrent_info_hash,
    read_alt_grab_receipt,
    validate_radarr_qbit_route,
)
from services.movie_actions_api.app import create_app as create_actions_app
from services.movie_actions_api.clients import (
    FeedbackPostResult,
    IntakePushClient,
    IntakePushPayloadResult,
    RadarrClient,
    RadarrReadResult,
    RadarrWriteResult,
    ShadowStubIntakePushClient,
    ShadowStubRadarrClient,
    ShadowStubTelegramClient,
    ShadowStubTriageFeedbackClient,
    TelegramClient,
    TelegramSendResult,
    TriageFeedbackClient,
)
from services.movie_actions_api.config import ActionsConfig, AlternativeGrabConfig
from services.movie_actions_api.db import ActionsDB, NewActionRecord, generate_uuid7, utc_now, utc_now_iso
from services.movie_actions_api.security import PrivateFileError, hash_token, redact_secrets
from services.movie_actions_api.telegram_format import (
    TELEGRAM_CAPTION_UTF16_LIMIT,
    TELEGRAM_TEXT_UTF16_LIMIT,
    build_card_html,
    format_evaluation_reply,
    utf16_len,
)
from services.movie_intake_api.app import create_app as create_intake_app
from services.movie_intake_api.clients import ActionsApiClient, TriageApiClient, TriageEvalResult
from services.movie_intake_api.config import IntakeConfig
from services.movie_intake_api.tmdb_verifier import TmdbIndependentClient, TmdbMovieRecord
from services.movie_intake_api.torrentleech import TorrentLeechResult, TorrentLeechScraper


def _verified_card_payload(
    *,
    release_id: str = "rel-001",
    card_revision: int = 1,
    source_occurrence_id: str = "occ-001",
    delivery_kind: str = "actionable_grab",
    tmdb_id: int = 550101,
    imdb_id: str = "tt5501010",
    title: str = "Festival Gem",
    year: int = 2025,
    release_name: str = "Festival.Gem.2025.2160p.UHD.BluRay.REMUX.DV.HDR10-CiNEPHiLES",
    external_id: str = "9001001",
    poster_path: Optional[str] = "/poster_ok.jpg",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "release_id": release_id,
        "card_revision": card_revision,
        "source_occurrence_id": source_occurrence_id,
        "delivery_kind": delivery_kind,
        "identity_gate_status": "verified" if delivery_kind != "evidence_review" else "evidence_review",
        "exact_release_name": release_name,
        "indexer": "TorrentLeech",
        "external_id": external_id,
        "category": "Movies :: 4K",
        "size_gb": 58.4,
        "freeleech": True,
        "tl_outcome": "listing_id_found",
        "radarr_status": "absent" if delivery_kind == "actionable_grab" else "owned_file",
        "radarr_note": None,
        "conflict_reason": None,
        "file_runtime_minutes": 114,
        "audio_languages": ["English"],
    }
    if delivery_kind != "evidence_review":
        payload["verified_tmdb_id"] = tmdb_id
        payload["verified_imdb_id"] = imdb_id
        payload["verified_title"] = title
        payload["verified_year"] = year
        payload["poster_path"] = poster_path
    if extra:
        payload.update(extra)
    return payload


def _extract_tokens_from_markup(reply_markup: dict[str, Any]) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for row in reply_markup.get("inline_keyboard") or []:
        for btn in row:
            text = str(btn.get("text") or "")
            url = str(btn.get("url") or "")
            tok = urlparse(url).path.rsplit("/", 1)[-1]
            if "Add & Grab" in text:
                tokens["add_grab"] = tok
            elif "Grab Anyway" in text:
                tokens["alternative_grab"] = tok
            elif "Watched – Good" in text:
                tokens["watched_good"] = tok
            elif "Watched – Bad" in text:
                tokens["watched_bad"] = tok
            elif "Skip" in text:
                tokens["skip"] = tok
    return tokens


class ControllableRadarrStub(ShadowStubRadarrClient):
    """Shadow-flagged Radarr stub supporting deterministic fault injection."""

    def __init__(self) -> None:
        super().__init__()
        self.push_outcome_queue: list[RadarrWriteResult] = []
        self.add_outcome_queue: list[RadarrWriteResult] = []
        self.update_outcome_queue: list[RadarrWriteResult] = []
        self.readback_override: Optional[RadarrReadResult] = None
        self.fail_reads: bool = False
        self.delay_update_event: Optional[asyncio.Event] = None
        self.update_started_event: Optional[asyncio.Event] = None
        self.readback_calls: int = 0

    async def get_tags(self) -> RadarrReadResult:
        if self.fail_reads:
            return RadarrReadResult(ok=False, status_code=503, error_redacted="radarr_http_503")
        return await super().get_tags()

    async def get_movies_by_tmdb(self, tmdb_id: int) -> RadarrReadResult:
        if self.fail_reads:
            return RadarrReadResult(ok=False, status_code=503, error_redacted="radarr_http_503")
        return await super().get_movies_by_tmdb(tmdb_id)

    async def get_movie_by_id(self, movie_id: int) -> RadarrReadResult:
        self.readback_calls += 1
        if self.readback_override is not None:
            return self.readback_override
        return await super().get_movie_by_id(movie_id)

    async def add_movie(self, payload: dict[str, Any]) -> RadarrWriteResult:
        if self.add_outcome_queue:
            self.add_calls.append(dict(payload))
            return self.add_outcome_queue.pop(0)
        return await super().add_movie(payload)

    async def update_movie(self, movie_id: int, payload: dict[str, Any]) -> RadarrWriteResult:
        if self.update_started_event is not None:
            self.update_started_event.set()
        if self.delay_update_event is not None:
            await self.delay_update_event.wait()
        if self.update_outcome_queue:
            self.update_calls.append({"movie_id": movie_id, "payload": dict(payload)})
            return self.update_outcome_queue.pop(0)
        return await super().update_movie(movie_id, payload)

    async def push_release(self, push_payload: dict[str, Any]) -> RadarrWriteResult:
        if self.push_outcome_queue:
            self.push_calls.append(dict(push_payload))
            return self.push_outcome_queue.pop(0)
        return await super().push_release(push_payload)


class ControllableTelegramStub(ShadowStubTelegramClient):
    """Shadow-flagged Telegram stub supporting deterministic photo/timeout fault injection."""

    def __init__(self) -> None:
        super().__init__()
        self.photo_outcome_queue: list[TelegramSendResult] = []
        self.message_outcome_queue: list[TelegramSendResult] = []
        self.doc_outcome_queue: list[TelegramSendResult] = []

    async def send_photo(self, **kwargs: Any) -> TelegramSendResult:
        if self.photo_outcome_queue:
            self.sent_photos.append(dict(kwargs))
            return self.photo_outcome_queue.pop(0)
        return await super().send_photo(**kwargs)

    async def send_message(self, **kwargs: Any) -> TelegramSendResult:
        if self.message_outcome_queue:
            self.sent_messages.append(dict(kwargs))
            return self.message_outcome_queue.pop(0)
        return await super().send_message(**kwargs)

    async def send_document(self, **kwargs: Any) -> TelegramSendResult:
        if self.doc_outcome_queue:
            self.sent_documents.append(dict(kwargs))
            return self.doc_outcome_queue.pop(0)
        return await super().send_document(**kwargs)


class ControllableFeedbackStub(ShadowStubTriageFeedbackClient):
    def __init__(self) -> None:
        super().__init__()
        self.outcome_queue: list[FeedbackPostResult] = []

    async def post_feedback(self, **kwargs: Any) -> FeedbackPostResult:
        self.calls.append(dict(kwargs))
        if self.outcome_queue:
            return self.outcome_queue.pop(0)
        expected = str(kwargs["decision"]).strip().upper()
        return FeedbackPostResult(saved=True, decision_saved=expected)


class TestMovieActionsApi(unittest.IsolatedAsyncioTestCase):
    def _make_config(
        self,
        tmpdir: str,
        *,
        internal_secret: str = "test-internal-secret-999",
        alt_policy_path: Optional[Path] = None,
        standalone_skip: bool = True,
        shadow_replay: bool = False,
        action_ttl: int = 3600,
    ) -> ActionsConfig:
        base = Path(tmpdir)
        return ActionsConfig(
            host="127.0.0.1",
            port=40721,
            public_base_url="https://seedbox.example.invalid/movie-intel",
            internal_api_secret=internal_secret,
            radarr_url="https://seedbox.example.invalid/user/radarr",
            radarr_api_key="RADARR_SECRET_KEY_VALUE",
            telegram_bot_token="123456789:AAH_SECRET_TELEGRAM_BOT_TOKEN_XYZ",
            telegram_chat_id="-100999888777",
            telegram_topic_id=10,
            telegram_add_grab_topic_id=20,
            telegram_grab_anyway_topic_id=30,
            action_ttl_seconds=action_ttl,
            standalone_skip_enabled=standalone_skip,
            alternative_grab_config_path=alt_policy_path,
            actions_db_path=base / "state" / "actions.db",
            intake_api_url="http://127.0.0.1:40720",
            triage_api_url="http://127.0.0.1:40716",
            shadow_replay_enabled=shadow_replay,
        )

    def _write_alt_grab_config(
        self,
        tmpdir: str,
        overrides: Optional[dict[str, Any]] = None,
        *,
        filename: str = "alt-grab.json",
        mode: int = 0o600,
    ) -> Path:
        base = Path(tmpdir)
        payload: dict[str, Any] = {
            "url": "https://qbit.seedbox.invalid/qbittorrent",
            "username": "qbit_secret_user",
            "password": "qbit_secret_password_999",
            "category": "movies",
            "state": str(base / "alt-grab-state"),
        }
        if overrides:
            payload.update(overrides)
        p = base / filename
        p.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(p, mode)
        return p

    async def test_config_loopback_paths_auth_headers_and_shadow_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Non-loopback bind rejected
            with self.assertRaises(ValueError):
                ActionsConfig.from_env({"ACTIONS_HOST": "0.0.0.0"})
            # Relative and unexpanded ~ path overrides rejected
            with self.assertRaises(ValueError):
                ActionsConfig.from_env({"ACTIONS_DB_PATH": "relative/actions.db"})
            with self.assertRaises(ValueError):
                ActionsConfig.from_env({"ACTIONS_DB_PATH": "~/.local/state/movie-intel/actions.db"})
            # Non-HTTPS non-loopback public base URL rejected
            with self.assertRaises(ValueError):
                ActionsConfig.from_env({"MOVIE_INTEL_PUBLIC_BASE_URL": "http://public.example.com"})
            # Alternative Grab live 5-key HTTPS config validation
            valid_cfg_path = self._write_alt_grab_config(tmpdir, filename="valid-alt.json")
            alt_cfg = AlternativeGrabConfig.load_from_file(valid_cfg_path)
            assert alt_cfg is not None
            self.assertEqual(alt_cfg.url, "https://qbit.seedbox.invalid/qbittorrent")
            self.assertEqual(alt_cfg.category, "movies")
            # Credentials hidden from repr()
            self.assertNotIn("qbit_secret_password_999", repr(alt_cfg))
            self.assertNotIn("qbit_secret_user", repr(alt_cfg))

            # Non-HTTPS qBittorrent URL rejected
            http_cfg = self._write_alt_grab_config(
                tmpdir, {"url": "http://qbit.seedbox.invalid/qbittorrent"}, filename="http-alt.json"
            )
            with self.assertRaises(ValueError):
                AlternativeGrabConfig.load_from_file(http_cfg)
            # Relative state path rejected
            rel_state_cfg = self._write_alt_grab_config(
                tmpdir, {"state": "relative/state"}, filename="rel-alt.json"
            )
            with self.assertRaises(ValueError):
                AlternativeGrabConfig.load_from_file(rel_state_cfg)
            # World-readable config rejected
            world_cfg = self._write_alt_grab_config(tmpdir, filename="world-alt.json", mode=0o644)
            with self.assertRaises(PrivateFileError):
                AlternativeGrabConfig.load_from_file(world_cfg)
            # Symlinked config rejected
            sym_cfg = Path(tmpdir) / "sym-alt.json"
            sym_cfg.symlink_to(valid_cfg_path)
            with self.assertRaises(PrivateFileError):
                AlternativeGrabConfig.load_from_file(sym_cfg)

            # Shadow mode refuses default production state directory
            prod_shadow_cfg = ActionsConfig.from_env({"SHADOW_REPLAY_ENABLED": "true"})
            with self.assertRaises(ValueError):
                create_actions_app(prod_shadow_cfg)

            # Shadow mode refuses non-stub clients (Radarr, TorrentFetcher, QBittorrentClient)
            iso_shadow_cfg = self._make_config(tmpdir, shadow_replay=True)
            with self.assertRaises(ValueError):
                create_actions_app(
                    iso_shadow_cfg,
                    radarr_client=RadarrClient(base_url="http://127.0.0.1:7878", api_key="k"),
                )
            with self.assertRaises(ValueError):
                create_actions_app(
                    iso_shadow_cfg,
                    torrent_fetcher=TorrentFetcher(),
                )
            with self.assertRaises(ValueError):
                create_actions_app(
                    iso_shadow_cfg,
                    alt_grab_config=alt_cfg,
                    qbit_client=QBittorrentClient(
                        base_url="https://qbit.seedbox.invalid", username="u", password="p"
                    ),
                )

            # Shadow mode auto-wires zero-mutation stubs cleanly
            shadow_app = create_actions_app(iso_shadow_cfg, alt_grab_config=alt_cfg)
            self.assertTrue(shadow_app.state.radarr_client.is_shadow_stub)
            self.assertTrue(shadow_app.state.telegram_client.is_shadow_stub)
            self.assertTrue(shadow_app.state.intake_push_client.is_shadow_stub)
            self.assertTrue(shadow_app.state.torrent_fetcher.is_shadow_stub)
            self.assertTrue(shadow_app.state.qbit_client.is_shadow_stub)
            self.assertTrue(shadow_app.state.triage_feedback_client.is_shadow_stub)

            # Symlinked DB rejected
            state_dir = Path(tmpdir) / "symstate"
            state_dir.mkdir(mode=0o700)
            real_db = state_dir / "real.db"
            real_db.touch()
            os.chmod(real_db, 0o600)
            sym_db = state_dir / "actions.db"
            sym_db.symlink_to(real_db)
            with self.assertRaises(PrivateFileError):
                ActionsDB(sym_db).init_db()

            # Auth: missing secret -> 503; wrong token -> 401
            no_auth_cfg = self._make_config(tmpdir, internal_secret="")
            app_no_auth = create_actions_app(
                no_auth_cfg,
                radarr_client=ControllableRadarrStub(),
                telegram_client=ControllableTelegramStub(),
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=ControllableFeedbackStub(),
            )
            async with app_no_auth.router.lifespan_context(app_no_auth):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_no_auth), base_url="http://test"
                ) as client:
                    r503 = await client.get("/internal/radarr/status", params={"tmdb_id": 1})
                    self.assertEqual(r503.status_code, 503)

            cfg = self._make_config(tmpdir)
            app = create_actions_app(
                cfg,
                radarr_client=ControllableRadarrStub(),
                telegram_client=ControllableTelegramStub(),
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=ControllableFeedbackStub(),
            )
            async with app.router.lifespan_context(app):
                # Verify 0700 dir and 0600 db permissions
                self.assertEqual(os.lstat(cfg.actions_db_path.parent).st_mode & 0o777, 0o700)
                self.assertEqual(os.lstat(cfg.actions_db_path).st_mode & 0o777, 0o600)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    r401 = await client.get("/internal/radarr/status", params={"tmdb_id": 1})
                    self.assertEqual(r401.status_code, 401)
                    r_health = await client.get("/health")
                    self.assertEqual(r_health.status_code, 200)
                    self.assertEqual(r_health.json()["status"], "ok")

    async def test_defense_in_depth_refuses_unverified_buttons_and_zero_secrets_in_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            intake_push = ShadowStubIntakePushClient()
            fb = ControllableFeedbackStub()
            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=tg,
                intake_push_client=intake_push,
                triage_feedback_client=fb,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # Unverified identity_gate_status MUST be refused (400)
                    bad1 = _verified_card_payload(release_id="bad-1")
                    bad1["identity_gate_status"] = "evidence_review"
                    r = await client.post("/internal/deliver/card", json=bad1, headers=headers)
                    self.assertEqual(r.status_code, 400)

                    # Missing verified_tmdb_id MUST be refused (400)
                    bad2 = _verified_card_payload(release_id="bad-2")
                    bad2["verified_tmdb_id"] = None
                    r = await client.post("/internal/deliver/card", json=bad2, headers=headers)
                    self.assertEqual(r.status_code, 400)

                    # Conflict reason present MUST be refused (400)
                    bad3 = _verified_card_payload(release_id="bad-3")
                    bad3["conflict_reason"] = "tracker_imdb_conflict"
                    r = await client.post("/internal/deliver/card", json=bad3, headers=headers)
                    self.assertEqual(r.status_code, 400)

                    # Alternative Grab when policy disabled MUST be refused (400)
                    bad4 = _verified_card_payload(
                        release_id="bad-4",
                        delivery_kind="alternative_grab",
                        extra={
                            "bound_existing_movie_id": 10,
                            "bound_existing_file_id": 20,
                            "bound_rejection_class": "cutoff_met",
                        },
                    )
                    r = await client.post("/internal/deliver/card", json=bad4, headers=headers)
                    self.assertEqual(r.status_code, 400)
                    self.assertEqual(len(tg.sent_photos) + len(tg.sent_messages), 0)

                    # Valid actionable_grab delivers card with Add & Grab, Watched Good/Bad, Skip
                    good = _verified_card_payload(release_id="rel-good-1")
                    r_ok = await client.post("/internal/deliver/card", json=good, headers=headers)
                    self.assertEqual(r_ok.status_code, 200)
                    body = r_ok.json()
                    self.assertEqual(body["status"], "delivered")
                    self.assertEqual(len(tg.sent_photos), 1)
                    tokens = _extract_tokens_from_markup(tg.sent_photos[0]["reply_markup"])
                    self.assertEqual(set(tokens.keys()), {"add_grab", "watched_good", "watched_bad", "skip"})

                    # GET /action/{token} sets strict privacy headers and performs ZERO mutations
                    r_get = await client.get(f"/action/{tokens['add_grab']}")
                    self.assertEqual(r_get.status_code, 200)
                    self.assertIn("no-store", r_get.headers.get("cache-control", ""))
                    self.assertEqual(r_get.headers.get("referrer-policy"), "no-referrer")
                    self.assertEqual(r_get.headers.get("x-frame-options"), "DENY")
                    self.assertEqual(len(radarr.add_calls), 0)
                    self.assertEqual(len(radarr.push_calls), 0)
                    self.assertEqual(len(fb.calls), 0)

                    # Zero-secret audit across raw SQLite file bytes: NO raw tokens, NO API keys, NO downloadUrl!
                    db_bytes = cfg.actions_db_path.read_bytes()
                    for raw_tok in tokens.values():
                        self.assertNotIn(raw_tok.encode("utf-8"), db_bytes)
                        self.assertIn(hash_token(raw_tok).encode("utf-8"), db_bytes)
                    self.assertNotIn(cfg.radarr_api_key.encode("utf-8"), db_bytes)
                    self.assertNotIn(cfg.telegram_bot_token.encode("utf-8"), db_bytes)
                    self.assertNotIn(b"downloadUrl", db_bytes)

    async def test_telegram_utf16_limits_escaping_long_txt_topic_routing_and_revision_edit(self) -> None:
        # Non-BMP emoji + HTML injection characters strictly budgeted in UTF-16 code units
        long_title = "🎬🔥<script>alert('&\"x\"')</script> " + ("🎥" * 400)
        card = _verified_card_payload(
            release_id="rel-utf16",
            title=long_title,
            release_name="The-Hyphenated-Movie-Title-2024-2160p-UHD-BluRay-REMUX-CiNEPHiLES",
        )
        caption = build_card_html(card, max_utf16_units=TELEGRAM_CAPTION_UTF16_LIMIT)
        self.assertLessEqual(utf16_len(caption), TELEGRAM_CAPTION_UTF16_LIMIT)
        self.assertNotIn("<script>", caption)
        self.assertIn("&lt;script&gt;", caption)
        self.assertIn("The-Hyphenated-Movie-Title-2024-2160p-UHD-BluRay-REMUX-CiNEPHiLES", caption)

        # Redactor scrubs Telegram bot tokens & action paths while keeping hyphenated release names intact
        scrubbed = redact_secrets(
            "Error calling https://api.telegram.org/bot123456789:AAH_SECRET_TOKEN_1234567890/sendMessage "
            "for /action/rawSecretToken123 on The-Hyphenated-Movie-Title-2024-2160p-UHD-BluRay-REMUX"
        )
        self.assertNotIn("AAH_SECRET_TOKEN", scrubbed)
        self.assertNotIn("rawSecretToken123", scrubbed)
        self.assertIn("/action/[REDACTED]", scrubbed)
        self.assertIn("The-Hyphenated-Movie-Title-2024-2160p-UHD-BluRay-REMUX", scrubbed)

        # Long evaluation report > 4096 UTF-16 units triggers compact summary + .txt attachment
        long_eval = {
            "identification": {
                "canonical_title": "Epic Film",
                "year": 2025,
                "director": "Auteur Director",
                "match_confidence": "high",
            },
            "ratings": {"rotten_tomatoes_critics": 95, "imdb": 8.4, "letterboxd": 4.3},
            "recognition": {
                "awards_highlights": ["Cannes Palme d'Or"],
                "critic_consensus": "🔥 " + ("Critically acclaimed masterpiece. " * 220),
            },
            "decision_aid": {
                "one_line_pitch": "A sweeping dramatic portrait.",
                "verdict": "WATCH",
                "verdict_reason": "Matches taste profile strongly.",
                "similar_to": ["Parasite", "Anatomy of a Fall"],
                "red_flags": [],
            },
        }
        formatted = format_evaluation_reply(release_id="rel-long-eval", evaluation=long_eval, error_redacted=None)
        self.assertTrue(formatted.attach_txt)
        self.assertLessEqual(utf16_len(formatted.html_text), TELEGRAM_TEXT_UTF16_LIMIT)
        self.assertIn("Recommendation", formatted.html_text)
        self.assertIsNotNone(formatted.txt_filename)
        self.assertIn("Critically acclaimed masterpiece", formatted.txt_content or "")

        with tempfile.TemporaryDirectory() as tmpdir:
            alt_cfg_path = self._write_alt_grab_config(tmpdir)
            cfg = self._make_config(tmpdir, alt_policy_path=alt_cfg_path)
            tg = ControllableTelegramStub()
            app = create_actions_app(
                cfg,
                radarr_client=ControllableRadarrStub(),
                telegram_client=tg,
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=ControllableFeedbackStub(),
                torrent_fetcher=ShadowStubTorrentFetcher(),
                qbit_client=ShadowStubQbitClient(category="movies"),
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # Verify GET /internal/capabilities auth + boolean response
                    r_cap_unauth = await client.get("/internal/capabilities")
                    self.assertEqual(r_cap_unauth.status_code, 401)
                    r_cap = await client.get("/internal/capabilities", headers=headers)
                    self.assertEqual(r_cap.status_code, 200)
                    self.assertEqual(r_cap.json(), {"alternative_grab_configured": True})

                    # 1. Actionable Grab -> TELEGRAM_ADD_GRAB_TOPIC_ID (20)
                    r1 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-topic-1", poster_path=None),
                        headers=headers,
                    )
                    self.assertEqual(r1.status_code, 200)
                    ack1 = r1.json()
                    self.assertEqual(ack1["ack_thread_id"], 20)
                    rev1_tokens = _extract_tokens_from_markup(tg.sent_messages[0]["reply_markup"])

                    # 2. Alternative Grab -> TELEGRAM_GRAB_ANYWAY_TOPIC_ID (30)
                    r2 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-topic-2",
                            delivery_kind="alternative_grab",
                            poster_path=None,
                            extra={
                                "bound_existing_movie_id": 11,
                                "bound_existing_file_id": 22,
                                "bound_rejection_class": "cutoff_met",
                                "rejection_reasons": ["Existing file meets cutoff: Remux-1080p"],
                                "bound_movie_file_snapshot": {
                                    "movie_id": 11,
                                    "file_id": 22,
                                    "monitored": True,
                                    "quality_profile_id": 1,
                                    "relative_path": "Festival.Gem.2025.Remux-1080p.mkv",
                                    "edition": "",
                                    "release_group": "FraMeSToR",
                                    "size": 31_000_000_000,
                                    "quality": "Remux-1080p",
                                },
                            },
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r2.status_code, 200)
                    self.assertEqual(r2.json()["ack_thread_id"], 30)

                    # 3. Evidence Review -> TELEGRAM_TOPIC_ID (10) with ZERO buttons
                    r3 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-topic-3",
                            delivery_kind="evidence_review",
                            extra={"conflict_reason": "imdb_mismatch"},
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r3.status_code, 200)
                    self.assertEqual(r3.json()["ack_thread_id"], 10)
                    self.assertEqual(tg.sent_messages[-1]["reply_markup"], {"inline_keyboard": []})

                    # 4. Deliver long evaluation reply -> sends summary message + .txt document bound to ack
                    r_eval = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-topic-1",
                            "card_revision": 1,
                            "ack_chat_id": ack1["ack_chat_id"],
                            "ack_message_id": ack1["ack_message_id"],
                            "ack_thread_id": ack1["ack_thread_id"],
                            "evaluation": long_eval,
                            "error_redacted": None,
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_eval.status_code, 200)
                    self.assertEqual(r_eval.json()["status"], "sent")
                    self.assertEqual(len(tg.sent_documents), 1)
                    self.assertEqual(tg.sent_documents[0]["reply_to_message_id"], ack1["ack_message_id"])
                    self.assertEqual(tg.sent_documents[0]["thread_id"], 20)

                    # 5. Higher card_revision=2 edits persisted message, supersedes rev1 tokens, rejects stale rev1 eval
                    r_edit = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-topic-1",
                            card_revision=2,
                            delivery_kind="evidence_review",
                            extra={"conflict_reason": "evaluator_conflict"},
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_edit.status_code, 200)
                    self.assertEqual(len(tg.edited_texts), 1)
                    self.assertEqual(tg.edited_texts[0]["message_id"], ack1["ack_message_id"])
                    self.assertEqual(tg.edited_texts[0]["reply_markup"], {"inline_keyboard": []})

                    # Old rev 1 token is now 410 Gone (superseded)
                    r_old_tok = await client.get(f"/action/{rev1_tokens['add_grab']}")
                    self.assertEqual(r_old_tok.status_code, 410)

                    # Stale rev 1 evaluation reply rejected (409)
                    r_stale_eval = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-topic-1",
                            "card_revision": 1,
                            "ack_chat_id": ack1["ack_chat_id"],
                            "ack_message_id": ack1["ack_message_id"],
                            "evaluation": long_eval,
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_stale_eval.status_code, 409)

    async def test_radarr_status_matrix_including_provider_prefix_and_failures(self) -> None:
        # Verify RadarrClient preserves provider URL prefixes on wire requests
        seen_urls: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            self.assertEqual(request.headers.get("X-Api-Key"), "secret-key")
            if request.url.path.endswith("/user/radarr/api/v3/tag"):
                return httpx.Response(200, json=[{"id": 9, "label": "watched"}])
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http_client:
            rc = RadarrClient(
                base_url="https://seedbox.invalid/user/radarr",
                api_key="secret-key",
                http_client=http_client,
            )
            tags = await rc.get_tags()
            self.assertTrue(tags.ok)
            self.assertEqual(seen_urls[0], "https://seedbox.invalid/user/radarr/api/v3/tag")

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            radarr = ControllableRadarrStub()
            radarr.movies = {
                1: {"id": 1, "tmdbId": 1001, "imdbId": "tt0001001", "monitored": False, "hasFile": False, "tags": [7]},
                2: {
                    "id": 2,
                    "tmdbId": 1002,
                    "imdbId": "tt0001002",
                    "monitored": True,
                    "hasFile": True,
                    "tags": [],
                    "movieFile": {"id": 222, "quality": {"quality": {"name": "Remux-2160p"}}},
                },
                3: {"id": 3, "tmdbId": 1003, "imdbId": "tt0001003", "monitored": True, "hasFile": False, "tags": []},
                4: {"id": 4, "tmdbId": 1004, "imdbId": "tt9999999", "monitored": True, "hasFile": False, "tags": []},
            }
            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=ControllableTelegramStub(),
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=ControllableFeedbackStub(),
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    self.assertEqual(
                        (await client.get("/internal/radarr/status", params={"tmdb_id": 1001}, headers=headers)).json()["status"],
                        "watched_unmonitored",
                    )
                    owned = (await client.get("/internal/radarr/status", params={"tmdb_id": 1002}, headers=headers)).json()
                    self.assertEqual(owned["status"], "owned_file")
                    self.assertEqual(owned["movie_id"], 2)
                    self.assertEqual(owned["file_id"], 222)
                    self.assertEqual(owned["existing_quality"], "Remux-2160p")
                    self.assertEqual(
                        (await client.get("/internal/radarr/status", params={"tmdb_id": 1003}, headers=headers)).json()["status"],
                        "monitored_no_file",
                    )
                    self.assertEqual(
                        (await client.get("/internal/radarr/status", params={"tmdb_id": 9999}, headers=headers)).json()["status"],
                        "absent",
                    )
                    # IMDb disagreement with Radarr movie -> ambiguous (NEVER suppress or treat absent)
                    self.assertEqual(
                        (
                            await client.get(
                                "/internal/radarr/status",
                                params={"tmdb_id": 1004, "imdb_id": "tt0001004"},
                                headers=headers,
                            )
                        ).json()["status"],
                        "ambiguous",
                    )
                    # Radarr HTTP 503 -> lookup_failed (NEVER absent!)
                    radarr.fail_reads = True
                    self.assertEqual(
                        (await client.get("/internal/radarr/status", params={"tmdb_id": 9999}, headers=headers)).json()["status"],
                        "lookup_failed",
                    )

    async def test_mandatory_case_5a_and_5e_lost_telegram_ack_and_poster_fallback(self) -> None:
        """Mandatory Shadow Case 5a (lost Telegram ack) and 5e (poster 400 -> text fallback)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            app = create_actions_app(
                cfg,
                radarr_client=ControllableRadarrStub(),
                telegram_client=tg,
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=ControllableFeedbackStub(),
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # --- 5a: Lost Telegram ack on sendPhoto -> ambiguous_delivery, ZERO fallback, ZERO retry ---
                    tg.photo_outcome_queue.append(
                        TelegramSendResult(outcome="ambiguous", error_redacted="telegram_transport_ambiguous:ReadTimeout")
                    )
                    r5a = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-5a"),
                        headers=headers,
                    )
                    self.assertEqual(r5a.status_code, 200)
                    self.assertEqual(r5a.json()["status"], "ambiguous_delivery")
                    self.assertEqual(len(tg.sent_photos), 1)
                    self.assertEqual(len(tg.sent_messages), 0)

                    # DB persists ambiguous_delivery with ack_message_id IS NULL
                    del_row = app.state.db.get_delivery("rel-5a")
                    self.assertIsNotNone(del_row)
                    self.assertEqual(del_row["status"], "ambiguous_delivery")
                    self.assertIsNone(del_row["ack_message_id"])

                    # Retry deliver_card MUST NOT resend!
                    r5a_retry = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-5a"),
                        headers=headers,
                    )
                    self.assertEqual(r5a_retry.json()["status"], "ambiguous_delivery")
                    self.assertEqual(len(tg.sent_photos), 1)
                    self.assertEqual(len(tg.sent_messages), 0)

                    # /internal/deliver/evaluation refuses unanchored reply (409)
                    r5a_eval = await client.post(
                        "/internal/deliver/evaluation",
                        json={"release_id": "rel-5a", "card_revision": 1, "error_redacted": "err"},
                        headers=headers,
                    )
                    self.assertEqual(r5a_eval.status_code, 409)
                    self.assertEqual(len(tg.sent_messages), 0)

                    # --- 5e: sendPhoto returns definitive HTTP 400 -> immediate sendMessage fallback preserving buttons ---
                    tg.photo_outcome_queue.append(
                        TelegramSendResult(outcome="failed_definitive", error_redacted="telegram_http_400:wrong_file_id")
                    )
                    r5e = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-5e"),
                        headers=headers,
                    )
                    self.assertEqual(r5e.status_code, 200)
                    body5e = r5e.json()
                    self.assertEqual(body5e["status"], "delivered")
                    self.assertEqual(len(tg.sent_photos), 2)
                    self.assertEqual(len(tg.sent_messages), 1)
                    # Exact same buttons preserved on fallback text card!
                    self.assertEqual(tg.sent_photos[-1]["reply_markup"], tg.sent_messages[-1]["reply_markup"])
                    self.assertEqual(app.state.db.get_delivery("rel-5e")["used_poster"], 0)

    async def test_mandatory_case_5b_and_5c_lost_radarr_ack_delayed_push_and_rejected_push(self) -> None:
        """Mandatory Shadow Case 5b (lost Radarr push ack + delayed grab) and 5c (HTTP 200 rejected push)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            intake_push = ShadowStubIntakePushClient()
            fb = ControllableFeedbackStub()
            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=tg,
                intake_push_client=intake_push,
                triage_feedback_client=fb,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # --- 5b: Lost ack during radarr_release_push + delayed original grab ---
                    rel_name = "Delayed.Grab.2025.2160p.UHD.BluRay.REMUX-CiNEPHiLES"
                    r_card = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-5b", tmdb_id=600501, title="Delayed Grab", release_name=rel_name
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_card.status_code, 200)
                    tokens = _extract_tokens_from_markup(tg.sent_photos[-1]["reply_markup"])
                    add_tok = tokens["add_grab"]

                    # Invalid root folder is rejected BEFORE committing attempting
                    r_bad_root = await client.post(
                        f"/action/{add_tok}",
                        data={"confirm": "1", "root_folder_path": "/not/a/radarr/root", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_bad_root.status_code, 400)
                    self.assertEqual(len(radarr.add_calls), 0)

                    # Inject lost ack (timeout) on /api/v3/release/push
                    radarr.push_outcome_queue.append(
                        RadarrWriteResult(outcome="ambiguous", error_redacted="radarr_push_ambiguous:ReadTimeout")
                    )
                    r_post = await client.post(
                        f"/action/{add_tok}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_post.status_code, 503)
                    self.assertEqual(len(radarr.add_calls), 1)
                    self.assertEqual(radarr.add_calls[0]["addOptions"], {"searchForMovie": False})
                    self.assertEqual(len(radarr.push_calls), 1)
                    self.assertEqual(len(fb.calls), 0)

                    # Verify actions.db contains ZERO downloadUrl even after push attempt
                    self.assertNotIn(b"downloadUrl", cfg.actions_db_path.read_bytes())
                    self.assertNotIn(b"shadow-tracker.invalid", cfg.actions_db_path.read_bytes())

                    # First reconciliation while queue & history are STILL EMPTY -> MUST retain ambiguous_radarr & 0 2nd pushes!
                    r_rec1 = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec1.status_code, 200)
                    self.assertIn("NO second release push", r_rec1.text)
                    self.assertEqual(len(radarr.push_calls), 1)
                    self.assertEqual(len(fb.calls), 0)
                    snap1 = app.state.db.get_action_group_readonly(hash_token(add_tok))
                    self.assertEqual(snap1.action["status"], "ambiguous_radarr")
                    self.assertIsNotNone(snap1.action["last_reconciled_at"])

                    # Now delayed original push lands in Radarr history!
                    added_movie_id = next(iter(radarr.movies.keys()))
                    radarr.history.append(
                        {
                            "movieId": added_movie_id,
                            "eventType": "grabbed",
                            "sourceTitle": rel_name,
                            "date": utc_now_iso(),
                        }
                    )

                    # Second reconciliation confirms grab -> seals completed_radarr with EXACTLY 1 push total, posts /feedback once!
                    r_rec2 = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec2.status_code, 200)
                    self.assertIn("evaluator rating saved (WATCH)", r_rec2.text)
                    self.assertEqual(len(radarr.push_calls), 1)
                    self.assertEqual(len(fb.calls), 1)
                    self.assertEqual(fb.calls[0]["decision"], "watch")
                    self.assertEqual(fb.calls[0]["note"], "Grabbed via Telegram")
                    snap2 = app.state.db.get_action_group_readonly(hash_token(add_tok))
                    self.assertEqual(snap2.action["status"], "completed_all")

                    # --- 5c: HTTP 200 rejected push (approved=false, rejected=true) ---
                    r_card5c = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-5c", tmdb_id=600502, title="Rejected Push"),
                        headers=headers,
                    )
                    self.assertEqual(r_card5c.status_code, 200)
                    tok5c = _extract_tokens_from_markup(tg.sent_photos[-1]["reply_markup"])["add_grab"]
                    radarr.push_outcome_queue.append(
                        RadarrWriteResult(
                            outcome="rejected",
                            data={"approved": False, "rejected": True, "rejections": ["Quality cutoff met"]},
                            status_code=200,
                            error_redacted="Radarr rejected release push: Quality cutoff met",
                        )
                    )
                    r_post5c = await client.post(
                        f"/action/{tok5c}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_post5c.status_code, 200)
                    self.assertIn("Quality cutoff met", r_post5c.text)
                    self.assertEqual(len(fb.calls), 1)  # ZERO new /feedback calls!
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(tok5c)).action["status"],
                        "rejected_push",
                    )

                    # Also verify RadarrClient.push_release() directly treats HTTP 200 rejected=true as rejected
                    def _rej_handler(_: httpx.Request) -> httpx.Response:
                        return httpx.Response(
                            200,
                            json=[{"approved": False, "rejected": True, "rejections": ["Quality cutoff met"]}],
                        )

                    async with httpx.AsyncClient(transport=httpx.MockTransport(_rej_handler)) as hc:
                        rc = RadarrClient(base_url="http://radarr.invalid", api_key="k", http_client=hc)
                        res = await rc.push_release({"title": "x", "downloadUrl": "https://x"})
                        self.assertEqual(res.outcome, "rejected")

    async def test_mandatory_case_5d_watched_shared_lock_readback_feedback_isolation_skip_and_alt_grab(self) -> None:
        """Mandatory Shadow Case 5d + Watched legacy unrated + Standalone Skip + Alternative Grab revalidation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            alt_cfg_path = self._write_alt_grab_config(tmpdir)
            cfg = self._make_config(tmpdir, alt_policy_path=alt_cfg_path)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            # Existing movie with an existing tag (id=42) and file -> watched MUST preserve tag 42 and file!
            radarr.movies[50] = {
                "id": 50,
                "tmdbId": 700101,
                "imdbId": "tt7001010",
                "title": "Watched Film",
                "monitored": True,
                "qualityProfileId": 1,
                "hasFile": True,
                "movieFile": {
                    "id": 888,
                    "relativePath": "Watched.Film.2025.Bluray-1080p.mkv",
                    "edition": "",
                    "releaseGroup": "FraMeSToR",
                    "size": 25_000_000_000,
                    "quality": {"quality": {"name": "Bluray-1080p"}},
                },
                "tags": [42],
            }
            fb = ControllableFeedbackStub()
            qbit = ShadowStubQbitClient(category="movies")
            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=tg,
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=fb,
                torrent_fetcher=ShadowStubTorrentFetcher(),
                qbit_client=qbit,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    r_card = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-5d",
                            tmdb_id=700101,
                            imdb_id="tt7001010",
                            title="Watched Film",
                            delivery_kind="alternative_grab",
                            extra={
                                "bound_existing_movie_id": 50,
                                "bound_existing_file_id": 888,
                                "bound_rejection_class": "cutoff_met",
                                "rejection_reasons": ["Existing file meets cutoff: Bluray-1080p"],
                                "bound_movie_file_snapshot": build_movie_file_snapshot(radarr.movies[50]),
                            },
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_card.status_code, 200)
                    tokens = _extract_tokens_from_markup(tg.sent_photos[-1]["reply_markup"])

                    # Concurrent POSTs to watched_good and watched_bad with /feedback failing (HTTP 502)
                    radarr.update_started_event = asyncio.Event()
                    radarr.delay_update_event = asyncio.Event()
                    fb.outcome_queue.append(FeedbackPostResult(saved=False, error_redacted="triage_feedback_http_502"))

                    async def _post_good() -> httpx.Response:
                        return await client.post(f"/action/{tokens['watched_good']}", data={"confirm": "1"})

                    async def _post_bad_after_start() -> httpx.Response:
                        await radarr.update_started_event.wait()
                        resp = await client.post(f"/action/{tokens['watched_bad']}", data={"confirm": "1"})
                        radarr.delay_update_event.set()
                        return resp

                    r_good, r_bad = await asyncio.gather(_post_good(), _post_bad_after_start())
                    self.assertEqual(r_good.status_code, 200)
                    self.assertEqual(r_bad.status_code, 409)  # Serialized by shared completion_group!
                    # Exactly 1 Radarr update + 1 mandatory read-back check
                    self.assertEqual(len(radarr.update_calls), 1)
                    self.assertEqual(radarr.readback_calls, 1)
                    # Existing tag 42 and existing file preserved, watched tag 7 added, monitored=False
                    self.assertEqual(radarr.movies[50]["monitored"], False)
                    self.assertEqual(set(radarr.movies[50]["tags"]), {42, 7})
                    self.assertTrue(radarr.movies[50]["hasFile"])
                    # Honest reporting: Radarr succeeded, feedback did NOT save!
                    self.assertIn("evaluator rating was NOT saved", r_good.text)
                    self.assertIn("Retry Evaluator Feedback Only", r_good.text)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(tokens["watched_good"])).action["status"],
                        "completed_radarr",
                    )

                    # Isolated feedback retry: ZERO additional Radarr writes, succeeds & seals completed_all!
                    r_retry_fb = await client.post(
                        f"/action/{tokens['watched_good']}",
                        data={"confirm": "1", "retry_feedback_only": "1"},
                    )
                    self.assertEqual(r_retry_fb.status_code, 200)
                    self.assertIn("evaluator rating saved (WATCH)", r_retry_fb.text)
                    self.assertEqual(len(radarr.update_calls), 1)  # ZERO second Radarr write!
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(tokens["watched_good"])).action["status"],
                        "completed_all",
                    )

                    # Clicking watched_bad now returns idempotent completion with ZERO Radarr or feedback writes!
                    fb_count_before = len(fb.calls)
                    r_bad_after = await client.post(f"/action/{tokens['watched_bad']}", data={"confirm": "1"})
                    self.assertEqual(r_bad_after.status_code, 200)
                    self.assertEqual(len(radarr.update_calls), 1)
                    self.assertEqual(len(fb.calls), fb_count_before)

                    # Alternative Grab now REFUSES (409) because movie 50 is now watched + unmonitored!
                    r_alt_refused = await client.post(f"/action/{tokens['alternative_grab']}", data={"confirm": "1"})
                    self.assertEqual(r_alt_refused.status_code, 409)
                    self.assertEqual(len(radarr.push_calls), 0)

                    # Standalone Skip: explicit POST -> decision="skip", ZERO Radarr mutations!
                    r_skip = await client.post(f"/action/{tokens['skip']}", data={"confirm": "1"})
                    self.assertEqual(r_skip.status_code, 200)
                    self.assertIn("No Radarr changes were made", r_skip.text)
                    self.assertEqual(fb.calls[-1]["decision"], "skip")
                    self.assertEqual(fb.calls[-1]["note"], "Skipped via Telegram")

                    # Watched Legacy Unrated: marks watched in Radarr + read-back, ZERO /feedback calls!
                    radarr.update_started_event = None
                    radarr.delay_update_event = None
                    legacy_tok = "legacy-unrated-token-001"
                    app.state.db.begin_card_delivery(
                        release_id="rel-legacy",
                        card_revision=1,
                        delivery_kind="actionable_grab",
                        target_chat_id="-100",
                        target_thread_id=10,
                        process_instance_id=app.state.process_instance_id,
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=60)),
                        new_actions=[
                            NewActionRecord(
                                action_id=generate_uuid7(),
                                token_sha256=hash_token(legacy_tok),
                                release_id="rel-legacy",
                                card_revision=1,
                                source_occurrence_id="occ-legacy",
                                action_type="watched_legacy_unrated",
                                completion_group="watched:rel-legacy:700202",
                                bound_tmdb_id=700202,
                                bound_imdb_id="tt7002020",
                                bound_title="Legacy Film",
                                bound_year=2020,
                                bound_release_name="Legacy.Film.2020.REMUX",
                                bound_existing_movie_id=None,
                                bound_existing_file_id=None,
                                bound_existing_file_quality=None,
                                bound_rejection_class=None,
                                expires_at=utc_now_iso(utc_now() + timedelta(hours=1)),
                            )
                        ],
                    )
                    fb_before_legacy = len(fb.calls)
                    r_leg = await client.post(f"/action/{legacy_tok}", data={"confirm": "1"})
                    self.assertEqual(r_leg.status_code, 200)
                    self.assertEqual(len(fb.calls), fb_before_legacy)  # ZERO /feedback for unrated link!
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(legacy_tok)).action["status"],
                        "completed_all",
                    )

    async def test_mandatory_case_5f_process_kill_startup_recovery_and_reconciliation(self) -> None:
        """Mandatory Shadow Case 5f: hard-kill after committing attempting/sending -> startup recovery -> reconcile with 0 duplicate writes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            db = ActionsDB(cfg.actions_db_path)
            db.init_db()

            dead_proc = "proc-dead-before-crash"
            future_lease = utc_now_iso(utc_now() + timedelta(seconds=600))
            expires_future = utc_now_iso(utc_now() + timedelta(hours=2))

            tok_push = "tok-crash-push"
            tok_watched = "tok-crash-watched"
            tok_fb = "tok-crash-fb"

            # Seed (i) orphaned radarr_release_push, (ii) orphaned radarr_watched, (iii) orphaned sending delivery, (iv) orphaned feedback
            act_push_id = generate_uuid7()
            act_watched_id = generate_uuid7()
            act_fb_id = generate_uuid7()

            db.begin_card_delivery(
                release_id="rel-crash-1",
                card_revision=1,
                delivery_kind="actionable_grab",
                target_chat_id="-100",
                target_thread_id=20,
                process_instance_id=dead_proc,
                lease_expires_at=future_lease,
                new_actions=[
                    NewActionRecord(
                        action_id=act_push_id,
                        token_sha256=hash_token(tok_push),
                        release_id="rel-crash-1",
                        card_revision=1,
                        source_occurrence_id="occ-1",
                        action_type="add_grab",
                        completion_group="grab:rel-crash-1:800101",
                        bound_tmdb_id=800101,
                        bound_imdb_id="tt8001010",
                        bound_title="Crash Grab",
                        bound_year=2025,
                        bound_release_name="Crash.Grab.2025.2160p.REMUX-GRP",
                        bound_existing_movie_id=None,
                        bound_existing_file_id=None,
                        bound_existing_file_quality=None,
                        bound_rejection_class=None,
                        expires_at=expires_future,
                    ),
                    NewActionRecord(
                        action_id=act_watched_id,
                        token_sha256=hash_token(tok_watched),
                        release_id="rel-crash-1",
                        card_revision=1,
                        source_occurrence_id="occ-1",
                        action_type="watched_good",
                        completion_group="watched:rel-crash-1:800101",
                        bound_tmdb_id=800101,
                        bound_imdb_id="tt8001010",
                        bound_title="Crash Grab",
                        bound_year=2025,
                        bound_release_name="Crash.Grab.2025.2160p.REMUX-GRP",
                        bound_existing_movie_id=None,
                        bound_existing_file_id=None,
                        bound_existing_file_quality=None,
                        bound_rejection_class=None,
                        expires_at=expires_future,
                    ),
                ],
            )
            db.finish_card_delivery_success(
                "rel-crash-1",
                card_revision=1,
                delivery_kind="actionable_grab",
                ack_chat_id="-100",
                ack_message_id=9001,
                ack_thread_id=20,
                used_poster=False,
                is_edit=False,
            )
            # Commit attempting rows for dead_proc (simulating crash right before Radarr response persisted!)
            ok_p, _, _ = db.begin_write_attempt(
                action_id=act_push_id,
                completion_group="grab:rel-crash-1:800101",
                phase="radarr_release_push",
                process_instance_id=dead_proc,
                lease_expires_at=future_lease,
                request_fingerprint='{"tmdb_id":800101}',
                allowed_action_statuses=frozenset({"active"}),
                radarr_movie_id=501,
            )
            self.assertTrue(ok_p)
            ok_w, _, _ = db.begin_write_attempt(
                action_id=act_watched_id,
                completion_group="watched:rel-crash-1:800101",
                phase="radarr_watched",
                process_instance_id=dead_proc,
                lease_expires_at=future_lease,
                request_fingerprint='{"tmdb_id":800101}',
                allowed_action_statuses=frozenset({"active"}),
                radarr_movie_id=501,
            )
            self.assertTrue(ok_w)

            # Seed orphaned delivery still in status='sending' from dead_proc
            db.begin_card_delivery(
                release_id="rel-crash-sending",
                card_revision=1,
                delivery_kind="evidence_review",
                target_chat_id="-100",
                target_thread_id=10,
                process_instance_id=dead_proc,
                lease_expires_at=future_lease,
                new_actions=[],
            )

            # Seed orphaned evaluator_feedback attempt after completed_radarr
            db.begin_card_delivery(
                release_id="rel-crash-fb",
                card_revision=1,
                delivery_kind="actionable_grab",
                target_chat_id="-100",
                target_thread_id=20,
                process_instance_id=dead_proc,
                lease_expires_at=future_lease,
                new_actions=[
                    NewActionRecord(
                        action_id=act_fb_id,
                        token_sha256=hash_token(tok_fb),
                        release_id="rel-crash-fb",
                        card_revision=1,
                        source_occurrence_id="occ-fb",
                        action_type="watched_bad",
                        completion_group="watched:rel-crash-fb:800202",
                        bound_tmdb_id=800202,
                        bound_imdb_id="tt8002020",
                        bound_title="Crash Feedback",
                        bound_year=2025,
                        bound_release_name="Crash.Feedback.2025.REMUX",
                        bound_existing_movie_id=None,
                        bound_existing_file_id=None,
                        bound_existing_file_quality=None,
                        bound_rejection_class=None,
                        expires_at=expires_future,
                    )
                ],
            )
            with sqlite3.connect(str(cfg.actions_db_path)) as conn:
                conn.execute("UPDATE actions SET status = 'completed_radarr', feedback_status = 'pending' WHERE action_id = ?", (act_fb_id,))
            ok_fb, _, _ = db.begin_write_attempt(
                action_id=act_fb_id,
                completion_group="watched:rel-crash-fb:800202",
                phase="evaluator_feedback",
                process_instance_id=dead_proc,
                lease_expires_at=future_lease,
                request_fingerprint='{"decision":"skip"}',
                allowed_action_statuses=frozenset({"completed_radarr"}),
                forbid_group_completed_radarr=False,
            )
            self.assertTrue(ok_fb)

            # Live Radarr already applied BOTH the grab (in queue) AND watched+unmonitored!
            radarr = ControllableRadarrStub()
            radarr.movies[501] = {
                "id": 501,
                "tmdbId": 800101,
                "imdbId": "tt8001010",
                "monitored": False,
                "hasFile": False,
                "tags": [7],
            }
            radarr.queue.append(
                {
                    "movieId": 501,
                    "title": "Crash.Grab.2025.2160p.REMUX-GRP",
                    "added": utc_now_iso(),
                }
            )
            fb = ControllableFeedbackStub()

            # Start FRESH movie-actions-api process with new process_instance_id!
            new_app = create_actions_app(
                cfg,
                process_instance_id="proc-fresh-after-restart",
                radarr_client=radarr,
                telegram_client=ControllableTelegramStub(),
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=fb,
            )
            async with new_app.router.lifespan_context(new_app):
                # Startup recovery promoted ALL orphaned rows BEFORE serving any request!
                self.assertEqual(
                    db.get_action_group_readonly(hash_token(tok_push)).action["status"],
                    "ambiguous_radarr",
                )
                self.assertEqual(
                    db.get_action_group_readonly(hash_token(tok_watched)).action["status"],
                    "ambiguous_radarr",
                )
                self.assertEqual(
                    db.get_delivery("rel-crash-sending")["status"],
                    "ambiguous_delivery",
                )
                fb_act_after_boot = db.get_action_group_readonly(hash_token(tok_fb)).action
                self.assertEqual(fb_act_after_boot["status"], "completed_radarr")
                self.assertEqual(fb_act_after_boot["feedback_status"], "failed")

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=new_app), base_url="http://test"
                ) as client:
                    # (i) Reconcile orphaned Grab -> 200 (NOT 409!), 0 duplicate pushes, /feedback posted once!
                    r_p = await client.post(f"/action/{tok_push}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_p.status_code, 200)
                    self.assertEqual(len(radarr.push_calls), 0)
                    self.assertEqual(len(radarr.add_calls), 0)
                    self.assertEqual(
                        db.get_action_group_readonly(hash_token(tok_push)).action["status"],
                        "completed_all",
                    )

                    # (ii) Reconcile orphaned Watched -> 200 (NOT 409!), 0 duplicate Radarr writes, /feedback posted once!
                    r_w = await client.post(f"/action/{tok_watched}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_w.status_code, 200)
                    self.assertEqual(len(radarr.update_calls), 0)
                    self.assertEqual(len(radarr.add_calls), 0)
                    self.assertEqual(
                        db.get_action_group_readonly(hash_token(tok_watched)).action["status"],
                        "completed_all",
                    )

                    # (iv) Isolated feedback retry on orphaned feedback -> 0 Radarr writes, saves SKIP!
                    r_f = await client.post(
                        f"/action/{tok_fb}", data={"confirm": "1", "retry_feedback_only": "1"}
                    )
                    self.assertEqual(r_f.status_code, 200)
                    self.assertEqual(
                        db.get_action_group_readonly(hash_token(tok_fb)).action["status"],
                        "completed_all",
                    )
                    self.assertEqual(len(fb.calls), 3)

    async def test_end_to_end_real_intake_actions_triage_wiring(self) -> None:
        """Full 3-service integration: real movie-intake-api <-> real movie-actions-api <-> real movie-triage-api /feedback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            internal_secret = "shared-loopback-secret-3-services"
            webhook_secret = "autobrr-webhook-secret-xyz"
            passkey_url = "https://tracker.invalid/download/999001/SUPER_SECRET_PASSKEY_ABC123/test.torrent"

            # 1. Configure isolated real movie-triage-api SQLite DB + internal token
            orig_triage_db = triage_main.db
            orig_triage_secret = triage_main.INTERNAL_API_SECRET
            triage_db_path = base / "triage" / "triage.db"
            triage_main.db = triage_db_mod.TriageDB(triage_db_path)
            triage_main.INTERNAL_API_SECRET = internal_secret
            triage_main.db.init_db()

            try:
                # 2. Stub external boundaries (Radarr, Telegram, TMDB, TL, Gemini /evaluate)
                radarr_stub = ControllableRadarrStub()
                tg_stub = ControllableTelegramStub()

                class _FakeTmdb(TmdbIndependentClient):
                    async def get_movie_by_tmdb_id(self, tmdb_id: int) -> Optional[TmdbMovieRecord]:
                        if tmdb_id == 900901:
                            return TmdbMovieRecord(
                                tmdb_id=900901,
                                imdb_id="tt9009010",
                                title="Integration Cinema",
                                original_title="Integration Cinema",
                                year=2025,
                                runtime_minutes=118,
                                alternative_titles=(),
                                poster_path="/integration_poster.jpg",
                            )
                        return None

                    async def find_movies_by_imdb_id(self, imdb_id: str) -> Optional[list[int]]:
                        return [900901] if imdb_id.lower() == "tt9009010" else []

                class _FakeTl(TorrentLeechScraper):
                    async def fetch_listing(
                        self, *, external_id: str, exact_release_name: str, **kwargs: Any
                    ) -> TorrentLeechResult:
                        return TorrentLeechResult(
                            outcome="listing_id_found",
                            imdb_id="tt9009010",
                            file_runtime_minutes=118,
                            audio_languages=("English",),
                            freeleech_badge=True,
                        )

                class _FakeEvalClient(TriageApiClient):
                    async def evaluate(self, request_body: dict[str, Any]) -> TriageEvalResult:
                        return TriageEvalResult(
                            ok=True,
                            payload={
                                "identification": {
                                    "canonical_title": "Integration Cinema",
                                    "year": 2025,
                                    "director": "Test Director",
                                    "imdb_id": "tt9009010",
                                    "tmdb_id": 900901,
                                    "match_confidence": "high",
                                },
                                "ratings": {"rotten_tomatoes_critics": 92, "imdb": 8.1},
                                "recognition": {"awards_highlights": ["TIFF"], "critic_consensus": "Strong craft."},
                                "decision_aid": {
                                    "one_line_pitch": "A razor-sharp thriller.",
                                    "verdict": "WATCH",
                                    "verdict_reason": "Fits profile.",
                                    "similar_to": [],
                                    "red_flags": [],
                                },
                                "cached": False,
                            },
                        )

                intake_cfg = IntakeConfig(
                    host="127.0.0.1",
                    port=40720,
                    webhook_secret=webhook_secret,
                    internal_api_secret=internal_secret,
                    tl_metadata_enabled=True,
                    tl_cookie_file=base / "cookies.json",
                    tmdb_api_token="unused",
                    tmdb_cache_path=base / "intake" / "intake-tmdb-cache.json",
                    webhook_archive_dir=base / "intake" / "webhook-archive",
                    intake_db_path=base / "intake" / "intake.db",
                    triage_api_url="http://triage.local",
                    actions_api_url="http://actions.local",
                    shadow_replay_enabled=False,
                )
                actions_cfg = self._make_config(tmpdir, internal_secret=internal_secret)
                actions_cfg = ActionsConfig(
                    **{
                        **actions_cfg.__dict__,
                        "intake_api_url": "http://intake.local",
                        "triage_api_url": "http://triage.local",
                    }
                )

                # Build circular ASGI clients so real intake <-> real actions <-> real triage talk HTTP!
                triage_http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=triage_main.app), base_url="http://triage.local"
                )
                # Placeholder apps created in two steps with real ASGI transports
                actions_app_holder: dict[str, Any] = {}
                intake_app_holder: dict[str, Any] = {}

                async def _intake_transport_handler(request: httpx.Request) -> httpx.Response:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=intake_app_holder["app"]),
                        base_url="http://intake.local",
                    ) as c:
                        return await c.request(
                            request.method,
                            str(request.url),
                            headers=request.headers,
                            content=request.content,
                        )

                async def _actions_transport_handler(request: httpx.Request) -> httpx.Response:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=actions_app_holder["app"]),
                        base_url="http://actions.local",
                    ) as c:
                        return await c.request(
                            request.method,
                            str(request.url),
                            headers=request.headers,
                            content=request.content,
                        )

                intake_to_actions_http = httpx.AsyncClient(
                    transport=httpx.MockTransport(_actions_transport_handler), base_url="http://actions.local"
                )
                actions_to_intake_http = httpx.AsyncClient(
                    transport=httpx.MockTransport(_intake_transport_handler), base_url="http://intake.local"
                )

                actions_app = create_actions_app(
                    actions_cfg,
                    radarr_client=radarr_stub,
                    telegram_client=tg_stub,
                    intake_push_client=IntakePushClient(
                        base_url="http://intake.local",
                        internal_token=internal_secret,
                        http_client=actions_to_intake_http,
                    ),
                    triage_feedback_client=TriageFeedbackClient(
                        base_url="http://triage.local",
                        internal_token=internal_secret,
                        http_client=triage_http,
                    ),
                )
                actions_app_holder["app"] = actions_app

                intake_app = create_intake_app(
                    intake_cfg,
                    tl_scraper=_FakeTl(
                        db=None,  # type: ignore[arg-type]
                        cookie_file=intake_cfg.tl_cookie_file,
                        enabled=True,
                    ),
                    tmdb_client=_FakeTmdb(api_token="unused", cache_path=intake_cfg.tmdb_cache_path),
                    triage_client=_FakeEvalClient(base_url="http://triage.local", internal_token=internal_secret),
                    actions_client=ActionsApiClient(
                        base_url="http://actions.local",
                        internal_token=internal_secret,
                        http_client=intake_to_actions_http,
                    ),
                    start_worker=False,
                )
                intake_app_holder["app"] = intake_app

                async with actions_app.router.lifespan_context(actions_app):
                    async with intake_app.router.lifespan_context(intake_app):
                        async with httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=intake_app), base_url="http://intake.local"
                        ) as intake_c, httpx.AsyncClient(
                            transport=httpx.ASGITransport(app=actions_app), base_url="http://actions.local"
                        ) as actions_c:
                            webhook_payload = {
                                "event": "action.rejected",
                                "data": {
                                    "indexer": "TorrentLeech",
                                    "action": {"type": "RADARR"},
                                    "filter": {"id": 7, "name": "Movies Remux"},
                                    "result": {"status": "PUSH_REJECTED", "rejections": ["Unknown Movie"]},
                                    "release": {
                                        "external_id": "999001",
                                        "name": "Integration.Cinema.2025.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                                        "title": "Integration Cinema",
                                        "year": 2025,
                                        "type": "movie",
                                        "category": "Movies :: 4K",
                                        "resolution": "2160p",
                                        "source": "UHD BluRay",
                                        "media_processing": "REMUX",
                                        "meta_imdb": "tt9009010",
                                        "meta_tmdb": 900901,
                                        "download_url": passkey_url,
                                        "size": 60_000_000_000,
                                    },
                                },
                            }
                            r_wh = await intake_c.post(f"/webhook/{webhook_secret}", json=webhook_payload)
                            self.assertEqual(r_wh.status_code, 200)
                            release_id = r_wh.json()["release_id"]
                            self.assertIsNotNone(release_id)

                            # Run one worker pass -> verifies TMDB, checks real Actions Radarr status,
                            # delivers Telegram card + bound evaluation reply!
                            processed = await intake_app.state.worker.process_next_queued()
                            self.assertTrue(processed)
                            self.assertEqual(len(tg_stub.sent_photos), 1)
                            self.assertEqual(len(tg_stub.sent_messages), 1)
                            self.assertEqual(
                                tg_stub.sent_messages[0]["reply_to_message_id"],
                                tg_stub.sent_photos[0]["message_id"],
                            )

                            # User clicks Add & Grab from real Telegram button!
                            tokens = _extract_tokens_from_markup(tg_stub.sent_photos[0]["reply_markup"])
                            r_get = await actions_c.get(f"/action/{tokens['add_grab']}")
                            self.assertEqual(r_get.status_code, 200)
                            self.assertIn("/media/movies", r_get.text)

                            # User submits POST -> fetches ephemeral passkey URL from real intake archive,
                            # adds movie + pushes exact URL to Radarr, and saves feedback to real triage.db!
                            r_post = await actions_c.post(
                                f"/action/{tokens['add_grab']}",
                                data={
                                    "confirm": "1",
                                    "root_folder_path": "/media/movies",
                                    "quality_profile_id": "4",
                                },
                            )
                            self.assertEqual(r_post.status_code, 200)
                            self.assertIn("evaluator rating saved (WATCH)", r_post.text)
                            self.assertEqual(len(radarr_stub.add_calls), 1)
                            self.assertEqual(len(radarr_stub.push_calls), 1)
                            self.assertEqual(radarr_stub.push_calls[0]["downloadUrl"], passkey_url)

                            # Verify real movie-triage-api persisted feedback in triage.db
                            fb_items = triage_main.db.get_recent_feedback()
                            self.assertEqual(len(fb_items), 1)
                            self.assertEqual(fb_items[0]["title"], "Integration Cinema")
                            self.assertEqual(fb_items[0]["decision"], "WATCH")
                            self.assertEqual(fb_items[0]["note"], "Grabbed via Telegram")

                            # Verify ZERO passkeys leaked into intake.db, actions.db, or triage.db!
                            for db_file in (
                                intake_cfg.intake_db_path,
                                actions_cfg.actions_db_path,
                                triage_db_path,
                            ):
                                raw_bytes = db_file.read_bytes()
                                self.assertNotIn(b"SUPER_SECRET_PASSKEY_ABC123", raw_bytes)
                                self.assertNotIn(passkey_url.encode("utf-8"), raw_bytes)

                await triage_http.aclose()
                await intake_to_actions_http.aclose()
                await actions_to_intake_http.aclose()
            finally:
                triage_main.db = orig_triage_db
                triage_main.INTERNAL_API_SECRET = orig_triage_secret

    async def test_concurrent_phase1_reconciliation_never_issues_second_push_after_ambiguous_push(self) -> None:
        """Codex Round 1 P1 #1: Concurrent Phase 1 reconcilers cannot issue a 2nd push once 1st push is ambiguous."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()

            # Controllable intake push client synchronizing two concurrent Phase 1 reconcilers
            gate_active = False
            p1_first_done = asyncio.Event()
            both_at_fetch = asyncio.Event()
            entered_count = 0

            class _GatedIntakePush(ShadowStubIntakePushClient):
                async def fetch_push_payload(
                    self, *, release_id: str, source_occurrence_id: str
                ) -> IntakePushPayloadResult:
                    nonlocal entered_count
                    if gate_active:
                        entered_count += 1
                        idx = entered_count
                        if entered_count == 2:
                            both_at_fetch.set()
                        else:
                            await asyncio.wait_for(both_at_fetch.wait(), timeout=5.0)
                        if idx == 2:
                            # Hold caller #2 until caller #1's push has finished with ambiguous_radarr!
                            await asyncio.wait_for(p1_first_done.wait(), timeout=5.0)
                    return await super().fetch_push_payload(
                        release_id=release_id, source_occurrence_id=source_occurrence_id
                    )

            intake_push = _GatedIntakePush()
            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=tg,
                intake_push_client=intake_push,
                triage_feedback_client=ControllableFeedbackStub(),
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # Deliver card and trigger Phase 1 (radarr_add_movie) timeout -> ambiguous_radarr
                    r_card = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-race-p1", tmdb_id=610101, title="Race Film"),
                        headers=headers,
                    )
                    self.assertEqual(r_card.status_code, 200)
                    add_tok = _extract_tokens_from_markup(tg.sent_photos[-1]["reply_markup"])["add_grab"]

                    radarr.add_outcome_queue.append(
                        RadarrWriteResult(outcome="ambiguous", error_redacted="radarr_add_ambiguous:ReadTimeout")
                    )
                    r_init = await client.post(
                        f"/action/{add_tok}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_init.status_code, 503)
                    self.assertEqual(len(radarr.push_calls), 0)

                    # Movie actually DID land in Radarr during the timed-out Phase 1 add
                    radarr.movies[610] = {
                        "id": 610,
                        "tmdbId": 610101,
                        "imdbId": "tt5501010",
                        "monitored": True,
                        "hasFile": False,
                        "tags": [],
                    }
                    # First Phase 2 push will time out (ambiguous!)
                    radarr.push_outcome_queue.append(
                        RadarrWriteResult(outcome="ambiguous", error_redacted="radarr_push_ambiguous:ReadTimeout")
                    )
                    gate_active = True

                    async def _run_reconciler() -> httpx.Response:
                        try:
                            return await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                        finally:
                            p1_first_done.set()

                    r1, r2 = await asyncio.gather(_run_reconciler(), _run_reconciler())
                    # Caller #1 issued the single push (timed out -> 503); caller #2 MUST be refused (409) with ZERO 2nd push!
                    self.assertEqual({r1.status_code, r2.status_code}, {503, 409})
                    self.assertEqual(len(radarr.push_calls), 1)
                    snap = app.state.db.get_action_group_readonly(hash_token(add_tok))
                    self.assertEqual(snap.action["status"], "ambiguous_radarr")

    async def test_in_flight_rev1_eval_reply_cannot_clobber_rev2_pending_eval_state(self) -> None:
        """Codex Round 1 P1 #2: Rev 1 reply finishing after Rev 2 card edit must not mark Rev 2 evaluation sent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            rev1_entered_send = asyncio.Event()
            allow_rev1_send_finish = asyncio.Event()
            orig_send_message = tg.send_message

            async def _gated_send_message(**kwargs: Any) -> TelegramSendResult:
                # Gate ONLY the rev 1 evaluation reply (has reply_to_message_id and "Rev1 Reason")
                if kwargs.get("reply_to_message_id") and "Rev1 Reason" in str(kwargs.get("text_html") or ""):
                    rev1_entered_send.set()
                    await allow_rev1_send_finish.wait()
                return await orig_send_message(**kwargs)

            tg.send_message = _gated_send_message  # type: ignore[method-assign]

            app = create_actions_app(
                cfg,
                radarr_client=ControllableRadarrStub(),
                telegram_client=tg,
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=ControllableFeedbackStub(),
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # 1. Deliver rev 1 text card
                    r_card1 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-eval-race", card_revision=1, poster_path=None),
                        headers=headers,
                    )
                    self.assertEqual(r_card1.status_code, 200)
                    ack1 = r_card1.json()

                    # 2. Start rev 1 evaluation reply (pauses inside Telegram sendMessage)
                    rev1_eval_task = asyncio.create_task(
                        client.post(
                            "/internal/deliver/evaluation",
                            json={
                                "release_id": "rel-eval-race",
                                "card_revision": 1,
                                "ack_chat_id": ack1["ack_chat_id"],
                                "ack_message_id": ack1["ack_message_id"],
                                "evaluation": {
                                    "identification": {"canonical_title": "Film", "year": 2025, "director": "D", "match_confidence": "high"},
                                    "ratings": {},
                                    "recognition": {"awards_highlights": [], "critic_consensus": "OK"},
                                    "decision_aid": {"one_line_pitch": "P", "verdict": "MAYBE", "verdict_reason": "Rev1 Reason", "similar_to": [], "red_flags": []},
                                },
                            },
                            headers=headers,
                        )
                    )
                    await rev1_entered_send.wait()

                    # 3. While rev 1 reply is in flight, rev 2 card edit succeeds (resets eval reply to rev 2 pending)
                    r_card2 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-eval-race",
                            card_revision=2,
                            delivery_kind="evidence_review",
                            extra={"conflict_reason": "corrected_conflict"},
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_card2.status_code, 200)

                    # 4. Now let rev 1 reply finish Telegram sendMessage & call finish_eval_reply
                    allow_rev1_send_finish.set()
                    r_eval1 = await rev1_eval_task
                    self.assertEqual(r_eval1.status_code, 200)
                    self.assertEqual(r_eval1.json().get("detail"), "superseded_during_send")

                    # Verify rev 2 eval_reply_status is STILL 'pending' (NOT clobbered to 'sent'!)
                    del_row = app.state.db.get_delivery("rel-eval-race")
                    self.assertEqual(del_row["eval_reply_card_revision"], 2)
                    self.assertEqual(del_row["eval_reply_status"], "pending")
                    self.assertIsNone(del_row["eval_reply_message_id"])

                    # 5. Rev 2 evaluation reply MUST ACTUALLY SEND (NOT return already_sent!)
                    sent_before_rev2 = len(tg.sent_messages)
                    r_eval2 = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-eval-race",
                            "card_revision": 2,
                            "ack_chat_id": ack1["ack_chat_id"],
                            "ack_message_id": ack1["ack_message_id"],
                            "evaluation": {
                                "identification": {"canonical_title": "Corrected Film", "year": 2025, "director": "D", "match_confidence": "high"},
                                "ratings": {},
                                "recognition": {"awards_highlights": [], "critic_consensus": "Great"},
                                "decision_aid": {"one_line_pitch": "P2", "verdict": "WATCH", "verdict_reason": "Rev2 Corrected Reason", "similar_to": [], "red_flags": []},
                            },
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_eval2.status_code, 200)
                    self.assertEqual(r_eval2.json()["status"], "sent")
                    self.assertNotEqual(r_eval2.json().get("detail"), "already_sent")
                    self.assertEqual(len(tg.sent_messages), sent_before_rev2 + 1)
                    self.assertIn("Rev2 Corrected Reason", tg.sent_messages[-1]["text_html"])
                    del_final = app.state.db.get_delivery("rel-eval-race")
                    self.assertEqual(del_final["eval_reply_status"], "sent")
                    self.assertEqual(del_final["eval_reply_message_id"], tg.sent_messages[-1]["message_id"])

    async def test_stale_reconciliation_preserves_completed_all_and_never_reopens_feedback(self) -> None:
        """Codex Round 2 P1: Stale empty or positive reconciliation finishing after completed_all must never downgrade or re-post feedback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            fb = ControllableFeedbackStub()

            rel_name = "Completed.Race.2025.2160p.REMUX-CiNEPHiLES"
            empty_entered = asyncio.Event()
            release_empty = asyncio.Event()
            orig_get_history = radarr.get_movie_history
            history_call = 0

            async def _interleaved_history(movie_id: int) -> RadarrReadResult:
                nonlocal history_call
                history_call += 1
                if history_call == 1:
                    # Caller #1 observes an EMPTY history snapshot, then pauses until Caller #2 completes!
                    empty_entered.set()
                    await asyncio.wait_for(release_empty.wait(), timeout=5.0)
                    return RadarrReadResult(ok=True, data=[], status_code=200)
                return await orig_get_history(movie_id)

            radarr.get_movie_history = _interleaved_history  # type: ignore[method-assign]

            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=tg,
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=fb,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # 1. Deliver card & make push go ambiguous
                    r_card = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-r2-stale", tmdb_id=620101, title="Completed Race", release_name=rel_name
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_card.status_code, 200)
                    add_tok = _extract_tokens_from_markup(tg.sent_photos[-1]["reply_markup"])["add_grab"]

                    radarr.push_outcome_queue.append(
                        RadarrWriteResult(outcome="ambiguous", error_redacted="radarr_push_ambiguous:ReadTimeout")
                    )
                    r_init = await client.post(
                        f"/action/{add_tok}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_init.status_code, 503)
                    movie_id = next(iter(radarr.movies.keys()))

                    # 2. Caller #1 starts reconciliation (sees empty history and pauses)
                    stale_task = asyncio.create_task(
                        client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    )
                    await asyncio.wait_for(empty_entered.wait(), timeout=5.0)

                    # 3. Delayed push lands in Radarr history; Caller #2 reconciles -> completed_all + feedback saved!
                    radarr.history.append(
                        {
                            "movieId": movie_id,
                            "eventType": "grabbed",
                            "sourceTitle": rel_name,
                            "date": utc_now_iso(),
                        }
                    )
                    r_winner = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_winner.status_code, 200)
                    self.assertIn("evaluator rating saved (WATCH)", r_winner.text)
                    self.assertEqual(len(fb.calls), 1)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(add_tok)).action["status"],
                        "completed_all",
                    )

                    # 4. Now let stale Caller #1 finish its empty-snapshot reconciliation
                    release_empty.set()
                    r_stale = await stale_task
                    self.assertEqual(r_stale.status_code, 200)
                    # Persisted status MUST remain completed_all (NOT downgraded to ambiguous_radarr!)
                    snap_after_stale = app.state.db.get_action_group_readonly(hash_token(add_tok))
                    self.assertEqual(snap_after_stale.action["status"], "completed_all")
                    self.assertIn("evaluator rating saved (WATCH)", r_stale.text)

                    # 5. Direct seal_reconciled_attempt call on completed_all MUST return False (never reopens feedback!)
                    reopened = app.state.db.seal_reconciled_attempt(
                        attempt_id=None,
                        action_id=str(snap_after_stale.action["action_id"]),
                        radarr_movie_id=movie_id,
                        outcome_summary="duplicate_seal_attempt",
                        needs_feedback=True,
                    )
                    self.assertFalse(reopened)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(add_tok)).action["status"],
                        "completed_all",
                    )
                    self.assertEqual(len(fb.calls), 1)

    async def test_push_reconciliation_requires_exact_release_identity_rejecting_repack_substring(self) -> None:
        """Codex Round 3 P1: Containing release like -REPACK must NOT resolve an ambiguous push; exact component or GUID does."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            fb = ControllableFeedbackStub()

            announced = "Film.2025.1080p.BluRay.x264-GROUP"
            repack = "Film.2025.1080p.BluRay.x264-GROUP-REPACK"

            class _GuidIntakePush(ShadowStubIntakePushClient):
                async def fetch_push_payload(
                    self, *, release_id: str, source_occurrence_id: str
                ) -> IntakePushPayloadResult:
                    return IntakePushPayloadResult(
                        ok=True,
                        push_payload={
                            "title": announced,
                            "downloadUrl": "https://shadow-tracker.invalid/dl/630101",
                            "protocol": "torrent",
                            "publishDate": utc_now_iso(),
                            "indexer": "torrentleech",
                            "guid": "torrentleech:630101",
                        },
                        status_code=200,
                    )

            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=tg,
                intake_push_client=_GuidIntakePush(),
                triage_feedback_client=fb,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # 1. Trigger ambiguous push for Film.2025.1080p.BluRay.x264-GROUP
                    r_card = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-r3-exact", tmdb_id=630101, title="Film", release_name=announced
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_card.status_code, 200)
                    add_tok = _extract_tokens_from_markup(tg.sent_photos[-1]["reply_markup"])["add_grab"]

                    radarr.push_outcome_queue.append(
                        RadarrWriteResult(outcome="ambiguous", error_redacted="radarr_push_ambiguous:ReadTimeout")
                    )
                    r_init = await client.post(
                        f"/action/{add_tok}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_init.status_code, 503)
                    movie_id = next(iter(radarr.movies.keys()))

                    # 2. Another release containing the announced name (-REPACK) appears in queue, history, and imported path
                    radarr.queue.append({"movieId": movie_id, "title": repack, "added": utc_now_iso()})
                    radarr.history.append(
                        {
                            "movieId": movie_id,
                            "eventType": "grabbed",
                            "sourceTitle": repack,
                            "date": utc_now_iso(),
                            "data": {
                                "guid": "torrentleech:999999",
                                "droppedPath": f"/downloads/{repack}/{repack}.mkv",
                            },
                        }
                    )
                    radarr.movies[movie_id]["movieFile"] = {
                        "id": 910,
                        "sceneName": repack,
                        "relativePath": f"{repack}/{repack}.mkv",
                        "dateAdded": utc_now_iso(),
                    }

                    # Reconcile -> MUST NOT match -REPACK! Must stay ambiguous_radarr with 0 feedback calls!
                    r_rec_repack = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_repack.status_code, 200)
                    self.assertIn("NO second release push", r_rec_repack.text)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(add_tok)).action["status"],
                        "ambiguous_radarr",
                    )
                    self.assertEqual(len(fb.calls), 0)
                    self.assertEqual(len(radarr.push_calls), 1)

                    # 3. Exact release path wrapper (or exact GUID) DOES resolve cleanly!
                    radarr.history.append(
                        {
                            "movieId": movie_id,
                            "eventType": "downloadFolderImported",
                            "sourceTitle": "unrelated-display-label",
                            "date": utc_now_iso(),
                            "data": {
                                "importedPath": f"/downloads/{announced}/{announced}.mkv",
                            },
                        }
                    )
                    r_rec_exact = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_exact.status_code, 200)
                    self.assertIn("evaluator rating saved (WATCH)", r_rec_exact.text)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(add_tok)).action["status"],
                        "completed_all",
                    )
                    self.assertEqual(len(fb.calls), 1)
                    self.assertEqual(len(radarr.push_calls), 1)

    async def test_radarr_client_filters_imdb_locally_against_populated_unrelated_library(self) -> None:
        """Codex Round 4 P1: Radarr /api/v3/movie ignores imdbId and returns the full library; filter locally."""
        populated_library = [
            {"id": 10, "tmdbId": 11111, "imdbId": "tt0011111", "monitored": True, "hasFile": True, "tags": []},
            {"id": 11, "tmdbId": 22222, "imdbId": "tt0022222", "monitored": False, "hasFile": False, "tags": [9]},
            {"id": 12, "tmdbId": 33333, "imdbId": "tt0033333", "monitored": True, "hasFile": False, "tags": []},
        ]

        def _radarr_v3_controller(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/v3/tag"):
                return httpx.Response(200, json=[{"id": 9, "label": "watched"}])
            if request.url.path.endswith("/api/v3/movie"):
                # Real Radarr MovieController ONLY filters on tmdbId; otherwise returns ALL movies!
                tmdb_param = request.url.params.get("tmdbId")
                if tmdb_param is not None:
                    tid = int(tmdb_param)
                    return httpx.Response(200, json=[m for m in populated_library if m["tmdbId"] == tid])
                return httpx.Response(200, json=list(populated_library))
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            async with httpx.AsyncClient(transport=httpx.MockTransport(_radarr_v3_controller)) as radarr_http:
                real_radarr_client = RadarrClient(
                    base_url="https://seedbox.invalid/user/radarr",
                    api_key="test-key",
                    http_client=radarr_http,
                )
                app = create_actions_app(
                    cfg,
                    radarr_client=real_radarr_client,
                    telegram_client=ControllableTelegramStub(),
                    intake_push_client=ShadowStubIntakePushClient(),
                    triage_feedback_client=ControllableFeedbackStub(),
                )
                headers = {"X-Internal-Token": cfg.internal_api_secret}
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test"
                    ) as client:
                        # Brand new movie with BOTH tmdb_id and imdb_id MUST report 'absent' (NOT ambiguous!)
                        r_absent = await client.get(
                            "/internal/radarr/status",
                            params={"tmdb_id": 999999, "imdb_id": "tt9999999"},
                            headers=headers,
                        )
                        self.assertEqual(r_absent.status_code, 200)
                        self.assertEqual(r_absent.json()["status"], "absent")

                        # IMDb-only lookup against populated library matches ONLY exact movie (watched_unmonitored)
                        r_imdb = await client.get(
                            "/internal/radarr/status",
                            params={"imdb_id": "TT0022222"},
                            headers=headers,
                        )
                        self.assertEqual(r_imdb.status_code, 200)
                        self.assertEqual(r_imdb.json()["status"], "watched_unmonitored")
                        self.assertEqual(r_imdb.json()["movie_id"], 11)

                        # Cross-check conflict: absent tmdb_id=888888, but imdb_id=tt0011111 belongs to tmdbId=11111 -> ambiguous!
                        r_conflict = await client.get(
                            "/internal/radarr/status",
                            params={"tmdb_id": 888888, "imdb_id": "tt0011111"},
                            headers=headers,
                        )
                        self.assertEqual(r_conflict.status_code, 200)
                        self.assertEqual(r_conflict.json()["status"], "ambiguous")

    async def test_qbit_alternative_grab_full_lifecycle_invariants_and_reconciliation(self) -> None:
        """
        Comprehensive verification of legacy qBittorrent Alternative Grab ("Grab Anyway"):
          - Canonical TL HTTPS download URL binding, v1 bencode SHA-1, 4MB cap, no-redirect fetch
          - Real QBittorrentClient multipart bytes-only POST /api/v2/torrents/add (no urls, no pause/start override)
          - Radarr single-enabled HTTPS qBittorrent route validation
          - Pre-fetch AND post-fetch 9-field snapshot drift refusal
          - already_present (leaves category untouched), missing category, autorun-enabled refusal
          - Happy path: queued, 0600 receipt, 0 Radarr writes, 0 /feedback writes, zero credential leaks
          - Lost ack -> uncertain -> read-only reconciliation requiring exact category (0 duplicate adds)
          - Prior durable receipt prevents duplicate add across fresh tokens
          - Rejected evidence_review with preferred_topic_kind routes to Grab Anyway topic with 0 buttons
        """
        # 1) Canonical TL download URL validation
        self.assertTrue(
            is_canonical_tl_download_url(
                "https://www.torrentleech.org/rss/download/123456/SECRET_KEY/Movie.2024.torrent",
                "123456",
            )
        )
        self.assertTrue(
            is_canonical_tl_download_url(
                "https://torrentleech.org/download/123456/Movie.2024.torrent",
                "123456",
            )
        )
        for bad_url in (
            "http://www.torrentleech.org/rss/download/123456/KEY/Movie.torrent",
            "https://evil.example/rss/download/123456/KEY/Movie.torrent",
            "https://www.torrentleech.org/rss/download/999999/KEY/Movie.torrent",
            "https://www.torrentleech.org/rss/download/123456/KEY/Movie.torrent?foo=1",
            "https://www.torrentleech.org/rss/download/123456/../999/Movie.torrent",
        ):
            self.assertFalse(is_canonical_tl_download_url(bad_url, "123456"), msg=f"Should reject {bad_url}")

        # 2) Strict v1 bencode validation
        valid_torrent = _build_synthetic_v1_torrent(b"Festival.Gem.2025.2160p.REMUX.mkv")
        valid_hash = parse_v1_torrent_info_hash(valid_torrent)
        self.assertEqual(len(valid_hash), 40)
        with self.assertRaises(TorrentMetainfoError):
            parse_v1_torrent_info_hash(b"not-bencode")
        with self.assertRaises(TorrentMetainfoError):
            parse_v1_torrent_info_hash(valid_torrent + b"trailing")
        with self.assertRaises(TorrentMetainfoError):
            # v2-only info dict missing v1 `pieces`
            parse_v1_torrent_info_hash(b"d4:infod12:meta versioni2e4:name4:test12:piece lengthi16384eee")
        with self.assertRaises(TorrentMetainfoError):
            # Unsorted keys
            parse_v1_torrent_info_hash(b"d1:bi1e1:ai2ee")

        # 3) Real TorrentFetcher transport checks: redirect refused, >4MB refused, valid v1 accepted
        fetch_mode = {"mode": "redirect"}

        def _tl_dl_handler(request: httpx.Request) -> httpx.Response:
            if fetch_mode["mode"] == "redirect":
                return httpx.Response(302, headers={"Location": "https://other.invalid/file.torrent"})
            if fetch_mode["mode"] == "too_large":
                return httpx.Response(200, content=b"x" * 4_000_005)
            return httpx.Response(200, content=valid_torrent)

        tl_url = "https://www.torrentleech.org/rss/download/555111/PASSKEY_MUST_NOT_LEAK/Movie.torrent"
        async with httpx.AsyncClient(transport=httpx.MockTransport(_tl_dl_handler), follow_redirects=False) as tf_http:
            fetcher = TorrentFetcher(http_client=tf_http)
            r_redir = await fetcher.fetch_bound_torrent(download_url=tl_url, external_id="555111")
            self.assertFalse(r_redir.ok)
            self.assertIn("redirect_refused", r_redir.error_redacted or "")
            self.assertNotIn("PASSKEY_MUST_NOT_LEAK", r_redir.error_redacted or "")

            fetch_mode["mode"] = "too_large"
            r_big = await fetcher.fetch_bound_torrent(download_url=tl_url, external_id="555111")
            self.assertFalse(r_big.ok)
            self.assertIn("4mb", (r_big.error_redacted or "").lower())

            fetch_mode["mode"] = "ok"
            r_ok = await fetcher.fetch_bound_torrent(download_url=tl_url, external_id="555111")
            self.assertTrue(r_ok.ok)
            self.assertEqual(r_ok.info_hash, valid_hash)

        # 4) Real QBittorrentClient multipart bytes-only verification (NO urls, NO pause/start overrides!)
        captured_qbit_reqs: list[tuple[str, str, bytes]] = []

        def _qbit_transport(request: httpx.Request) -> httpx.Response:
            captured_qbit_reqs.append((request.method, str(request.url), request.content))
            if request.url.path.endswith("/api/v2/auth/login"):
                return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=clean_session"})
            if request.url.path.endswith("/api/v2/torrents/add"):
                return httpx.Response(200, text="Ok.")
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(_qbit_transport), follow_redirects=False) as qb_http:
            real_qb = QBittorrentClient(
                base_url="https://qbit.seedbox.invalid/qbittorrent",
                username="qbit_secret_user",
                password="qbit_secret_password_999",
                http_client=qb_http,
            )
            sess, _owns, login_r = await real_qb.open_authenticated_session()
            self.assertTrue(login_r.ok)
            add_r = await real_qb.add_torrent_bytes(
                sess,
                torrent_bytes=valid_torrent,
                category="movies",
                filename="555111.torrent",
            )
            self.assertEqual(add_r.outcome, "ok")
            add_body = captured_qbit_reqs[-1][2]
            self.assertIn(b'name="torrents"', add_body)
            self.assertIn(b'name="category"', add_body)
            self.assertIn(valid_torrent, add_body)
            self.assertNotIn(b'name="urls"', add_body)
            self.assertNotIn(b'name="paused"', add_body)
            self.assertNotIn(b'name="stopped"', add_body)
            self.assertNotIn(b'name="autoTMM"', add_body)

        # 5) Radarr single-enabled HTTPS qBittorrent route validation
        matching_client = {
            "enable": True,
            "implementation": "QBittorrent",
            "protocol": "torrent",
            "fields": [
                {"name": "host", "value": "qbit.seedbox.invalid"},
                {"name": "port", "value": 443},
                {"name": "useSsl", "value": True},
                {"name": "urlBase", "value": "/qbittorrent"},
                {"name": "movieCategory", "value": "movies"},
            ],
        }
        self.assertEqual(
            validate_radarr_qbit_route(
                [matching_client],
                configured_url="https://qbit.seedbox.invalid/qbittorrent",
                configured_category="movies",
            )[0],
            True,
        )
        # 0 enabled clients, 2 enabled clients, HTTP client, wrong category -> all refused!
        self.assertFalse(
            validate_radarr_qbit_route(
                [], configured_url="https://qbit.seedbox.invalid/qbittorrent", configured_category="movies"
            )[0]
        )
        self.assertFalse(
            validate_radarr_qbit_route(
                [matching_client, matching_client],
                configured_url="https://qbit.seedbox.invalid/qbittorrent",
                configured_category="movies",
            )[0]
        )
        http_client = dict(
            matching_client,
            fields=[
                {"name": "host", "value": "qbit.seedbox.invalid"},
                {"name": "port", "value": 443},
                {"name": "useSsl", "value": False},
                {"name": "urlBase", "value": "/qbittorrent"},
                {"name": "movieCategory", "value": "movies"},
            ],
        )
        self.assertFalse(
            validate_radarr_qbit_route(
                [http_client],
                configured_url="https://qbit.seedbox.invalid/qbittorrent",
                configured_category="movies",
            )[0]
        )
        self.assertFalse(
            validate_radarr_qbit_route(
                [matching_client],
                configured_url="https://qbit.seedbox.invalid/qbittorrent",
                configured_category="other-cat",
            )[0]
        )

        # 6) End-to-end movie-actions-api Alternative Grab lifecycle & edge cases
        with tempfile.TemporaryDirectory() as tmpdir:
            alt_cfg_path = self._write_alt_grab_config(tmpdir)
            cfg = self._make_config(tmpdir, alt_policy_path=alt_cfg_path)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            radarr.download_clients = [matching_client]
            radarr.movies[80] = {
                "id": 80,
                "tmdbId": 880080,
                "imdbId": "tt8800800",
                "title": "Heat",
                "monitored": True,
                "qualityProfileId": 3,
                "hasFile": True,
                "movieFile": {
                    "id": 801,
                    "relativePath": "Heat.1995.Remux-1080p.mkv",
                    "edition": "Director's Definitive Edition",
                    "releaseGroup": "FraMeSToR",
                    "size": 36_000_000_000,
                    "quality": {"quality": {"name": "Remux-1080p"}},
                },
                "tags": [],
            }
            fb = ControllableFeedbackStub()
            qbit = ShadowStubQbitClient(category="movies")

            # Custom push stub emitting canonical TL download URL with external_id matching release
            dl_map: dict[str, str] = {}

            class _MapPushStub(ShadowStubIntakePushClient):
                async def fetch_push_payload(
                    self, *, release_id: str, source_occurrence_id: str
                ) -> IntakePushPayloadResult:
                    return IntakePushPayloadResult(
                        ok=True,
                        push_payload={
                            "title": "Heat.1995.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                            "downloadUrl": dl_map[release_id],
                            "protocol": "torrent",
                            "publishDate": "2026-11-26T00:00:00Z",
                            "indexer": "TorrentLeech",
                        },
                    )

            post_fetch_hook: dict[str, Any] = {"fn": None}

            class _HookFetcher(ShadowStubTorrentFetcher):
                async def fetch_bound_torrent(self, *, download_url: str, external_id: str) -> Any:
                    res = await super().fetch_bound_torrent(download_url=download_url, external_id=external_id)
                    if post_fetch_hook["fn"] is not None:
                        fn = post_fetch_hook["fn"]
                        post_fetch_hook["fn"] = None
                        fn()
                    return res

            fetcher = _HookFetcher(torrent_bytes=valid_torrent)
            app = create_actions_app(
                cfg,
                radarr_client=radarr,
                telegram_client=tg,
                intake_push_client=_MapPushStub(),
                triage_feedback_client=fb,
                torrent_fetcher=fetcher,
                qbit_client=qbit,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            snap_80 = build_movie_file_snapshot(radarr.movies[80])
            assert snap_80 is not None

            async def _mint_alt(client: httpx.AsyncClient, rel_id: str, ext_id: str) -> str:
                dl_map[rel_id] = f"https://www.torrentleech.org/rss/download/{ext_id}/QBIT_PASSKEY_SECRET/Heat.torrent"
                r = await client.post(
                    "/internal/deliver/card",
                    json=_verified_card_payload(
                        release_id=rel_id,
                        tmdb_id=880080,
                        imdb_id="tt8800800",
                        title="Heat",
                        year=1995,
                        release_name="Heat.1995.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                        external_id=ext_id,
                        delivery_kind="alternative_grab",
                        poster_path=None,
                        extra={
                            "bound_existing_movie_id": 80,
                            "bound_existing_file_id": 801,
                            "bound_rejection_class": "cutoff_met",
                            "rejection_reasons": ["Existing file meets cutoff: Remux-1080p"],
                            "bound_movie_file_snapshot": dict(snap_80),
                        },
                    ),
                    headers=headers,
                )
                self.assertEqual(r.status_code, 200)
                return _extract_tokens_from_markup(tg.sent_messages[-1]["reply_markup"])["alternative_grab"]

            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # 6a: Rejected evidence_review with preferred_topic_kind routes to Grab Anyway topic (30) with 0 buttons
                    r_ev = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-alt-ev",
                            delivery_kind="evidence_review",
                            poster_path=None,
                            extra={
                                "conflict_reason": "tracker_imdb_mismatch",
                                "preferred_topic_kind": "alternative_grab",
                            },
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_ev.status_code, 200)
                    self.assertEqual(r_ev.json()["ack_thread_id"], 30)
                    self.assertEqual(tg.sent_messages[-1]["reply_markup"], {"inline_keyboard": []})

                    # 6b: Post-fetch 9-field snapshot drift -> 409 refused, 0 qBit adds!
                    tok_drift = await _mint_alt(client, "rel-alt-drift", "660001")
                    post_fetch_hook["fn"] = lambda: radarr.movies[80]["movieFile"].update({"size": 99_999_999})
                    r_drift = await client.post(f"/action/{tok_drift}", data={"confirm": "1"})
                    self.assertEqual(r_drift.status_code, 409)
                    self.assertIn("Post-fetch revalidation failed", r_drift.text)
                    self.assertEqual(len(qbit.add_calls), 0)
                    # Restore snapshot size
                    radarr.movies[80]["movieFile"]["size"] = 36_000_000_000

                    # 6c: Missing qBit category -> 409 refused, 0 qBit adds
                    tok_cat = await _mint_alt(client, "rel-alt-cat", "660002")
                    qbit.categories = {"other": {"name": "other"}}
                    r_cat = await client.post(f"/action/{tok_cat}", data={"confirm": "1"})
                    self.assertEqual(r_cat.status_code, 409)
                    self.assertIn("does not exist in qBittorrent", r_cat.text)
                    self.assertEqual(len(qbit.add_calls), 0)
                    qbit.categories = {"movies": {"name": "movies"}}

                    # 6d: qBit autorun hook enabled -> 409 refused, 0 qBit adds
                    tok_auto = await _mint_alt(client, "rel-alt-auto", "660003")
                    qbit.preferences["autorun_enabled"] = True
                    r_auto = await client.post(f"/action/{tok_auto}", data={"confirm": "1"})
                    self.assertEqual(r_auto.status_code, 409)
                    self.assertIn("autorun", r_auto.text.lower())
                    self.assertEqual(len(qbit.add_calls), 0)
                    qbit.preferences["autorun_enabled"] = False

                    # 6e: Already present in qBit (under a DIFFERENT category) -> 200 already_present, category untouched, 0 adds!
                    tok_present = await _mint_alt(client, "rel-alt-present", "660004")
                    qbit.torrents[valid_hash] = {"hash": valid_hash, "category": "manual-keep", "state": "stalledUP"}
                    r_present = await client.post(f"/action/{tok_present}", data={"confirm": "1"})
                    self.assertEqual(r_present.status_code, 200)
                    self.assertIn("already_present", r_present.text)
                    self.assertEqual(qbit.torrents[valid_hash]["category"], "manual-keep")
                    self.assertEqual(len(qbit.add_calls), 0)
                    qbit.torrents.clear()

                    # 6f: Happy path -> GET read-only -> POST queues in qBit, 0600 receipt, 0 Radarr writes, 0 /feedback!
                    tok_ok = await _mint_alt(client, "rel-alt-ok", "660005")
                    r_get = await client.get(f"/action/{tok_ok}")
                    self.assertEqual(r_get.status_code, 200)
                    self.assertIn("qBittorrent", r_get.text)
                    self.assertEqual(len(qbit.add_calls), 0)

                    r_ok = await client.post(f"/action/{tok_ok}", data={"confirm": "1"})
                    self.assertEqual(r_ok.status_code, 200)
                    self.assertIn("queued", r_ok.text.lower())
                    self.assertEqual(len(qbit.add_calls), 1)
                    self.assertEqual(qbit.add_calls[0]["category"], "movies")
                    self.assertEqual(len(radarr.add_calls), 0)
                    self.assertEqual(len(radarr.update_calls), 0)
                    self.assertEqual(len(radarr.push_calls), 0)
                    self.assertEqual(len(fb.calls), 0)

                    # Durable 0600 receipt written and zero credentials anywhere in DB or receipt
                    receipt_dir = Path(tmpdir) / "alt-grab-state"
                    receipt = read_alt_grab_receipt(receipt_dir, "660005")
                    assert receipt is not None
                    self.assertEqual(receipt["status"], "queued")
                    self.assertEqual(receipt["info_hash"], valid_hash)
                    receipt_file = receipt_dir / "alt-grab-tl-660005.json"
                    self.assertEqual(os.lstat(receipt_dir).st_mode & 0o777, 0o700)
                    self.assertEqual(os.lstat(receipt_file).st_mode & 0o777, 0o600)
                    for blob in (
                        cfg.actions_db_path.read_bytes(),
                        receipt_file.read_bytes(),
                        r_ok.text.encode("utf-8"),
                    ):
                        self.assertNotIn(b"qbit_secret_password_999", blob)
                        self.assertNotIn(b"qbit_secret_user", blob)
                        self.assertNotIn(b"QBIT_PASSKEY_SECRET", blob)

                    # Prior durable receipt on disk prevents duplicate add even for a brand new release/token with same external_id!
                    tok_dup_ext = await _mint_alt(client, "rel-alt-dup-ext", "660005")
                    r_dup = await client.post(f"/action/{tok_dup_ext}", data={"confirm": "1"})
                    self.assertEqual(r_dup.status_code, 200)
                    self.assertEqual(len(qbit.add_calls), 1)  # Still 1 total!

                    # 6g: Lost HTTP ack -> uncertain (ambiguous_radarr) -> wrong category stays uncertain -> expected category seals queued (0 duplicate adds!)
                    qbit.torrents.clear()
                    tok_amb = await _mint_alt(client, "rel-alt-amb", "660006")
                    qbit.add_queue.append(QbitAddResult(outcome="ambiguous", error_redacted="qbit_add_ambiguous:ReadTimeout"))
                    r_amb = await client.post(f"/action/{tok_amb}", data={"confirm": "1"})
                    self.assertEqual(r_amb.status_code, 503)
                    self.assertIn("uncertain", r_amb.text.lower())
                    self.assertEqual(len(qbit.add_calls), 2)
                    rec_amb = read_alt_grab_receipt(receipt_dir, "660006")
                    assert rec_amb is not None
                    self.assertEqual(rec_amb["status"], "uncertain")

                    # Reconcile when torrent appears in WRONG category -> MUST stay uncertain (never seal queued!)
                    qbit.torrents[valid_hash] = {"hash": valid_hash, "category": "wrong-cat", "state": "downloading"}
                    r_rec_wrong = await client.post(f"/action/{tok_amb}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_wrong.status_code, 200)
                    self.assertIn("Still Uncertain", r_rec_wrong.text)
                    self.assertEqual(len(qbit.add_calls), 2)

                    # Reconcile when torrent appears in EXACT expected category -> seals completed_all (queued), 0 extra adds, 0 feedback!
                    qbit.torrents[valid_hash] = {"hash": valid_hash, "category": "movies", "state": "downloading"}
                    r_rec_ok = await client.post(f"/action/{tok_amb}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_ok.status_code, 200)
                    self.assertIn("Confirmed in qBittorrent", r_rec_ok.text)
                    self.assertEqual(len(qbit.add_calls), 2)  # Zero duplicate adds!
                    self.assertEqual(len(fb.calls), 0)  # Zero evaluator feedback!
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(tok_amb)).action["status"],
                        "completed_all",
                    )

                    # 6h: Hard crash during read-only qbit_alt_preflight (before receipt/add) -> startup orphan recovery
                    # promotes attempt to ambiguous -> reconcile resets cleanly to active -> user re-confirms & queues!
                    qbit.torrents.clear()
                    tok_pf_crash = await _mint_alt(client, "rel-alt-pf-crash", "660007")
                    pf_act = app.state.db.get_action_group_readonly(hash_token(tok_pf_crash)).action
                    assert pf_act is not None
                    ok_pf, _att_pf, _ = app.state.db.begin_write_attempt(
                        action_id=str(pf_act["action_id"]),
                        completion_group=str(pf_act["completion_group"]),
                        phase="qbit_alt_preflight",
                        process_instance_id="dead-process-instance",
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=60)),
                        request_fingerprint='{"phase":"preflight"}',
                        allowed_action_statuses=frozenset({"active"}),
                    )
                    self.assertTrue(ok_pf)
                    recov = app.state.db.recover_orphaned_state(app.state.process_instance_id)
                    self.assertEqual(recov["promoted_attempts"], 1)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(tok_pf_crash)).action["status"],
                        "ambiguous_radarr",
                    )
                    r_reset = await client.post(f"/action/{tok_pf_crash}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_reset.status_code, 200)
                    self.assertIn("Preflight Reset", r_reset.text)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(tok_pf_crash)).action["status"],
                        "active",
                    )
                    r_pf_ok = await client.post(f"/action/{tok_pf_crash}", data={"confirm": "1"})
                    self.assertEqual(r_pf_ok.status_code, 200)
                    self.assertIn("queued", r_pf_ok.text.lower())
                    self.assertEqual(len(qbit.add_calls), 3)

                    # 6i (Codex Round 1 P2): failed_not_transmitted -> active retry -> crash in read-only preflight ->
                    # reconcile MUST ignore the prior failed_definitive add, reset ambiguous preflight to active, and succeed!
                    qbit.torrents.clear()
                    tok_retry_pf = await _mint_alt(client, "rel-alt-retry-pf", "660008")
                    qbit.add_queue.append(
                        QbitAddResult(outcome="failed_not_transmitted", error_redacted="qbit_connect_error")
                    )
                    r_notx = await client.post(f"/action/{tok_retry_pf}", data={"confirm": "1"})
                    self.assertEqual(r_notx.status_code, 502)
                    retry_act = app.state.db.get_action_group_readonly(hash_token(tok_retry_pf)).action
                    assert retry_act is not None
                    self.assertEqual(retry_act["status"], "active")
                    self.assertEqual(len(qbit.add_calls), 4)

                    # Simulate retry crashing during read-only preflight before receipt/add
                    ok_pf2, _att_pf2, _ = app.state.db.begin_write_attempt(
                        action_id=str(retry_act["action_id"]),
                        completion_group=str(retry_act["completion_group"]),
                        phase="qbit_alt_preflight",
                        process_instance_id="dead-process-instance-2",
                        lease_expires_at=utc_now_iso(utc_now() + timedelta(seconds=60)),
                        request_fingerprint='{"phase":"preflight-retry"}',
                        allowed_action_statuses=frozenset({"active"}),
                    )
                    self.assertTrue(ok_pf2)
                    recov2 = app.state.db.recover_orphaned_state(app.state.process_instance_id)
                    self.assertEqual(recov2["promoted_attempts"], 1)

                    r_reset2 = await client.post(f"/action/{tok_retry_pf}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_reset2.status_code, 200)
                    self.assertIn("Preflight Reset", r_reset2.text)
                    self.assertEqual(
                        app.state.db.get_action_group_readonly(hash_token(tok_retry_pf)).action["status"],
                        "active",
                    )

                    r_retry_ok = await client.post(f"/action/{tok_retry_pf}", data={"confirm": "1"})
                    self.assertEqual(r_retry_ok.status_code, 200)
                    self.assertIn("queued", r_retry_ok.text.lower())
                    self.assertEqual(len(qbit.add_calls), 5)

    async def test_card_edit_with_preserve_eval_reply_keeps_existing_reply_and_blocks_duplicate_send(self) -> None:
        """OPS-REPORT #6: Card-only edit with preserve_eval_reply=True keeps sent eval reply and blocks duplicate sends."""
        with tempfile.TemporaryDirectory() as tmpdir:
            alt_state = Path(tmpdir) / "alt-state"
            alt_state.mkdir(mode=0o700)
            alt_cfg_path = Path(tmpdir) / "alt-grab.json"
            alt_cfg_path.write_text(
                json.dumps(
                    {
                        "url": "https://qbit.invalid:8443",
                        "username": "qbit-user",
                        "password": "qbit-secret-password",
                        "category": "radarr-alt",
                        "state": str(alt_state),
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(alt_cfg_path, 0o600)

            cfg = self._make_config(tmpdir, alt_policy_path=alt_cfg_path)
            tg = ControllableTelegramStub()
            app = create_actions_app(
                cfg,
                radarr_client=ControllableRadarrStub(),
                telegram_client=tg,
                intake_push_client=ShadowStubIntakePushClient(),
                triage_feedback_client=ControllableFeedbackStub(),
                torrent_fetcher=ShadowStubTorrentFetcher(),
                qbit_client=ShadowStubQbitClient(category="radarr-alt"),
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}
            sample_eval = {
                "identification": {"canonical_title": "Elio", "year": 2025, "director": "D", "match_confidence": "high"},
                "ratings": {},
                "recognition": {"awards_highlights": [], "critic_consensus": "Strong"},
                "decision_aid": {"one_line_pitch": "P", "verdict": "WATCH", "verdict_reason": "Good", "similar_to": [], "red_flags": []},
            }
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # 1. Deliver rev 1 actionable_grab card + send rev 1 evaluation reply
                    r_c1 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-preserve-1", card_revision=1, poster_path=None),
                        headers=headers,
                    )
                    self.assertEqual(r_c1.status_code, 200)
                    ack1 = r_c1.json()
                    r_e1 = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-preserve-1",
                            "card_revision": 1,
                            "ack_chat_id": ack1["ack_chat_id"],
                            "ack_message_id": ack1["ack_message_id"],
                            "evaluation": sample_eval,
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_e1.status_code, 200)
                    orig_reply_msg_id = r_e1.json()["reply_message_id"]
                    sent_count_after_e1 = len(tg.sent_messages)

                    # 2. Edit card to rev 2 (alternative_grab) with preserve_eval_reply=True
                    r_c2 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-preserve-1",
                            card_revision=2,
                            delivery_kind="alternative_grab",
                            poster_path=None,
                            extra={
                                "external_id": "241889121",
                                "exact_release_name": "Elio 2025 2160p UHD BluRay REMUX DV HDR HEVC Atmos-TRiToN",
                                "verified_title": "Elio",
                                "verified_year": 2025,
                                "verified_imdb_id": "tt4919268",
                                "verified_tmdb_id": 1022789,
                                "bound_existing_movie_id": 501,
                                "bound_existing_file_id": 901,
                                "bound_rejection_class": "cutoff_met",
                                "rejection_reasons": ["Existing file meets cutoff: Remux-2160p"],
                                "bound_movie_file_snapshot": {
                                    "movie_id": 501,
                                    "file_id": 901,
                                    "monitored": True,
                                    "quality_profile_id": 4,
                                    "relative_path": "Elio.2025.2160p.REMUX-OLD.mkv",
                                    "edition": "",
                                    "release_group": "OLD",
                                    "size": 55_000_000_000,
                                    "quality": "Remux-2160p",
                                },
                                "preserve_eval_reply": True,
                            },
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_c2.status_code, 200)
                    self.assertTrue(r_c2.json().get("eval_reply_preserved"))
                    self.assertEqual(len(tg.edited_texts), 1)
                    row_r2 = app.state.db.get_delivery("rel-preserve-1")
                    assert row_r2 is not None
                    self.assertEqual(row_r2["card_revision"], 2)
                    self.assertEqual(row_r2["delivery_kind"], "alternative_grab")
                    self.assertEqual(row_r2["eval_reply_status"], "sent")
                    self.assertEqual(row_r2["eval_reply_card_revision"], 2)
                    self.assertEqual(row_r2["eval_reply_message_id"], orig_reply_msg_id)

                    # Duplicate rev 2 evaluation call MUST be blocked as already_sent (0 new Telegram messages)
                    r_e2_dup = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-preserve-1",
                            "card_revision": 2,
                            "ack_chat_id": ack1["ack_chat_id"],
                            "ack_message_id": ack1["ack_message_id"],
                            "evaluation": sample_eval,
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_e2_dup.status_code, 200)
                    self.assertEqual(r_e2_dup.json().get("detail"), "already_sent")
                    self.assertEqual(len(tg.sent_messages), sent_count_after_e1)

                    # 3. Subsequent rev 3 conflict edit WITHOUT preserve_eval_reply MUST reset eval state & allow rev 3 reply
                    r_c3 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-preserve-1",
                            card_revision=3,
                            delivery_kind="evidence_review",
                            poster_path=None,
                            extra={"conflict_reason": "real_conflict"},
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_c3.status_code, 200)
                    self.assertFalse(r_c3.json().get("eval_reply_preserved"))
                    row_r3 = app.state.db.get_delivery("rel-preserve-1")
                    assert row_r3 is not None
                    self.assertEqual(row_r3["eval_reply_status"], "pending")
                    self.assertIsNone(row_r3["eval_reply_message_id"])

                    r_e3 = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-preserve-1",
                            "card_revision": 3,
                            "ack_chat_id": ack1["ack_chat_id"],
                            "ack_message_id": ack1["ack_message_id"],
                            "evaluation": sample_eval,
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_e3.status_code, 200)
                    self.assertEqual(len(tg.sent_messages), sent_count_after_e1 + 1)

                    # 4. Safety fallback: preserve_eval_reply=True when rev 1 eval was NEVER sent returns False & keeps pending
                    r_u1 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(release_id="rel-preserve-unsent", card_revision=1, poster_path=None),
                        headers=headers,
                    )
                    self.assertEqual(r_u1.status_code, 200)
                    r_u2 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-preserve-unsent",
                            card_revision=2,
                            delivery_kind="evidence_review",
                            poster_path=None,
                            extra={"conflict_reason": "unsent_check", "preserve_eval_reply": True},
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_u2.status_code, 200)
                    self.assertFalse(r_u2.json().get("eval_reply_preserved"))
                    row_unsent = app.state.db.get_delivery("rel-preserve-unsent")
                    assert row_unsent is not None
                    self.assertEqual(row_unsent["eval_reply_status"], "pending")

    async def test_issue_13_2a_and_2b_retired_card_and_final_button_rechecks_after_get(self) -> None:
        """Issue #13 §2a & §2b: Card retirement, post-GET Radarr drift checks, and Watched-then-Grab block."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._make_config(tmpdir)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            fb = ControllableFeedbackStub()
            intake_push = ShadowStubIntakePushClient()
            app = create_actions_app(
                cfg,
                telegram_client=tg,
                radarr_client=radarr,
                triage_feedback_client=fb,
                intake_push_client=intake_push,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}

            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # 1. §2a: Deliver actionable_grab rev 1 + eval reply -> retire at rev 2 -> buttons removed & 410!
                    r_del1 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-retire-1",
                            card_revision=1,
                            tmdb_id=910001,
                            imdb_id="tt9100010",
                            title="Retire Film",
                            poster_path=None,
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_del1.status_code, 200)
                    ack_r1 = r_del1.json()
                    toks_r1 = _extract_tokens_from_markup(tg.sent_messages[-1]["reply_markup"])
                    self.assertIn("add_grab", toks_r1)
                    self.assertIn("watched_good", toks_r1)

                    r_eval1 = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-retire-1",
                            "card_revision": 1,
                            "ack_chat_id": ack_r1["ack_chat_id"],
                            "ack_message_id": ack_r1["ack_message_id"],
                            "evaluation": {
                                "identification": {"canonical_title": "Retire Film", "year": 2025},
                                "ratings": {},
                                "decision_aid": {"verdict": "MAYBE", "verdict_reason": "Initial eval"},
                            },
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_eval1.status_code, 200)

                    r_ret2 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-retire-1",
                            card_revision=2,
                            delivery_kind="retired",
                            tmdb_id=910001,
                            imdb_id="tt9100010",
                            title="Retire Film",
                            poster_path=None,
                            extra={
                                "radarr_note": "Disqualified by later Radarr rejection: Quality is not wanted in profile",
                                "conflict_reason": "Disqualified by later Radarr rejection: Quality is not wanted in profile",
                                "preserve_eval_reply": True,
                            },
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_ret2.status_code, 200)
                    self.assertTrue(r_ret2.json().get("eval_reply_preserved"))
                    self.assertEqual(tg.edited_texts[-1]["reply_markup"], {"inline_keyboard": []})
                    self.assertIn("Proposal Retired", tg.edited_texts[-1]["text_html"])

                    # Pressing retired buttons MUST return 410 with ZERO Radarr writes
                    r_post_ret = await client.post(
                        f"/action/{toks_r1['add_grab']}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_post_ret.status_code, 410)
                    self.assertEqual(len(radarr.add_calls), 0)
                    self.assertEqual(len(radarr.push_calls), 0)

                    # 2. §2b: State changes AFTER GET confirmation page opens!
                    r_del2 = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-drift-2",
                            card_revision=1,
                            tmdb_id=910002,
                            imdb_id="tt9100020",
                            title="Drift Film",
                            poster_path=None,
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_del2.status_code, 200)
                    toks_d2 = _extract_tokens_from_markup(tg.sent_messages[-1]["reply_markup"])

                    # Open GET while movie is ABSENT -> 200 confirmation page opens!
                    r_get_open = await client.get(f"/action/{toks_d2['add_grab']}")
                    self.assertEqual(r_get_open.status_code, 200)

                    # 2a) After GET opened, movie becomes watched + unmonitored in Radarr -> POST MUST refuse 409!
                    radarr.movies[92] = {
                        "id": 92,
                        "tmdbId": 910002,
                        "imdbId": "tt9100020",
                        "title": "Drift Film",
                        "monitored": False,
                        "hasFile": False,
                        "tags": [7],  # watched
                    }
                    r_post_watched = await client.post(
                        f"/action/{toks_d2['add_grab']}",
                        data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                    )
                    self.assertEqual(r_post_watched.status_code, 409)
                    self.assertIn("watched", r_post_watched.text.lower())
                    self.assertEqual(len(radarr.add_calls), 0)
                    self.assertEqual(len(radarr.push_calls), 0)

                    # 2b) Movie now owns a file (hasFile=True, monitored=True) -> POST MUST refuse 409!
                    radarr.movies[92].update({"monitored": True, "hasFile": True, "tags": []})
                    r_post_owned = await client.post(
                        f"/action/{toks_d2['add_grab']}",
                        data={"confirm": "1"},
                    )
                    self.assertEqual(r_post_owned.status_code, 409)
                    self.assertIn("file", r_post_owned.text.lower())
                    self.assertEqual(len(radarr.push_calls), 0)

                    # 2c) Movie's IMDb ID on its TMDb row drifted -> POST MUST refuse 409 (both Grab & Watched)!
                    radarr.movies[92].update({"monitored": True, "hasFile": False, "imdbId": "tt9999999", "tags": []})
                    r_post_imdb_drift = await client.post(
                        f"/action/{toks_d2['add_grab']}",
                        data={"confirm": "1"},
                    )
                    self.assertEqual(r_post_imdb_drift.status_code, 409)
                    self.assertIn("imdb", r_post_imdb_drift.text.lower())
                    r_post_watched_drift = await client.post(
                        f"/action/{toks_d2['watched_good']}",
                        data={"confirm": "1"},
                    )
                    self.assertEqual(r_post_watched_drift.status_code, 409)
                    self.assertEqual(len(radarr.push_calls), 0)
                    self.assertEqual(len(radarr.update_calls), 0)

                    # 2d) Control: monitored movie with NO file & matching IMDb remains eligible -> succeeds!
                    radarr.movies[92].update({"monitored": True, "hasFile": False, "imdbId": "tt9100020", "tags": []})
                    r_post_ok = await client.post(
                        f"/action/{toks_d2['add_grab']}",
                        data={"confirm": "1"},
                    )
                    self.assertEqual(r_post_ok.status_code, 200)
                    self.assertEqual(len(radarr.push_calls), 1)
                    self.assertEqual(len(fb.calls), 1)
                    self.assertEqual(fb.calls[-1]["note"], "Grabbed via Telegram")

                    # 3. §2b: Watched Good / Bad followed by pressing previously issued Grab link -> 409 blocked!
                    for suffix, watched_key, expected_dec, tid, iid in (
                        ("good", "watched_good", "watch", 910003, "tt9100030"),
                        ("bad", "watched_bad", "skip", 910004, "tt9100040"),
                    ):
                        r_del_w = await client.post(
                            "/internal/deliver/card",
                            json=_verified_card_payload(
                                release_id=f"rel-watched-then-grab-{suffix}",
                                card_revision=1,
                                tmdb_id=tid,
                                imdb_id=iid,
                                title=f"Watched Block {suffix}",
                                poster_path=None,
                            ),
                            headers=headers,
                        )
                        self.assertEqual(r_del_w.status_code, 200)
                        toks_w = _extract_tokens_from_markup(tg.sent_messages[-1]["reply_markup"])

                        # Open Grab confirmation page BEFORE clicking Watched
                        r_get_g = await client.get(f"/action/{toks_w['add_grab']}")
                        self.assertEqual(r_get_g.status_code, 200)

                        # User clicks Watched Good / Bad -> succeeds in Radarr + feedback
                        r_w_post = await client.post(
                            f"/action/{toks_w[watched_key]}",
                            data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                        )
                        self.assertEqual(r_w_post.status_code, 200)
                        self.assertEqual(fb.calls[-1]["decision"], expected_dec)
                        self.assertEqual(fb.calls[-1]["note"], "watched")

                        # Now user submits the previously opened Grab link -> MUST refuse 409, ZERO pushes!
                        pushes_before = len(radarr.push_calls)
                        r_g_after_w = await client.post(
                            f"/action/{toks_w['add_grab']}",
                            data={"confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4"},
                        )
                        self.assertEqual(r_g_after_w.status_code, 409)
                        self.assertIn("watched", r_g_after_w.text.lower())
                        self.assertEqual(len(radarr.push_calls), pushes_before)

    async def test_issue_13_4b_and_4c_alt_grab_strictness_and_missing_timestamp_reconciliation(self) -> None:
        """Issue #13 §4b & §4c: Raw rejection validation, positive qBit autorun=false, and strict reconcile timestamps."""
        with tempfile.TemporaryDirectory() as tmpdir:
            alt_cfg_path = self._write_alt_grab_config(tmpdir)
            cfg = self._make_config(tmpdir, alt_policy_path=alt_cfg_path)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            fb = ControllableFeedbackStub()
            qbit = ShadowStubQbitClient(category="movies")

            valid_torrent = _build_synthetic_v1_torrent(b"Strict.Film.2025.2160p.UHD.BluRay.REMUX-GRP.mkv")
            fetcher = ShadowStubTorrentFetcher(torrent_bytes=valid_torrent)

            class _MapPushStub(ShadowStubIntakePushClient):
                async def fetch_push_payload(
                    self, *, release_id: str, source_occurrence_id: str
                ) -> IntakePushPayloadResult:
                    return IntakePushPayloadResult(
                        ok=True,
                        push_payload={
                            "title": "Strict.Film.2025.2160p.UHD.BluRay.REMUX-GRP",
                            "downloadUrl": "https://www.torrentleech.org/rss/download/920001/QBIT_SECRET_PASS/Strict.torrent",
                            "protocol": "torrent",
                            "publishDate": "2026-11-26T00:00:00Z",
                            "indexer": "TorrentLeech",
                        },
                    )

            radarr.download_clients = [
                {
                    "id": 1,
                    "enable": True,
                    "implementation": "QBittorrent",
                    "fields": [
                        {"name": "host", "value": "qbit.seedbox.invalid"},
                        {"name": "port", "value": 443},
                        {"name": "useSsl", "value": True},
                        {"name": "urlBase", "value": "/qbittorrent"},
                        {"name": "movieCategory", "value": "movies"},
                    ],
                }
            ]
            radarr.movies[88] = {
                "id": 88,
                "tmdbId": 920001,
                "imdbId": "tt9200010",
                "title": "Strict Film",
                "year": 2025,
                "monitored": True,
                "qualityProfileId": 4,
                "hasFile": True,
                "tags": [],
                "movieFile": {
                    "id": 880,
                    "relativePath": "Strict.Film.2025.1080p.BluRay.REMUX.mkv",
                    "edition": "",
                    "releaseGroup": "OLDGRP",
                    "size": 30_000_000_000,
                    "quality": {"quality": {"name": "Remux-1080p"}},
                },
            }
            app = create_actions_app(
                cfg,
                telegram_client=tg,
                radarr_client=radarr,
                triage_feedback_client=fb,
                intake_push_client=_MapPushStub(),
                qbit_client=qbit,
                torrent_fetcher=fetcher,
            )
            headers = {"X-Internal-Token": cfg.internal_api_secret}

            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    # 1. §4b: Malformed non-string rejection entries MUST be refused at /internal/deliver/card (422)
                    bad_alt_card = _verified_card_payload(
                        release_id="rel-alt-bad-rej",
                        delivery_kind="alternative_grab",
                        tmdb_id=920001,
                        imdb_id="tt9200010",
                        title="Strict Film",
                        release_name="Strict.Film.2025.2160p.UHD.BluRay.REMUX-GRP",
                        poster_path=None,
                        extra={
                            "indexer": "TorrentLeech",
                            "external_id": "920001",
                            "bound_existing_movie_id": 88,
                            "bound_existing_file_id": 880,
                            "bound_rejection_class": "cutoff_met",
                            "rejection_reasons": ["Existing file meets cutoff: Remux-1080p", 999],
                            "bound_movie_file_snapshot": {
                                "movie_id": 88,
                                "file_id": 880,
                                "monitored": True,
                                "quality_profile_id": 4,
                                "relative_path": "Strict.Film.2025.1080p.BluRay.REMUX.mkv",
                                "edition": "",
                                "release_group": "OLDGRP",
                                "size": 30_000_000_000,
                                "quality": "Remux-1080p",
                            },
                        },
                    )
                    r_bad = await client.post("/internal/deliver/card", json=bad_alt_card, headers=headers)
                    self.assertEqual(r_bad.status_code, 400)

                    # 2. §4b: Missing qBittorrent autorun preference keys MUST refuse (409) with 0 adds!
                    good_alt_card = dict(bad_alt_card)
                    good_alt_card["release_id"] = "rel-alt-missing-prefs"
                    good_alt_card["rejection_reasons"] = ["Existing file meets cutoff: Remux-1080p"]
                    r_ok_card = await client.post("/internal/deliver/card", json=good_alt_card, headers=headers)
                    self.assertEqual(r_ok_card.status_code, 200)
                    alt_tok = _extract_tokens_from_markup(tg.sent_messages[-1]["reply_markup"])["alternative_grab"]

                    for bad_prefs in (
                        {},
                        {"autorun_enabled": False},  # missing autorun_on_torrent_added_enabled
                        {"autorun_on_torrent_added_enabled": False},  # missing autorun_enabled
                        {"autorun_enabled": None, "autorun_on_torrent_added_enabled": False},
                        {"autorun_enabled": False, "autorun_on_torrent_added_enabled": "false"},
                    ):
                        qbit.preferences = dict(bad_prefs)
                        r_pref = await client.post(f"/action/{alt_tok}", data={"confirm": "1"})
                        self.assertEqual(r_pref.status_code, 409, f"Expected 409 for prefs={bad_prefs!r}")
                        self.assertEqual(len(qbit.add_calls), 0)

                    # 3. §4c: Ambiguous Radarr push reconciliation MUST reject absent/unreadable/stale timestamps
                    # and NEVER emit 'Grabbed via Telegram' feedback when uncertain!
                    rel_name = "Timestamp.Strict.2025.2160p.UHD.BluRay.REMUX-GRP"
                    radarr.movies[99] = {
                        "id": 99,
                        "tmdbId": 920002,
                        "imdbId": "tt9200020",
                        "title": "Timestamp Strict",
                        "monitored": True,
                        "hasFile": False,
                        "tags": [],
                    }
                    r_ts_card = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-ts-strict",
                            tmdb_id=920002,
                            imdb_id="tt9200020",
                            title="Timestamp Strict",
                            release_name=rel_name,
                            poster_path=None,
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_ts_card.status_code, 200)
                    add_tok = _extract_tokens_from_markup(tg.sent_messages[-1]["reply_markup"])["add_grab"]

                    # Trigger ambiguous release push (lost ack)
                    radarr.push_outcome_queue.append(
                        RadarrWriteResult(outcome="ambiguous", error_redacted="radarr_push_ambiguous:ReadTimeout")
                    )
                    r_amb = await client.post(f"/action/{add_tok}", data={"confirm": "1"})
                    self.assertEqual(r_amb.status_code, 503)
                    self.assertEqual(len(radarr.push_calls), 1)
                    self.assertEqual(len(fb.calls), 0)

                    # 3a) Queue/history/file match by release name with MISSING timestamps -> stays ambiguous_radarr!
                    radarr.queue = [{"movieId": 99, "title": rel_name}]
                    radarr.history = [{"movieId": 99, "eventType": "grabbed", "sourceTitle": rel_name, "data": {}}]
                    radarr.movies[99]["movieFile"] = {"id": 990, "sceneName": rel_name}
                    r_rec_missing = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_missing.status_code, 200)
                    self.assertIn("ambiguous_radarr", r_rec_missing.text)
                    self.assertEqual(len(fb.calls), 0)
                    self.assertEqual(len(radarr.push_calls), 1)

                    # 3b) UNREADABLE timestamps -> stays ambiguous_radarr, 0 feedback!
                    radarr.queue = [{"movieId": 99, "title": rel_name, "added": "not-a-timestamp"}]
                    radarr.history = [
                        {"movieId": 99, "eventType": "grabbed", "sourceTitle": rel_name, "date": "bad-date", "data": {}}
                    ]
                    radarr.movies[99]["movieFile"] = {"id": 990, "sceneName": rel_name, "dateAdded": "???"}
                    r_rec_bad = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_bad.status_code, 200)
                    self.assertIn("ambiguous_radarr", r_rec_bad.text)
                    self.assertEqual(len(fb.calls), 0)

                    # 3c) Stale timestamp (from 2020) -> stays ambiguous_radarr, 0 feedback!
                    radarr.queue = [{"movieId": 99, "title": rel_name, "added": "2020-01-01T00:00:00Z"}]
                    radarr.history = []
                    radarr.movies[99].pop("movieFile", None)
                    r_rec_stale = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_stale.status_code, 200)
                    self.assertIn("ambiguous_radarr", r_rec_stale.text)
                    self.assertEqual(len(fb.calls), 0)

                    # 3d) Valid in-window timestamp + exact release -> reconciles cleanly & sends feedback ONCE!
                    radarr.queue = [{"movieId": 99, "title": rel_name, "added": utc_now_iso()}]
                    r_rec_ok = await client.post(f"/action/{add_tok}", data={"confirm": "1", "reconcile": "1"})
                    self.assertEqual(r_rec_ok.status_code, 200)
                    self.assertNotIn("ambiguous_radarr", r_rec_ok.text)
                    self.assertEqual(len(fb.calls), 1)
                    self.assertEqual(fb.calls[0]["note"], "Grabbed via Telegram")
                    self.assertEqual(len(radarr.push_calls), 1)

    async def test_issue_13_6a_6b_6c_listing_link_error_attachments_and_omitted_topic_handling(self) -> None:
        """Issue #13 §6a, §6b, §6c: Canonical TL listing links, bounded error .txt attachments, and omitted topic handling."""
        from services.movie_actions_api.telegram_format import (
            MAX_TELEGRAM_ERROR_REPORT_CHARS,
            TELEGRAM_TEXT_UTF16_LIMIT,
            build_card_html,
            format_evaluation_reply,
            utf16_len,
        )

        # 1. §6a: Canonical TorrentLeech listing link on BOTH evidence_review and verified cards; never download links!
        ev_card_html = build_card_html(
            {
                "delivery_kind": "evidence_review",
                "exact_release_name": "North.Crossing.2021.2160p.UHD.BluRay.REMUX-GRP",
                "indexer": "TorrentLeech",
                "external_id": "445566",
                "category": "Movies :: 4K",
                "conflict_reason": "Release title 'North Crossing' does not match TMDB 'North Crossing II'",
                "tl_outcome": "listing_no_imdb",
                "observed_meta_imdb": "tt1111111",
                "observed_tl_imdb": "tt2222222",
                "checks_passed": ["source_tl_integrity"],
                "checks_failed": ["release_title_mismatch"],
            },
            max_utf16_units=TELEGRAM_TEXT_UTF16_LIMIT,
        )
        self.assertIn('https://www.torrentleech.org/torrent/445566', ev_card_html)
        self.assertIn("TL Listing (445566)", ev_card_html)
        self.assertNotIn("/download/", ev_card_html)
        self.assertIn("Failed Checks:", ev_card_html)
        self.assertIn("Passed Checks:", ev_card_html)
        self.assertIn("meta_imdb=tt1111111", ev_card_html)
        self.assertIn("tl_imdb=tt2222222", ev_card_html)

        ver_card_html = build_card_html(
            {
                "delivery_kind": "actionable_grab",
                "exact_release_name": "North.Crossing.2021.2160p.UHD.BluRay.REMUX-GRP",
                "indexer": "TorrentLeech",
                "external_id": "445566",
                "category": "Movies :: 4K",
                "verified_title": "North Crossing",
                "verified_year": 2021,
                "verified_imdb_id": "tt1111111",
                "verified_tmdb_id": 654321,
                "accepted_evidence": "tt1111111 / TMDB 654321 via TL listing IMDb, TMDB bidirectional + title/year verified",
                "checks_passed": ["source_tl_integrity", "release_title_year_match", "imdb_tmdb_bidirectional_agreement"],
            },
            max_utf16_units=TELEGRAM_TEXT_UTF16_LIMIT,
        )
        self.assertIn('https://www.torrentleech.org/torrent/445566', ver_card_html)
        self.assertIn("TL Listing (445566)", ver_card_html)
        self.assertIn("IMDb (tt1111111)", ver_card_html)
        self.assertIn("TMDB (654321)", ver_card_html)
        self.assertIn("Accepted Evidence:", ver_card_html)
        self.assertNotIn("/download/", ver_card_html)

        # Malformed non-numeric external_id or ineligible indexer MUST NOT emit a TL link
        bad_id_html = build_card_html(
            {
                "delivery_kind": "evidence_review",
                "exact_release_name": "Example.2025.2160p.UHD.BluRay.REMUX-GRP",
                "indexer": "TorrentLeech",
                "external_id": "445566/../download/secret",
            },
            max_utf16_units=TELEGRAM_TEXT_UTF16_LIMIT,
        )
        self.assertNotIn("torrentleech.org/torrent/", bad_id_html)

        # 2. §6b: Evaluator error rendering: inline when it fits (> 600 chars preserved!), .txt when > 4096, explicit truncation notice!
        med_err = ("error detail segment " * 50) + "passkey=ERR_SECRET_111 DIAGNOSTIC_ENDING_999"
        fmt_med = format_evaluation_reply(release_id="rel-err-med", evaluation=None, error_redacted=med_err)
        self.assertFalse(fmt_med.attach_txt)
        self.assertIn("DIAGNOSTIC_ENDING_999", fmt_med.html_text)
        self.assertNotIn("ERR_SECRET_111", fmt_med.html_text)
        self.assertLessEqual(utf16_len(fmt_med.html_text), TELEGRAM_TEXT_UTF16_LIMIT)

        long_err = ("long diagnostic stack " * 250) + "token=LONG_SECRET_222 FINAL_TAIL_TOKEN_888"
        fmt_long = format_evaluation_reply(release_id="rel-err-long", evaluation=None, error_redacted=long_err)
        self.assertTrue(fmt_long.attach_txt)
        self.assertLessEqual(utf16_len(fmt_long.html_text), TELEGRAM_TEXT_UTF16_LIMIT)
        self.assertIsNotNone(fmt_long.txt_content)
        assert fmt_long.txt_content is not None
        self.assertIn("FINAL_TAIL_TOKEN_888", fmt_long.txt_content)
        self.assertIn("Safety-Bound Truncated: no", fmt_long.txt_content)
        self.assertNotIn("LONG_SECRET_222", fmt_long.txt_content)

        oversized_err = (
            ("upstream diagnostic trace line " * ((MAX_TELEGRAM_ERROR_REPORT_CHARS // 30) + 200))
            + " api_key=HUGE_SECRET_333"
        )
        fmt_huge = format_evaluation_reply(release_id="rel-err-huge", evaluation=None, error_redacted=oversized_err)
        self.assertTrue(fmt_huge.attach_txt)
        self.assertIn("truncated at safety bound", fmt_huge.html_text.lower())
        assert fmt_huge.txt_content is not None
        self.assertIn("Safety-Bound Truncated: yes", fmt_huge.txt_content)
        self.assertIn("[truncated:", fmt_huge.txt_content)
        self.assertNotIn("HUGE_SECRET_333", fmt_huge.txt_content)

        # 3. §6c: Omitted Telegram topic on topic chat vs known non-topic chat
        with tempfile.TemporaryDirectory() as tmpdir:
            # 3a) Topic chat configured (telegram_add_grab_topic_id=20), Telegram omits message_thread_id!
            cfg_topic = self._make_config(tmpdir)
            tg_topic = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            fb = ControllableFeedbackStub()
            app_topic = create_actions_app(
                cfg_topic,
                telegram_client=tg_topic,
                radarr_client=radarr,
                triage_feedback_client=fb,
                intake_push_client=ShadowStubIntakePushClient(),
            )
            headers = {"X-Internal-Token": cfg_topic.internal_api_secret}

            async with app_topic.router.lifespan_context(app_topic):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_topic), base_url="http://test"
                ) as client:
                    # Photo send omits ack_thread_id -> DB MUST store ack_thread_id=None (NOT 20!)
                    tg_topic.photo_outcome_queue.append(
                        TelegramSendResult(
                            outcome="ok",
                            ack_chat_id=cfg_topic.telegram_chat_id,
                            ack_message_id=701,
                            ack_thread_id=None,
                        )
                    )
                    r_p = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-omitted-photo-topic",
                            poster_path="/poster.jpg",
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_p.status_code, 200)
                    self.assertIsNone(r_p.json()["ack_thread_id"])
                    del_p = app_topic.state.db.get_delivery("rel-omitted-photo-topic")
                    assert del_p is not None
                    self.assertEqual(del_p["target_thread_id"], 20)
                    self.assertIsNone(del_p["ack_thread_id"])

                    # Dependent evaluation reply MUST refuse (409) because topic was omitted!
                    sent_before = len(tg_topic.sent_messages)
                    r_eval_p = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-omitted-photo-topic",
                            "card_revision": 1,
                            "ack_chat_id": cfg_topic.telegram_chat_id,
                            "ack_message_id": 701,
                            "evaluation": None,
                            "error_redacted": long_err,
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_eval_p.status_code, 409)
                    self.assertEqual(len(tg_topic.sent_messages), sent_before)

                    # Text send omits ack_thread_id -> DB MUST also store ack_thread_id=None (NOT 20!)
                    tg_topic.message_outcome_queue.append(
                        TelegramSendResult(
                            outcome="ok",
                            ack_chat_id=cfg_topic.telegram_chat_id,
                            ack_message_id=702,
                            ack_thread_id=None,
                        )
                    )
                    r_t = await client.post(
                        "/internal/deliver/card",
                        json=_verified_card_payload(
                            release_id="rel-omitted-text-topic",
                            poster_path=None,
                        ),
                        headers=headers,
                    )
                    self.assertEqual(r_t.status_code, 200)
                    self.assertIsNone(r_t.json()["ack_thread_id"])
                    del_t = app_topic.state.db.get_delivery("rel-omitted-text-topic")
                    assert del_t is not None
                    self.assertEqual(del_t["target_thread_id"], 20)
                    self.assertIsNone(del_t["ack_thread_id"])

                    r_eval_t = await client.post(
                        "/internal/deliver/evaluation",
                        json={
                            "release_id": "rel-omitted-text-topic",
                            "card_revision": 1,
                            "ack_chat_id": cfg_topic.telegram_chat_id,
                            "ack_message_id": 702,
                            "evaluation": None,
                            "error_redacted": "err",
                        },
                        headers=headers,
                    )
                    self.assertEqual(r_eval_t.status_code, 409)

            # 3b) Known non-topic chat (all topic IDs None) -> ack_thread_id=None is valid & eval reply + .txt succeed!
            with tempfile.TemporaryDirectory() as tmpdir_nt:
                base_nt = self._make_config(tmpdir_nt)
                cfg_nontopic = ActionsConfig(
                    host=base_nt.host,
                    port=base_nt.port,
                    public_base_url=base_nt.public_base_url,
                    internal_api_secret=base_nt.internal_api_secret,
                    radarr_url=base_nt.radarr_url,
                    radarr_api_key=base_nt.radarr_api_key,
                    telegram_bot_token=base_nt.telegram_bot_token,
                    telegram_chat_id=base_nt.telegram_chat_id,
                    telegram_topic_id=None,
                    telegram_add_grab_topic_id=None,
                    telegram_grab_anyway_topic_id=None,
                    action_ttl_seconds=base_nt.action_ttl_seconds,
                    standalone_skip_enabled=base_nt.standalone_skip_enabled,
                    alternative_grab_config_path=None,
                    actions_db_path=base_nt.actions_db_path,
                    intake_api_url=base_nt.intake_api_url,
                    triage_api_url=base_nt.triage_api_url,
                    shadow_replay_enabled=False,
                )
                tg_nt = ControllableTelegramStub()
                app_nt = create_actions_app(
                    cfg_nontopic,
                    telegram_client=tg_nt,
                    radarr_client=ControllableRadarrStub(),
                    triage_feedback_client=ControllableFeedbackStub(),
                    intake_push_client=ShadowStubIntakePushClient(),
                )
                async with app_nt.router.lifespan_context(app_nt):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app_nt), base_url="http://test"
                    ) as client_nt:
                        r_nt = await client_nt.post(
                            "/internal/deliver/card",
                            json=_verified_card_payload(
                                release_id="rel-nontopic-1",
                                poster_path=None,
                            ),
                            headers=headers,
                        )
                        self.assertEqual(r_nt.status_code, 200)
                        ack_nt = r_nt.json()
                        self.assertIsNone(ack_nt["ack_thread_id"])

                        # Long sanitized error reply succeeds in non-topic chat AND attaches .txt!
                        r_eval_nt = await client_nt.post(
                            "/internal/deliver/evaluation",
                            json={
                                "release_id": "rel-nontopic-1",
                                "card_revision": 1,
                                "ack_chat_id": ack_nt["ack_chat_id"],
                                "ack_message_id": ack_nt["ack_message_id"],
                                "evaluation": None,
                                "error_redacted": long_err,
                            },
                            headers=headers,
                        )
                        self.assertEqual(r_eval_nt.status_code, 200)
                        self.assertEqual(r_eval_nt.json()["status"], "sent")
                        self.assertEqual(len(tg_nt.sent_documents), 1)
                        self.assertIn(b"FINAL_TAIL_TOKEN_888", tg_nt.sent_documents[0]["content_bytes"])
                        self.assertNotIn(b"LONG_SECRET_222", tg_nt.sent_documents[0]["content_bytes"])
                        self.assertIsNone(tg_nt.sent_documents[0]["thread_id"])


if __name__ == "__main__":
    unittest.main()
