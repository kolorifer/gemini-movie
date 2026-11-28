"""Crash recovery across separate intake and Actions journals."""
import asyncio
import json
import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from services.movie_actions_api.app import create_app as create_actions_app
from services.movie_actions_api.config import ActionsConfig
from services.movie_actions_api.clients import TelegramSendResult
from services.movie_intake_api.clients import ActionsApiClient, RadarrStatusResponse
from services.movie_intake_api.tests.test_radarr_timeout_retry import ABSENT, TIMEOUT, RetryFixture


def historical_fixture(stack, new_responses):
    """Start both services with isolated state and reopen an acknowledged timeout card on upgrade."""
    initial = RadarrStatusResponse(status="lookup_failed", detail="radarr_http_401")
    f = RetryFixture(stack, listing_has_id=True, responses=[initial, *new_responses])
    config = ActionsConfig.from_env({
        "ACTIONS_DB_PATH": str(f.cfg.intake_db_path.parent / "actions.db"),
        "SHADOW_REPLAY_ENABLED": "true",
        "INTERNAL_API_SECRET": "invented-secret",
        "MOVIE_INTEL_PUBLIC_BASE_URL": "https://example.invalid/actions",
        "TELEGRAM_CHAT_ID": "-100123",
    })
    actions_app = create_actions_app(config)
    stack.enter_context(TestClient(actions_app))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=actions_app))
    stack.callback(lambda: asyncio.run(http.aclose()))
    client = ActionsApiClient(base_url="http://fixture.invalid", internal_token=config.internal_api_secret,
                              http_client=http)
    f.actions.deliver_card = AsyncMock(wraps=client.deliver_card)
    f.actions.deliver_evaluation = AsyncMock(wraps=client.deliver_evaluation)
    telegram = actions_app.state.telegram_client
    assert f.process()
    with sqlite3.connect(f.cfg.intake_db_path) as conn:
        conn.execute("UPDATE releases SET radarr_status_snapshot=? WHERE release_id=?",
                     (json.dumps(TIMEOUT.to_dict()), f.release_id))
        conn.execute("ALTER TABLE releases DROP COLUMN pending_radarr_retry_json")
    f.reopen()
    return f, telegram, http


class TestLegacyRetryRecovery(unittest.TestCase):
    def test_last_lookup_succeeds_then_worker_crashes_after_acknowledged_edit(self):
        with ExitStack() as stack:
            f, telegram, _ = historical_fixture(stack, [TIMEOUT, ABSENT])
            original = f.row()
            self.assertEqual(original["eval_reply_status"], "sent")
            id_calls = f.identification_calls()
            original_sends = len(telegram.sent_messages) + len(telegram.sent_photos)
            self.assertTrue(f.process())  # second lookup times out
            f.make_retry_due()
            with patch.object(f.db, "finalize_release", side_effect=RuntimeError("simulated worker exit")):
                with self.assertRaisesRegex(RuntimeError, "simulated worker exit"):
                    f.process()  # third lookup succeeds, Telegram edit acknowledged
            self.assertEqual(len(telegram.edited_captions) + len(telegram.edited_texts), 1)
            checkpoint = json.loads(f.row()["pending_radarr_retry_json"])
            self.assertEqual(checkpoint["attempts"], 3)
            self.assertEqual(checkpoint["lookup_result"], ABSENT.to_dict())

            f.reopen()
            self.assertEqual(f.db.recover_orphaned_leases(active_worker_id=f.worker.worker_id,
                                                         recover_dead_owners=True), 1)
            self.assertTrue(f.process())
            self.assertFalse(f.process())
            self.assertEqual(f.row()["state"], "completed")
            self.assertEqual(f.row()["card_revision"], original["card_revision"] + 1)
            self.assertEqual(f.row()["ack_message_id"], original["ack_message_id"])
            self.assertEqual(len(f.actions.radarr_calls), 3)
            self.assertEqual(f.identification_calls(), id_calls)
            self.assertEqual(len(f.triage.calls), 1)
            f.actions.deliver_evaluation.assert_awaited_once()
            calls = f.actions.deliver_card.await_args_list
            self.assertEqual(len(calls), 3)  # initial send, edit, read back the confirmed edit receipt
            self.assertEqual(calls[1], calls[2])
            self.assertEqual(len(telegram.edited_captions) + len(telegram.edited_texts), 1)
            self.assertEqual(len(telegram.sent_messages) + len(telegram.sent_photos), original_sends)

    def test_pre_send_connection_failure_retries_only_the_same_card_edit(self):
        for failure in ("actions_connect_error", "telegram_connect_error"):
            with self.subTest(failure=failure), ExitStack() as stack:
                f, telegram, http = historical_fixture(stack, [ABSENT])
                original = f.row()
                id_calls = f.identification_calls()
                sends = len(telegram.sent_photos) + len(telegram.sent_messages)
                with ExitStack() as outage:
                    if failure == "actions_connect_error":
                        async def refuse_connection(request):
                            raise httpx.ConnectError("simulated pre-send connect failure", request=request)
                        outage.enter_context(patch.dict(http.event_hooks, request=[refuse_connection]))
                    else:
                        method = "edit_message_caption" if telegram.sent_photos else "edit_message_text"
                        outage.enter_context(patch.object(
                            telegram, method, return_value=TelegramSendResult(outcome="failed_definitive",
                                                                             error_redacted=failure),
                        ))
                    self.assertTrue(f.process())
                self.assertEqual(f.row()["state"], "queued")
                self.assertEqual(f.row()["retry_count"], 1)
                self.assertFalse(f.process(), "Wait for delivery backoff")
                self.assertEqual(len(telegram.edited_captions) + len(telegram.edited_texts), 0)
                f.reopen()
                f.make_retry_due()
                self.assertTrue(f.process())
                self.assertEqual(f.row()["state"], "completed")
                self.assertEqual(f.row()["card_revision"], original["card_revision"] + 1)
                self.assertEqual(f.row()["ack_message_id"], original["ack_message_id"])
                self.assertEqual(len(f.actions.radarr_calls), 2)
                self.assertEqual(f.identification_calls(), id_calls)
                self.assertEqual(len(f.triage.calls), 1)
                calls = f.actions.deliver_card.await_args_list
                self.assertEqual(len(calls), 3)
                self.assertEqual(calls[1], calls[2])
                f.actions.deliver_evaluation.assert_awaited_once()
                self.assertEqual(len(telegram.edited_captions) + len(telegram.edited_texts), 1)
                self.assertEqual(len(telegram.sent_photos) + len(telegram.sent_messages), sends)
