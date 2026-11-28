"""A finished user action keeps its original card, revision and evaluation."""
import json
import tempfile
import unittest
from contextlib import ExitStack

import httpx

from services.movie_actions_api.app import create_app as create_actions_app
from services.movie_actions_api.clients import ShadowStubIntakePushClient
from services.movie_actions_api.security import hash_token
from services.movie_actions_api.tests import test_actions_api as actions_fixtures
from services.movie_intake_api.clients import ActionsApiClient
from services.movie_intake_api.tests.test_intake_api import _make_autobrr_movie_webhook
from services.movie_intake_api.tests.test_radarr_timeout_retry import ABSENT, TIMEOUT, RetryFixture


class TestExistingCardLock(unittest.IsolatedAsyncioTestCase):
    async def test_finished_action_before_recovered_lookup_preserves_card_and_evaluation(self):
        for button in ("watched_good", "skip", "add_grab"):
            with self.subTest(button=button), ExitStack() as stack:
                f = RetryFixture(stack, listing_has_id=True, responses=[ABSENT, TIMEOUT, ABSENT])
                state_dir = stack.enter_context(tempfile.TemporaryDirectory())
                cfg = actions_fixtures.TestMovieActionsApi()._make_config(state_dir)
                telegram = actions_fixtures.ControllableTelegramStub()
                actions = create_actions_app(
                    cfg, telegram_client=telegram, radarr_client=actions_fixtures.ControllableRadarrStub(),
                    intake_push_client=ShadowStubIntakePushClient(),
                    triage_feedback_client=actions_fixtures.ControllableFeedbackStub(),
                )
                requests = []

                async def observe_request(req):
                    if req.url.path.startswith("/internal/"):
                        requests.append((req.method, req.url.path, json.loads(req.content)))

                async with actions.router.lifespan_context(actions), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=actions), base_url="http://example.invalid",
                    event_hooks={"request": [observe_request]},
                ) as http:
                    client = ActionsApiClient(base_url=str(http.base_url), internal_token=cfg.internal_api_secret,
                                              http_client=http)
                    f.actions.deliver_card = client.deliver_card
                    f.actions.deliver_evaluation = client.deliver_evaluation
                    self.assertTrue(await f.worker.process_next_queued())
                    original = f.row()
                    self.assertEqual(original["state"], "completed")
                    tokens = actions_fixtures._extract_tokens_from_markup(telegram.sent_messages[0]["reply_markup"])
                    clicked = await http.post(f"/action/{tokens[button]}", data={
                        "confirm": "1", "root_folder_path": "/media/movies", "quality_profile_id": "4",
                    })
                    self.assertEqual(clicked.status_code, 200)
                    group = actions.state.db.get_action_group_readonly(hash_token(tokens[button]))
                    self.assertEqual(group.action["status"], "completed_all")
                    message_counts = (len(telegram.sent_messages), len(telegram.edited_texts))
                    evaluations = len(f.triage.calls)
                    requests.clear()

                    f.post(_make_autobrr_movie_webhook(freeleech=True))
                    self.assertTrue(await f.worker.process_next_queued())
                    self.assertEqual(f.row()["state"], "queued")
                    f.reopen()
                    f.make_retry_due()
                    self.assertTrue(await f.worker.process_next_queued())
                    self.assertEqual(f.row()["state"], "completed")
                    self.assertEqual([(method, route) for method, route, _ in requests],
                                     [("PUT", "/internal/deliver/card")])
                    for field in ("card_revision", "ack_message_id", "ack_chat_id", "ack_thread_id",
                                  "last_card_digest", "last_eval_reply_digest", "ack_incoming_digest"):
                        self.assertEqual(f.row()[field], original[field], field)
                    delivery = actions.state.db.get_delivery(f.release_id)
                    self.assertEqual(delivery["card_revision"], original["card_revision"])
                    self.assertEqual(len(f.triage.calls), evaluations)
                    self.assertEqual((len(telegram.sent_messages), len(telegram.edited_texts)), message_counts)
                    self.assertFalse(await f.worker.process_next_queued())

                    # Older callers without an edit receipt still receive the original delivery.
                    old_payload = dict(requests[0][2])
                    del old_payload["expected_ack"]
                    legacy = await client.deliver_card(old_payload)
                    self.assertEqual((legacy.status, legacy.ack_message_id),
                                     ("delivered", original["ack_message_id"]))
                    self.assertEqual((len(telegram.sent_messages), len(telegram.edited_texts)), message_counts)

    async def test_locked_response_requires_full_original_receipt_and_revision(self):
        receipt = {"chat_id": "-100", "message_id": 47, "thread_id": 81, "revision": 3}
        body = {"status": "locked_completed_radarr", "ack_chat_id": "-100", "ack_message_id": 47,
                "ack_thread_id": 81, "ack_card_revision": 3}
        for changed in ({}, {"ack_chat_id": "-200"}, {"ack_message_id": 48},
                        {"ack_thread_id": 82}, {"ack_card_revision": 2}):
            with self.subTest(changed=changed):
                def respond(request):
                    self.assertEqual(request.method, "PUT")
                    return httpx.Response(200, json={**body, **changed})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                    client = ActionsApiClient(base_url="http://example.invalid", internal_token="fixture",
                                              http_client=http)
                    result = await client.deliver_card({"card_revision": 4, "expected_ack": receipt})
                self.assertEqual(result.status, "ambiguous_delivery" if changed else "locked_completed_radarr")
