"""Exercise Radarr-only retries through the intake webhook, queue and card delivery."""
import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from services.movie_intake_api.app import create_app
from services.movie_intake_api.clients import ActionsApiClient, RadarrStatusResponse, TriageEvalResult
from services.movie_intake_api.db import IntakeDB
from services.movie_intake_api.tests.test_intake_api import (
    StubActionsClient,
    StubTlScraper,
    StubTmdbClient,
    StubTriageClient,
    _make_autobrr_movie_webhook,
    _make_config,
    _make_eval_payload,
)
from services.movie_intake_api.tmdb_verifier import TmdbMovieRecord
from services.movie_intake_api.torrentleech import TorrentLeechResult

MOVIE = TmdbMovieRecord(
    tmdb_id=8765432, title="Example Film", year=2024, imdb_id="tt2024001",
    runtime_minutes=118, director="Director Name",
)
TIMEOUT = RadarrStatusResponse(status="lookup_failed", detail="ReadTimeout")
ABSENT = RadarrStatusResponse(status="absent")


class RetryFixture:
    def __init__(self, stack, *, listing_has_id, responses):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.cfg = _make_config(root, retry_backoff_seconds=60)
        self.db = IntakeDB(self.cfg.intake_db_path)
        self.db.init_db()
        self.tmdb = StubTmdbClient(
            self.cfg.tmdb_cache_path, {MOVIE.tmdb_id: MOVIE}, {MOVIE.imdb_id: [MOVIE.tmdb_id]}
        )
        self.tmdb.find_movies_by_imdb_id = AsyncMock(wraps=self.tmdb.find_movies_by_imdb_id)
        self.tmdb.get_movie_by_tmdb_id = AsyncMock(wraps=self.tmdb.get_movie_by_tmdb_id)
        listing = TorrentLeechResult(
            outcome="listing_id_found" if listing_has_id else "listing_id_missing",
            imdb_id=MOVIE.imdb_id if listing_has_id else None,
            file_runtime_minutes=118,
        )
        self.tl = StubTlScraper(self.db, self.cfg.tl_cookie_file, lambda *_: listing)
        evaluation = _make_eval_payload(
            title=MOVIE.title, year=MOVIE.year, tmdb_id=MOVIE.tmdb_id, imdb_id=MOVIE.imdb_id
        )
        self.triage = StubTriageClient(lambda _: TriageEvalResult(ok=True, payload=evaluation))
        results = iter(responses)
        self.actions = StubActionsClient(radarr_handler=lambda *_: next(results))
        self.webhook = _make_autobrr_movie_webhook(meta_imdb=MOVIE.imdb_id if listing_has_id else None)
        self.app_lifetime = stack.enter_context(ExitStack())
        self.reopen()
        self.release_id = self.post(self.webhook)["release_id"]

    def reopen(self):
        self.app_lifetime.close()
        self.db = IntakeDB(self.cfg.intake_db_path)
        self.db.init_db()
        app = create_app(
            self.cfg, db=self.db, tl_scraper=self.tl, tmdb_client=self.tmdb,
            triage_client=self.triage, actions_client=self.actions, start_worker=False,
        )
        self.client = self.app_lifetime.enter_context(TestClient(app))
        self.worker = app.state.worker

    def post(self, webhook):
        response = self.client.post("/webhook/test-webhook-secret", json=webhook)
        response.raise_for_status()
        return response.json()

    def process(self):
        return asyncio.run(self.worker.process_next_queued())

    def make_retry_due(self):
        with sqlite3.connect(self.cfg.intake_db_path) as conn:
            conn.execute("UPDATE releases SET next_attempt_at = NULL WHERE release_id = ?", (self.release_id,))

    def row(self):
        return self.db.get_release(self.release_id)

    def identification_calls(self):
        return (len(self.tl.calls), self.tmdb.get_movie_by_tmdb_id.await_count,
                self.tmdb.find_movies_by_imdb_id.await_count)


