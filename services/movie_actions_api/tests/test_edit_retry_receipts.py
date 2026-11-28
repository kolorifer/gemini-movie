from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from services.movie_actions_api.app import create_app
from services.movie_actions_api.clients import TelegramSendResult
from services.movie_actions_api.tests import test_actions_api as fixtures


class EditRetryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = fixtures.TestMovieActionsApi()._make_config(tmp.name)
        self.telegram = fixtures.ControllableTelegramStub()
        self.client = await self.start_service()
        self.original = fixtures._verified_card_payload(poster_path=None)
        first = await self.client.post("/internal/deliver/card", json=self.original)
        first.raise_for_status()
        ack = first.json()
        self.assertEqual(ack["status"], "delivered")
        self.receipt = {
            "chat_id": ack["ack_chat_id"], "message_id": ack["ack_message_id"],
            "thread_id": ack["ack_thread_id"], "revision": 1,
        }
        self.edit = {**self.original, "card_revision": 2, "expected_ack": self.receipt}

    async def start_service(self):
        app = create_app(
            self.cfg, telegram_client=self.telegram, radarr_client=fixtures.ControllableRadarrStub(),
            intake_push_client=fixtures.ShadowStubIntakePushClient(),
            triage_feedback_client=fixtures.ControllableFeedbackStub(),
        )
        self.db = app.state.db
        await self.enterAsyncContext(app.router.lifespan_context(app))
        return await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"X-Internal-Token": self.cfg.internal_api_secret},
        ))

    async def put(self, payload=None):
        response = await self.client.put("/internal/deliver/card", json=self.edit if payload is None else payload)
        response.raise_for_status()
        return response.json()

    async def assert_receipt_required(self, payload=None):
        payload = self.edit if payload is None else payload
        for change in ({"chat_id": "other"}, {"thread_id": -1}, {"message_id": 99999}):
            wrong = {**payload, "expected_ack": {**self.receipt, **change}}
            self.assertEqual((await self.put(wrong))["detail"], "original_card_edit_refused")
        missing = {k: v for k, v in payload.items() if k != "expected_ack"}
        response = await self.client.put("/internal/deliver/card", json=missing)
        self.assertEqual(response.status_code, 400)

    async def exercise_definite_failure(self, restore, correction=None):
        failure = TelegramSendResult(outcome="failed_definitive", error_redacted="fixture_rejected")
        finish = self.db.finish_card_delivery_failed
        def finalize(release_id, **kwargs):
            if restore:
                kwargs["keep_prior_superseded"] = False
            return finish(release_id, **kwargs)

        with patch.object(self.db, "finish_card_delivery_failed", side_effect=finalize), patch.object(
            self.telegram, "edit_message_text", return_value=failure
        ) as first_edit:
            self.assertEqual((await self.put())["detail"], "fixture_rejected")
            first_edit.assert_awaited_once()
        self.client = await self.start_service()
        accepted = self.edit if correction is None else correction
        await self.assert_receipt_required(accepted)
        wrong_revision = {**accepted, "card_revision": 3, "expected_ack": {**self.receipt, "revision": 2}}
        self.assertEqual((await self.put(wrong_revision))["detail"], "original_card_edit_refused")
        unbound = {k: v for k, v in accepted.items() if k != "expected_ack"}
        for revision in (2, 3):
            post = await self.client.post("/internal/deliver/card", json={**unbound, "card_revision": revision})
            self.assertEqual(post.json()["status"], "failed")
        actions = self.db.list_actions_for_release(self.original["release_id"])
        self.assertTrue(actions)
        self.assertEqual({a["status"] for a in actions}, {"superseded"})
        self.assertEqual((await self.put(accepted))["status"], "delivered")
        self.assertEqual((await self.put(accepted))["status"], "delivered")
        if correction is not None:
            self.assertEqual((await self.put(self.edit))["detail"], "original_card_edit_refused")
        self.assertEqual(len(self.telegram.sent_messages), 1)
        self.assertEqual(len(self.telegram.edited_texts), 1)
        rendered = self.telegram.edited_texts[0]
        self.assertEqual((rendered["chat_id"], rendered["message_id"]),
                         (self.receipt["chat_id"], self.receipt["message_id"]))
        old_actions = {a["action_id"] for a in actions}
        actions = self.db.list_actions_for_release(self.original["release_id"])
        self.assertTrue(all(a["status"] == "superseded" for a in actions if a["action_id"] in old_actions))
        if accepted["delivery_kind"] == "actionable_grab":
            active = [a for a in actions if a["status"] == "active"]
            self.assertTrue(active)
            for a in active:
                self.assertNotIn(a["action_id"], old_actions)
                self.assertEqual(a["source_occurrence_id"], accepted["source_occurrence_id"])
                self.assertEqual(a["bound_tmdb_id"], accepted["verified_tmdb_id"])
                self.assertEqual(a["bound_imdb_id"], accepted["verified_imdb_id"])
                self.assertEqual(a["bound_release_name"], accepted["exact_release_name"])
        else:
            self.assertEqual({a["action_id"] for a in actions}, old_actions)
            self.assertTrue(all(a["status"] == "superseded" for a in actions))
            self.assertFalse((rendered["reply_markup"] or {}).get("inline_keyboard"))

        if self.edit["delivery_kind"] == "actionable_grab":
            self.assertEqual({a["card_revision"] for a in actions if a["action_id"] in old_actions}, {1, 2})

    async def test_definite_edit_replays_exact_receipt_and_content(self):
        await self.exercise_definite_failure(restore=False)

    async def test_new_occurrence_corrects_failed_proposal_on_original_card(self):
        correction = fixtures._verified_card_payload(
            card_revision=2, source_occurrence_id="occ-new", release_name="New.Identity.2024.1080p.BluRay.REMUX",
            title="New Identity", tmdb_id=30999, imdb_id="tt0339999", year=2024, poster_path=None,
            extra={"expected_ack": self.receipt},
        )
        await self.exercise_definite_failure(restore=False, correction=correction)

    async def test_same_occurrence_can_retire_failed_proposal_when_fresh_status_is_watched(self):
        retirement = {**self.edit, "delivery_kind": "retired", "radarr_status": "watched_unmonitored"}
        await self.exercise_definite_failure(restore=False, correction=retirement)

    async def test_failure_restoration_cannot_revive_old_tokens_or_drop_intent(self):
        await self.exercise_definite_failure(restore=True)

    async def test_retired_edit_retries_without_minting_buttons(self):
        self.edit["delivery_kind"] = "retired"
        await self.exercise_definite_failure(restore=False)

    async def test_evidence_review_edit_retries_without_minting_buttons(self):
        self.edit["delivery_kind"] = "evidence_review"
        self.edit["identity_gate_status"] = "evidence_review"
        await self.exercise_definite_failure(restore=False)

    async def test_active_edit_and_unknown_telegram_outcome_remain_ambiguous_on_replay(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed_edit(**kwargs):
            entered.set()
            await finish.wait()
            return TelegramSendResult(outcome="ambiguous", error_redacted="fixture_lost_ack")
        with patch.object(self.telegram, "edit_message_text", side_effect=delayed_edit) as telegram_call:
            first = asyncio.create_task(self.put())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.assert_receipt_required()
                replay = await self.put()
                self.assertEqual((replay["status"], replay["detail"]),
                                 ("ambiguous_delivery", "delivery_already_in_flight"))
            finally:
                finish.set()
                self.assertEqual((await first)["status"], "ambiguous_delivery")
            self.assertEqual((await self.put())["detail"], "existing_ambiguous_delivery")
            telegram_call.assert_awaited_once()
            self.assertEqual(len(self.telegram.sent_messages), 1)

    async def exercise_crash(self, restart):
        with patch.object(self.telegram, "edit_message_text", side_effect=RuntimeError("simulated crash")) as edit:
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                await self.put()
            if restart:
                self.client = await self.start_service()
            else:
                with sqlite3.connect(self.db.db_path) as conn:
                    conn.execute("UPDATE deliveries SET lease_expires_at='2000-01-01' WHERE status='sending'")
            await self.assert_receipt_required()
            self.assertEqual((await self.put())["status"], "ambiguous_delivery")
            self.assertEqual((await self.put())["detail"], "existing_ambiguous_delivery")
            edit.assert_awaited_once()
            self.assertEqual(len(self.telegram.sent_messages), 1)

    async def test_expired_edit_lease_is_promoted(self):
        await self.exercise_crash(restart=False)

    async def test_dead_edit_process_is_promoted(self):
        await self.exercise_crash(restart=True)
