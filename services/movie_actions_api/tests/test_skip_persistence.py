import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import httpx

from services.movie_actions_api.clients import RadarrClient, ShadowStubRadarrClient
from services.movie_actions_api.db import ActionsDB, NewActionRecord, utc_now, utc_now_iso


class SkipEditorTest(unittest.IsolatedAsyncioTestCase):
    async def test_editor_only_sends_only_monitoring_and_preserves_unrelated_fields(self):
        movie = {"id": 101, "monitored": True, "tags": [9], "qualityProfileId": 4, "movieFileId": 5}
        # Ignore unrelated fields even when the caller supplies a stale snapshot.
        payload = {"editor_only": True, "monitored": False, "tags": [], "qualityProfileId": 1}
        expected = {**movie, "monitored": False}

        def handle(request):
            self.assertEqual((request.method, request.url.path), ("PUT", "/api/v3/movie/editor"))
            self.assertEqual(json.loads(request.content), {"movieIds": [101], "monitored": False})
            return httpx.Response(202, json=[expected])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            radarr = RadarrClient(base_url="http://radarr.invalid", api_key="test", http_client=http)
            result = await radarr.update_movie(101, payload)
            self.assertEqual((result.outcome, result.data), ("ok", expected))

        stub = ShadowStubRadarrClient()
        stub.movies[101] = movie
        result = await stub.update_movie(101, payload)
        self.assertEqual((result.outcome, stub.movies[101]), ("ok", expected))


class SkipPersistenceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = ActionsDB(Path(directory.name) / "actions.db")
        self.db.init_db()
        self.deadline = utc_now_iso(utc_now() + timedelta(days=1))
        action = NewActionRecord(
            action_id="skip", token_sha256="digest", release_id="release", card_revision=1,
            source_occurrence_id="occurrence", action_type="skip", completion_group="skip-group",
            bound_tmdb_id=101, bound_imdb_id=None, bound_title="Movie", bound_year=2025,
            bound_release_name="Movie.2025", bound_existing_movie_id=None, bound_existing_file_id=None,
            bound_existing_file_quality=None, bound_rejection_class=None, expires_at=self.deadline,
        )
        self.assertEqual(self.plan_card(1, [action]).mode, "send_new")
        self.db.finish_card_delivery_success(
            "release", card_revision=1, delivery_kind="actionable_grab", ack_chat_id="chat",
            ack_message_id=2, ack_thread_id=None, used_poster=False, is_edit=False,
        )

    def plan_card(self, revision, actions):
        return self.db.begin_card_delivery(
            release_id="release", card_revision=revision, delivery_kind="actionable_grab",
            target_chat_id="chat", target_thread_id=None, process_instance_id="worker",
            lease_expires_at=self.deadline, new_actions=actions,
        )

    def begin_attempt(self, phase, from_status):
        ok, attempt_id, reason = self.db.begin_write_attempt(
            action_id="skip", completion_group="skip-group", phase=phase,
            process_instance_id="worker", lease_expires_at=self.deadline, request_fingerprint="fp",
            allowed_action_statuses=frozenset({from_status}),
            forbid_group_completed_radarr=phase != "evaluator_feedback",
        )
        self.assertTrue(ok, reason)
        return attempt_id

    def test_in_flight_and_orphaned_skip_lock_card_revision(self):
        self.begin_attempt("radarr_skip", "active")
        self.assertEqual(self.plan_card(2, []).mode, "locked_completed_radarr")
        self.db.recover_orphaned_state("next-worker")
        state = self.db.get_action_group_readonly("digest")
        self.assertEqual(state.action["status"], "ambiguous_radarr")
        self.assertEqual(state.group_attempts[0]["status"], "ambiguous")
        self.assertEqual(self.plan_card(2, []).mode, "locked_completed_radarr")

    def test_dangling_skip_requires_radarr_reconciliation(self):
        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute("UPDATE actions SET status = 'attempting' WHERE action_id = 'skip'")
        self.db.recover_orphaned_state("next-worker")
        self.assertEqual(self.db.get_action_group_readonly("digest").action["status"], "ambiguous_radarr")

    def test_skip_remains_sealed_during_and_after_orphaned_feedback(self):
        attempt_id = self.begin_attempt("radarr_skip", "active")
        self.assertTrue(self.db.finish_write_attempt(
            attempt_id=attempt_id, action_id="skip", attempt_status="succeeded",
            action_status="completed_radarr", feedback_status="pending", radarr_movie_id=101,
        ))
        self.begin_attempt("evaluator_feedback", "completed_radarr")
        self.assertEqual(self.db.get_action_group_readonly("digest").action["status"], "completed_radarr")
        self.db.recover_orphaned_state("next-worker")
        action = self.db.get_action_group_readonly("digest").action
        self.assertEqual((action["status"], action["feedback_status"], action["radarr_movie_id"]),
                         ("completed_radarr", "failed", 101))
        self.assertEqual(self.plan_card(2, []).mode, "locked_completed_radarr")

    def test_skip_lease_checks_expiry_after_lookup(self):
        for status, movie_id, reason in [
            ("active", None, "expired"), ("active", 101, "expired"),
            ("ambiguous_radarr", None, "expired_with_skip_movie_absent"),
            ("ambiguous_radarr", 101, None),
        ]:
            with self.subTest(status=status, existing_movie_id=movie_id):
                with sqlite3.connect(self.db.db_path) as conn:
                    conn.execute("UPDATE actions SET status = ? WHERE action_id = 'skip'", (status,))
                ok, _, conflict = self.db.begin_write_attempt(
                    action_id="skip", completion_group="skip-group", phase="radarr_skip",
                    process_instance_id="worker", lease_expires_at=self.deadline, request_fingerprint="fp",
                    allowed_action_statuses=frozenset({status}), radarr_movie_id=movie_id, now_iso=self.deadline,
                )
                self.assertEqual(ok, reason is None)
                if reason is not None:
                    self.assertEqual(conflict, "expired")
                    state = self.db.get_action_group_readonly("digest")
                    self.assertEqual((state.action["status"], state.action["last_error_redacted"]), ("expired", reason))
                    self.assertEqual(state.group_attempts, ())

    def test_race_baseline_is_recorded_only_while_skip_attempt_owns_claim(self):
        attempt_id = self.begin_attempt("radarr_skip", "active")
        record = self.db.record_skip_race_baseline
        self.assertTrue(record(attempt_id, request_fingerprint="observed_race_winner"))
        self.db.recover_orphaned_state("next-worker")
        self.assertFalse(record(attempt_id, request_fingerprint="too_late"))
        attempt, = self.db.list_attempts_for_action("skip")
        self.assertEqual(attempt["request_fingerprint"], "observed_race_winner")