class TestRadarrTimeoutRetry(unittest.TestCase):
    def test_restart_retries_only_the_verified_lookup_and_delivers_once(self):
        for listing_has_id, timeout in (
            (True, TIMEOUT),
            (False, RadarrStatusResponse(status="lookup_failed", detail="radarr_read_failed:ReadTimeout")),
        ):
            with self.subTest(listing_has_id=listing_has_id), ExitStack() as stack:
                f = RetryFixture(stack, listing_has_id=listing_has_id, responses=[timeout, ABSENT])
                self.assertTrue(f.process())
                self.assertEqual(f.row()["state"], "queued")
                self.assertIsNotNone(f.row()["next_attempt_at"])
                self.assertFalse(f.process(), "The backoff must prevent a hot retry loop")
                self.assertEqual(len(f.actions.radarr_calls), 1)
                self.assertEqual(f.actions.card_calls, [])
                if not listing_has_id:
                    self.assertEqual(len(f.triage.calls), 1, "The evaluator identified this release")
                initial_id_calls = f.identification_calls()
                self.assertTrue(all(initial_id_calls))
                f.post(f.webhook)  # Identical evidence arriving between attempts.
                f.reopen()
                f.make_retry_due()
                self.assertTrue(f.process())
                self.assertEqual(f.row()["state"], "completed")
                self.assertEqual(f.identification_calls(), initial_id_calls)
                self.assertEqual(len(f.triage.calls), 1)
                self.assertEqual(len(f.actions.radarr_calls), 2)
                self.assertEqual(f.actions.radarr_calls[0], f.actions.radarr_calls[1])
                self.assertEqual([card["delivery_kind"] for card in f.actions.card_calls], ["actionable_grab"])
                self.assertEqual(len(f.actions.eval_reply_calls), 1)
                self.assertEqual(f.actions.eval_reply_calls[0]["ack_message_id"], f.row()["ack_message_id"])
                self.assertFalse(f.process())

    def test_three_timeouts_yield_one_review_card_and_no_more_retries(self):
        with ExitStack() as stack:
            f = RetryFixture(stack, listing_has_id=False, responses=[TIMEOUT] * 3)
            for _ in range(2):
                self.assertTrue(f.process())
                self.assertEqual(f.row()["state"], "queued")
                self.assertFalse(f.actions.card_calls)
                f.make_retry_due()
            self.assertTrue(f.process())
            self.assertEqual(f.row()["state"], "completed")
            self.assertFalse(f.process())
            self.assertEqual(len(f.actions.radarr_calls), 3)
            self.assertEqual(len(f.tl.calls), 1)
            self.assertEqual(len(f.triage.calls), 1)
            self.assertEqual([c["delivery_kind"] for c in f.actions.card_calls], ["evidence_review"])
            self.assertEqual(f.actions.card_calls[0]["verified_tmdb_id"], MOVIE.tmdb_id)
            self.assertEqual(f.actions.card_calls[0]["radarr_status"], "lookup_failed")
            self.assertEqual(len(f.actions.eval_reply_calls), 1)

    def test_retry_checkpoint_omits_credential_fields_and_redacts_urls(self):
        with ExitStack() as stack:
            f = RetryFixture(stack, listing_has_id=False, responses=[TIMEOUT, ABSENT])
            result = _make_eval_payload(title=MOVIE.title, year=MOVIE.year,
                                        tmdb_id=MOVIE.tmdb_id, imdb_id=MOVIE.imdb_id)
            result["extra"] = {"download_url": "invented-download", "cookie": "invented-cookie",
                               "headers": {"x-example": "invented-header"},
                               "note": "see https://example.invalid/private-listing"}
            f.triage._handler = lambda _: TriageEvalResult(ok=True, payload=result)
            self.assertTrue(f.process())
            checkpoint = f.row()["pending_radarr_retry_json"]
            for marker in ("invented-download", "invented-cookie", "invented-header", "private-listing"):
                self.assertNotIn(marker, checkpoint)
            f.reopen()
            f.make_retry_due()
            self.assertTrue(f.process())
            self.assertEqual(len(f.triage.calls), 1)
            self.assertEqual(f.row()["state"], "completed")

    def test_permanent_and_ambiguous_statuses_do_not_retry(self):
        for status, detail in (("lookup_failed", "radarr_http_401"), ("ambiguous", "multiple_matches")):
            with self.subTest(detail=detail), ExitStack() as stack:
                f = RetryFixture(
                    stack, listing_has_id=True, responses=[RadarrStatusResponse(status=status, detail=detail)]
                )
                self.assertTrue(f.process())
                self.assertEqual(f.row()["state"], "completed")
                self.assertFalse(f.process())
                self.assertEqual(len(f.actions.radarr_calls), 1)
                self.assertEqual([c["delivery_kind"] for c in f.actions.card_calls], ["evidence_review"])

    def test_corrected_occurrence_cannot_reuse_the_old_verified_identity(self):
        with ExitStack() as stack:
            f = RetryFixture(stack, listing_has_id=True, responses=[TIMEOUT])
            self.assertTrue(f.process())
            self.assertEqual(f.row()["state"], "queued")
            f.post(_make_autobrr_movie_webhook(meta_imdb="tt9999002"))
            f.make_retry_due()
            self.assertTrue(f.process())
            self.assertGreater(len(f.tl.calls), 1, "A materially changed occurrence must be reverified")
            self.assertEqual(len(f.actions.radarr_calls), 1, "Never act on the superseded identity")
            self.assertEqual([c["delivery_kind"] for c in f.actions.card_calls], ["evidence_review"])

    def test_historical_acknowledged_timeout_edits_only_the_existing_card(self):
        for recovered, expected_kind in (
            (ABSENT, "actionable_grab"),
            (RadarrStatusResponse(status="watched_unmonitored"), "retired"),
        ):
            with self.subTest(status=recovered.status), ExitStack() as stack:
                f = RetryFixture(stack, listing_has_id=True, responses=[
                    RadarrStatusResponse(status="lookup_failed", detail="radarr_http_401"), recovered,
                ])
                self.assertTrue(f.process())
                original = f.row()
                self.assertEqual(original["eval_reply_status"], "sent")
                self.assertEqual([c["delivery_kind"] for c in f.actions.card_calls], ["evidence_review"])
                # Model the previous on-disk schema with an acknowledged timeout card.
                with sqlite3.connect(f.cfg.intake_db_path) as conn:
                    conn.execute(
                        "UPDATE releases SET radarr_status_snapshot=? WHERE release_id=?",
                        ('{"status":"lookup_failed","detail":"ReadTimeout"}', f.release_id),
                    )
                    conn.execute("ALTER TABLE releases DROP COLUMN pending_radarr_retry_json")
                id_calls = f.identification_calls()
                f.reopen()
                f.make_retry_due()
                self.assertTrue(f.process())
                self.assertEqual(f.identification_calls(), id_calls)
                self.assertEqual(len(f.triage.calls), 1)
                self.assertEqual(len(f.actions.eval_reply_calls), 1)
                self.assertEqual(len(f.actions.card_calls), 2)  # One initial send and one edit.
                edit = f.actions.card_calls[1]
                self.assertEqual(edit["delivery_kind"], expected_kind)
                self.assertEqual(edit["card_revision"], original["card_revision"] + 1)
                self.assertEqual(edit["expected_ack"], {
                    "chat_id": original["ack_chat_id"],
                    "message_id": original["ack_message_id"],
                    "thread_id": original["ack_thread_id"],
                    "revision": original["card_revision"],
                })
                self.assertEqual(f.row()["ack_message_id"], original["ack_message_id"])
                self.assertFalse(f.process())
                f.reopen()
                self.assertFalse(f.process(), "Startup must not enroll the same historical row again")

    def test_upgrade_skips_timeout_without_an_ack_for_the_current_evidence(self):
        with ExitStack() as stack:
            f = RetryFixture(stack, listing_has_id=True, responses=[
                RadarrStatusResponse(status="lookup_failed", detail="radarr_http_401")
            ])
            self.assertTrue(f.process())
            with sqlite3.connect(f.cfg.intake_db_path) as conn:
                conn.execute(
                    "UPDATE releases SET radarr_status_snapshot=?, ack_incoming_digest=NULL WHERE release_id=?",
                    ('{"status":"lookup_failed","detail":"ReadTimeout"}', f.release_id),
                )
                conn.execute("ALTER TABLE releases DROP COLUMN pending_radarr_retry_json")
            f.reopen()
            self.assertFalse(f.process())
            self.assertEqual(f.row()["state"], "completed")
            self.assertEqual(len(f.actions.radarr_calls), 1)
            self.assertEqual(len(f.actions.card_calls), 1)

    def test_correction_arriving_during_retry_cannot_publish_a_stale_grab_card(self):
        with ExitStack() as stack:
            f = RetryFixture(stack, listing_has_id=True, responses=[TIMEOUT])
            self.assertTrue(f.process())
            self.assertEqual(f.row()["state"], "queued")

            def lookup_during_correction(_tmdb, _imdb):
                f.post(_make_autobrr_movie_webhook(meta_imdb="tt9999002"))
                return ABSENT

            f.actions._radarr_handler = lookup_during_correction
            f.make_retry_due()
            self.assertTrue(f.process())
            self.assertEqual(f.actions.card_calls, [])
            self.assertEqual(f.row()["state"], "queued")
            f.make_retry_due()
            self.assertTrue(f.process())
            self.assertEqual([c["delivery_kind"] for c in f.actions.card_calls], ["evidence_review"])


class TestRadarrTimeoutClassification(unittest.IsolatedAsyncioTestCase):
    async def test_transport_read_timeout_with_nonempty_message_is_retryable(self):
        def timeout(_request):
            raise httpx.ReadTimeout("invented private exception detail")

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
            actions = ActionsApiClient(base_url="http://localhost", internal_token="", http_client=http)
            self.assertEqual(
                await actions.get_radarr_status(tmdb_id=MOVIE.tmdb_id, imdb_id=MOVIE.imdb_id),
                TIMEOUT,
            )
