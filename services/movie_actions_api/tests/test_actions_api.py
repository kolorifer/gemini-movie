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
from services.movie_actions_api.config import ActionsConfig, AlternativeGrabPolicy
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

    def _write_policy_file(self, tmpdir: str, payload: dict[str, Any], *, mode: int = 0o600) -> Path:
        p = Path(tmpdir) / "alt-grab.json"
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
            # Alternative Grab rejects non-quality rejection classes
            bad_policy = self._write_policy_file(
                tmpdir, {"enabled": True, "allowed_rejection_classes": ["identity_conflict"]}
            )
            with self.assertRaises(ValueError):
                AlternativeGrabPolicy.load_from_file(bad_policy)
            # World-readable policy file rejected
            world_policy = self._write_policy_file(
                tmpdir, {"enabled": True, "allowed_rejection_classes": ["cutoff_met"]}, mode=0o644
            )
            with self.assertRaises(PrivateFileError):
                AlternativeGrabPolicy.load_from_file(world_policy)

            # Shadow mode refuses default production state directory
            prod_shadow_cfg = ActionsConfig.from_env({"SHADOW_REPLAY_ENABLED": "true"})
            with self.assertRaises(ValueError):
                create_actions_app(prod_shadow_cfg)

            # Shadow mode refuses non-stub clients
            iso_shadow_cfg = self._make_config(tmpdir, shadow_replay=True)
            with self.assertRaises(ValueError):
                create_actions_app(
                    iso_shadow_cfg,
                    radarr_client=RadarrClient(base_url="http://127.0.0.1:7878", api_key="k"),
                )

            # Shadow mode auto-wires zero-mutation stubs cleanly
            shadow_app = create_actions_app(iso_shadow_cfg)
            self.assertTrue(shadow_app.state.radarr_client.is_shadow_stub)
            self.assertTrue(shadow_app.state.telegram_client.is_shadow_stub)
            self.assertTrue(shadow_app.state.intake_push_client.is_shadow_stub)
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
            policy_path = self._write_policy_file(
                tmpdir, {"enabled": True, "allowed_rejection_classes": ["cutoff_met"]}
            )
            cfg = self._make_config(tmpdir, alt_policy_path=policy_path)
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
            policy_path = self._write_policy_file(
                tmpdir, {"enabled": True, "allowed_rejection_classes": ["cutoff_met"]}
            )
            cfg = self._make_config(tmpdir, alt_policy_path=policy_path)
            tg = ControllableTelegramStub()
            radarr = ControllableRadarrStub()
            # Existing movie with an existing tag (id=42) and file -> watched MUST preserve tag 42 and file!
            radarr.movies[50] = {
                "id": 50,
                "tmdbId": 700101,
                "imdbId": "tt7001010",
                "title": "Watched Film",
                "monitored": True,
                "hasFile": True,
                "movieFile": {"id": 888, "quality": {"quality": {"name": "Bluray-1080p"}}},
                "tags": [42],
            }
            fb = ControllableFeedbackStub()
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
                    tmdb_cache_path=base / "intake" / "tmdb-cache.json",
                    webhook_archive_dir=base / "intake" / "webhook-archive",
                    intake_db_path=base / "intake" / "intake.db",
                    alternative_grab_config_path=None,
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
                                "action": {"rejected": False},
                                "data": {
                                    "indexer": "TorrentLeech",
                                    "release": {
                                        "external_id": "999001",
                                        "name": "Integration.Cinema.2025.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                                        "title": "Integration Cinema",
                                        "year": 2025,
                                        "type": "movie",
                                        "category": "Movies :: 4K",
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


if __name__ == "__main__":
    unittest.main()
