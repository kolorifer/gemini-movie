"""Delivery recovery must consume the previously completed lookup and identification."""
import json
import unittest
from contextlib import ExitStack

from services.movie_intake_api.clients import DeliveryCardResponse
from services.movie_intake_api.tests.test_radarr_timeout_retry import ABSENT, TIMEOUT, RetryFixture


class TestRadarrRetryDelivery(unittest.TestCase):
    def test_card_failure_after_final_lookup_retries_only_delivery(self):
        for listing_has_id in (True, False):
            for responses in ([TIMEOUT, ABSENT], [TIMEOUT] * 3):
                with self.subTest(listing_has_id=listing_has_id, lookups=len(responses)), ExitStack() as stack:
                    f = RetryFixture(stack, listing_has_id=listing_has_id, responses=responses)
                    f.process()
                    identity_calls = f.identification_calls()
                    for _ in responses[2:]:
                        f.make_retry_due()
                        f.process()
                    f.actions._card_handler = lambda _: DeliveryCardResponse(status="failed")
                    f.make_retry_due()
                    f.process()
                    self.assertEqual(f.row()["state"], "queued")
                    checkpoint = json.loads(f.row()["pending_radarr_retry_json"])
                    self.assertEqual(checkpoint["attempts"], len(responses))
                    self.assertEqual(checkpoint["lookup_result"]["status"], responses[-1].status)
                    self.assertIsNone(f.row()["ack_message_id"])

                    f.reopen()
                    f.actions._card_handler = None
                    f.make_retry_due()
                    f.process()
                    self.assertEqual(f.row()["state"], "completed")
                    self.assertIsNone(f.row()["pending_radarr_retry_json"])
                    self.assertEqual(f.identification_calls(), identity_calls)
                    self.assertEqual(len(f.triage.calls), 1)
                    self.assertEqual(len(f.actions.radarr_calls), len(responses))
                    self.assertEqual(len(f.actions.card_calls), 2)
                    self.assertEqual(f.actions.card_calls[0], f.actions.card_calls[1])
                    self.assertEqual(len(f.actions.eval_reply_calls), 1)
                    self.assertFalse(f.process())

    def test_orphan_recovery_after_card_ack_keeps_lookup_and_reuses_card(self):
        with ExitStack() as stack:
            f = RetryFixture(stack, listing_has_id=True, responses=[TIMEOUT, ABSENT])
            f.process()
            identity_calls = f.identification_calls()
            f.make_retry_due()
            evaluate = f.triage.evaluate

            async def interrupted(_body):
                raise RuntimeError("worker interrupted before evaluation")

            f.triage.evaluate = interrupted
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                f.process()
            self.assertGreater(f.row()["ack_message_id"], 0)
            self.assertEqual(len(f.actions.card_calls), 1)
            f.triage.evaluate = evaluate
            f.reopen()
            recovered = f.db.recover_orphaned_leases(active_worker_id=f.worker.worker_id, recover_dead_owners=True)
            self.assertEqual(recovered, 1)
            f.process()
            self.assertEqual(f.row()["state"], "completed")
            self.assertIsNone(f.row()["pending_radarr_retry_json"])
            self.assertEqual(f.identification_calls(), identity_calls)
            self.assertEqual(len(f.actions.radarr_calls), 2)
            self.assertEqual(len(f.triage.calls), 1)
            self.assertEqual(len(f.actions.card_calls), 1)
            self.assertEqual(len(f.actions.eval_reply_calls), 1)
