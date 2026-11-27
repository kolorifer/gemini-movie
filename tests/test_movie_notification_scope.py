"""Offline owner-policy regression tests for webhook notification scope, cooldowns, and archive durability.

Run directly with the repository on PYTHONPATH to activate the network audit guard:
    PYTHONPATH=. python tests/test_movie_notification_scope.py

All data is invented. Each case uses its own disposable archive and SQLite database.
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import os
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from services.movie_intake_api.app import create_app
from services.movie_intake_api.archive import archive_raw_webhook
from services.movie_intake_api.clients import RadarrStatusResponse, TriageEvalResult
from services.movie_intake_api.config import IntakeConfig
from services.movie_intake_api.db import IntakeDB
from services.movie_intake_api.security import atomic_write_private_file
from services.movie_intake_api.tests.test_intake_api import (
    StubActionsClient,
    StubTlScraper,
    StubTmdbClient,
    StubTriageClient,
    _make_config,
    _make_eval_payload,
)
from services.movie_intake_api.tmdb_verifier import TmdbMovieRecord
from services.movie_intake_api.torrentleech import (
    TorrentLeechResult,
    TorrentLeechScraper,
    parse_retry_after_seconds,
)


def refuse_network(event: str, args: tuple[Any, ...]) -> None:
    if event == "socket.connect" and args[0].family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError("This test permits only in-process test traffic")


class MovieNotificationScope(unittest.TestCase):
    """Exact 8-case owner-policy suite from Issue #13 plus extended eligibility/Op2/Op3 regressions."""

    def _build_harness(
        self,
        scratch: Path,
        *,
        allowed_filter_names: tuple[str, ...] = ("Movies Remux",),
        allowed_filter_ids: tuple[int, ...] = (),
        movie_title: str = "Example Film",
        movie_year: int = 2024,
    ):
        base_cfg = _make_config(scratch)
        cfg = IntakeConfig(
            **{
                **base_cfg.__dict__,
                "allowed_filter_names": allowed_filter_names,
                "allowed_filter_ids": allowed_filter_ids,
            }
        )
        db = IntakeDB(cfg.intake_db_path)
        db.init_db()
        movie = TmdbMovieRecord(
            tmdb_id=990001,
            imdb_id="tt9900001",
            title=movie_title,
            original_title=movie_title,
            year=movie_year,
            runtime_minutes=120,
        )
        catalogue = StubTmdbClient(
            cfg.tmdb_cache_path,
            {movie.tmdb_id: movie},
            {movie.imdb_id: [movie.tmdb_id]},
        )
        tracker = StubTlScraper(
            db,
            cfg.tl_cookie_file,
            lambda *_: TorrentLeechResult(
                outcome="listing_id_found",
                imdb_id=movie.imdb_id,
                file_runtime_minutes=120,
            ),
        )
        evaluator = StubTriageClient(
            lambda _: TriageEvalResult(
                ok=True,
                payload=_make_eval_payload(
                    title=movie.title,
                    year=movie.year,
                    imdb_id=movie.imdb_id,
                    tmdb_id=movie.tmdb_id,
                ),
            )
        )
        actions = StubActionsClient(radarr_handler=lambda *_: RadarrStatusResponse(status="absent"))
        app = create_app(
            cfg,
            db=db,
            tl_scraper=tracker,
            tmdb_client=catalogue,
            triage_client=evaluator,
            actions_client=actions,
            start_worker=False,
        )
        return cfg, db, movie, tracker, evaluator, actions, app

    def check_release(
        self,
        quality: str,
        source: Optional[str],
        processing: Optional[str],
        resolution: Optional[str],
        *,
        matched: bool,
        expected: int,
        filter_obj: Any = "DEFAULT",
        action_obj: Any = "DEFAULT",
        event_override: Optional[str] = None,
        result_obj: Any = "DEFAULT",
        extra_release_fields: Optional[dict[str, Any]] = None,
        movie_title: str = "Example Film",
        movie_year: int = 2024,
        allowed_filter_names: tuple[str, ...] = ("Movies Remux",),
        allowed_filter_ids: tuple[int, ...] = (),
    ) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            cfg, _db, movie, tracker, evaluator, actions, app = self._build_harness(
                Path(scratch),
                allowed_filter_names=allowed_filter_names,
                allowed_filter_ids=allowed_filter_ids,
                movie_title=movie_title,
                movie_year=movie_year,
            )
            title_dots = movie.title.replace(" ", ".")
            release_name = f"{title_dots}.{movie.year}.{quality}-GROUP" if quality else f"{title_dots}.{movie.year}-GROUP"
            release: dict[str, Any] = {
                "name": release_name,
                "type": "movie",
                "title": movie.title,
                "year": movie.year,
                "meta_imdb": movie.imdb_id,
                "category": "Movies :: 4K" if resolution == "2160p" else "Movies :: Bluray",
                "external_id": "990001",
            }
            if resolution is not None:
                release["resolution"] = resolution
            if source is not None:
                release["source"] = source
            if processing is not None:
                release["media_processing"] = processing
            if extra_release_fields:
                release.update(extra_release_fields)

            data: dict[str, Any] = {"indexer": "TorrentLeech", "release": release}
            event = "release.new"
            if matched:
                event = "action.rejected"
                data["action"] = {"type": "RADARR"} if action_obj == "DEFAULT" else action_obj
                if filter_obj != "OMIT":
                    data["filter"] = {"name": "Movies Remux", "id": 7} if filter_obj == "DEFAULT" else filter_obj
                data["result"] = (
                    {"status": "PUSH_REJECTED", "rejections": ["Unknown Movie"]}
                    if result_obj == "DEFAULT"
                    else result_obj
                )
            if event_override is not None:
                event = event_override

            raw = json.dumps({"event": event, "data": data}).encode()
            with TestClient(app) as client:
                response = client.post(
                    f"/webhook/{cfg.webhook_secret}",
                    content=raw,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 200)
                asyncio.run(app.state.worker.process_next_queued())
                archives = list(cfg.webhook_archive_dir.rglob("*.json"))
                self.assertEqual(len(archives), 1)
                self.assertEqual(archives[0].read_bytes(), raw)
                observed = (
                    len(tracker.calls),
                    len(evaluator.calls),
                    len(actions.card_calls),
                    len(actions.eval_reply_calls),
                )
                self.assertEqual(
                    observed,
                    (expected,) * 4,
                    "tracker, evaluator, card, evaluation reply",
                )

    # --- Issue #13 exact 8-case notification scope matrix ---

    def test_unfiltered_web_announcement(self) -> None:
        self.check_release("2160p.WEB-DL", "WEB-DL", "", "2160p", matched=False, expected=0)

    def test_unfiltered_remux_announcement(self) -> None:
        self.check_release("2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p", matched=False, expected=0)

    def test_matched_web_1080(self) -> None:
        self.check_release("1080p.WEB-DL", "WEB-DL", "", "1080p", matched=True, expected=0)

    def test_matched_web_2160(self) -> None:
        self.check_release("2160p.WEB-DL", "WEB-DL", "", "2160p", matched=True, expected=0)

    def test_matched_720_remux(self) -> None:
        self.check_release("720p.BluRay.REMUX", "BluRay", "REMUX", "720p", matched=True, expected=0)

    def test_matched_disc_encode(self) -> None:
        self.check_release("1080p.BluRay.x264", "BluRay", "Encode", "1080p", matched=True, expected=0)

    def test_matched_1080_remux(self) -> None:
        self.check_release("1080p.BluRay.REMUX", "BluRay", "REMUX", "1080p", matched=True, expected=1)

    def test_matched_2160_remux(self) -> None:
        self.check_release("2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p", matched=True, expected=1)

    # --- Extended Issue #13 §1 acceptance scenarios ---

    def test_two_event_sequence_archives_both_and_emits_one_card_after_qualifying_action(self) -> None:
        """Global release.new followed by qualifying action.rejected archives both payloads unchanged and emits 1 card."""
        with tempfile.TemporaryDirectory() as scratch:
            cfg, _db, movie, tracker, evaluator, actions, app = self._build_harness(Path(scratch))
            release = {
                "name": "Example.Film.2024.2160p.UHD.BluRay.REMUX-GROUP",
                "type": "movie",
                "title": movie.title,
                "year": movie.year,
                "meta_imdb": movie.imdb_id,
                "category": "Movies :: 4K",
                "external_id": "990001",
                "resolution": "2160p",
                "source": "UHD BluRay",
                "media_processing": "REMUX",
            }
            raw_new = json.dumps(
                {"event": "release.new", "data": {"indexer": "TorrentLeech", "release": release}}
            ).encode()
            raw_rej = json.dumps(
                {
                    "event": "action.rejected",
                    "data": {
                        "indexer": "TorrentLeech",
                        "action": {"type": "RADARR"},
                        "filter": {"name": "Movies Remux", "id": 7},
                        "result": {"status": "PUSH_REJECTED", "rejections": ["Unknown Movie"]},
                        "release": release,
                    },
                }
            ).encode()

            with TestClient(app) as client:
                r1 = client.post(
                    f"/webhook/{cfg.webhook_secret}",
                    content=raw_new,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(r1.json()["status"], "archived_non_movie")
                self.assertIsNone(r1.json()["release_id"])
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(
                    (len(tracker.calls), len(evaluator.calls), len(actions.card_calls), len(actions.eval_reply_calls)),
                    (0, 0, 0, 0),
                )

                r2 = client.post(
                    f"/webhook/{cfg.webhook_secret}",
                    content=raw_rej,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(r2.json()["status"], "accepted")
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))

                archives = sorted(cfg.webhook_archive_dir.rglob("*.json"))
                self.assertEqual(len(archives), 2)
                archived_bytes = {p.read_bytes() for p in archives}
                self.assertEqual(archived_bytes, {raw_new, raw_rej})
                self.assertEqual(
                    (len(tracker.calls), len(evaluator.calls), len(actions.card_calls), len(actions.eval_reply_calls)),
                    (1, 1, 1, 1),
                )

    def test_mismatched_and_missing_filters_and_non_radarr_actions_rejected(self) -> None:
        # Mismatched filter name
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=0, filter_obj={"name": "Movies WEB-DL", "id": 7},
        )
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=0, filter_obj={"name": "TV 4K", "id": 9},
        )
        # Missing filter object
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=0, filter_obj="OMIT",
        )
        # Filter ID enforcement when configured
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=0, filter_obj={"name": "Movies Remux", "id": 99},
            allowed_filter_ids=(7,),
        )
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=1, filter_obj={"name": "Movies Remux", "id": 7},
            allowed_filter_ids=(7,),
        )
        # Non-Radarr and missing action type
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=0, action_obj={"type": "QBITTORRENT"},
        )
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=0, action_obj={},
        )
        # Approved push must not emit an Add & Grab notification
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "2160p",
            matched=True, expected=0, event_override="action.approved",
            result_obj={"status": "PUSH_APPROVED", "rejections": []},
        )

    def test_absent_quality_evidence_rejected(self) -> None:
        # No quality tokens in name or metadata
        self.check_release("", None, None, None, matched=True, expected=0)
        # Quality in metadata only, absent from release name
        self.check_release("", "UHD BluRay", "REMUX", "2160p", matched=True, expected=0)
        # Missing resolution metadata
        self.check_release("2160p.UHD.BluRay.REMUX", "UHD BluRay", "REMUX", "", matched=True, expected=0)
        # Missing source metadata
        self.check_release("2160p.UHD.BluRay.REMUX", "", "REMUX", "2160p", matched=True, expected=0)
        # Missing media_processing metadata (even if container='mkv' is supplied)
        self.check_release(
            "2160p.UHD.BluRay.REMUX", "UHD BluRay", None, "2160p",
            matched=True, expected=0, extra_release_fields={"container": "mkv"},
        )
        # Missing Blu-ray disc marker in release name
        self.check_release("2160p.REMUX", "UHD BluRay", "REMUX", "2160p", matched=True, expected=0)

    def test_conflicting_name_and_metadata_quality_rejected(self) -> None:
        # Name says WEB-DL while metadata claims UHD BluRay REMUX
        self.check_release("2160p.WEB-DL", "UHD BluRay", "REMUX", "2160p", matched=True, expected=0)
        # Name says UHD BluRay REMUX while metadata says WEB-DL
        self.check_release("2160p.UHD.BluRay.REMUX", "WEB-DL", "REMUX", "2160p", matched=True, expected=0)
        # Name says 1080p while metadata says 2160p
        self.check_release("1080p.BluRay.REMUX", "BluRay", "REMUX", "2160p", matched=True, expected=0)
        # Name says x264 encode while metadata claims REMUX
        self.check_release("1080p.BluRay.x264", "BluRay", "REMUX", "1080p", matched=True, expected=0)
        # Name says BluRay REMUX while metadata says Encode
        self.check_release("1080p.BluRay.REMUX", "BluRay", "Encode", "1080p", matched=True, expected=0)
        # Name contains contradictory x265 + REMUX together
        self.check_release("2160p.UHD.BluRay.REMUX.x265", "UHD BluRay", "REMUX", "2160p", matched=True, expected=0)
        # Name contains contradictory dual resolutions (1080p + 2160p)
        self.check_release("1080p.2160p.BluRay.REMUX", "BluRay", "REMUX", "1080p", matched=True, expected=0)
        # 1080p paired with UHD BluRay disc source is physically incompatible for REMUX
        self.check_release("1080p.BluRay.REMUX", "UHD BluRay", "REMUX", "1080p", matched=True, expected=0)

    def test_title_containing_web_does_not_false_reject_eligible_bluray_remux(self) -> None:
        """Movie titles like 'Charlotte's Web' must not trigger non-disc source false positives."""
        self.check_release(
            "1080p.BluRay.REMUX.AVC",
            "BluRay",
            "REMUX",
            "1080p",
            matched=True,
            expected=1,
            movie_title="Charlotte's Web",
            movie_year=2006,
        )

    def test_op2_tracker_retry_after_honors_multi_hour_deadlines(self) -> None:
        """Op2 & PR #18 Gap 4: Numeric and HTTP-date Retry-After (multi-hour and multi-day, e.g. 8 days) honored verbatim."""
        ref_now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after_seconds("7200", now=ref_now), 7200.0)
        self.assertEqual(parse_retry_after_seconds("14400", now=ref_now), 14400.0)
        eight_days_sec = 8 * 24 * 3600
        self.assertEqual(parse_retry_after_seconds(str(eight_days_sec), now=ref_now), float(eight_days_sec))

        two_hours_later = email.utils.format_datetime(ref_now + timedelta(hours=2), usegmt=True)
        self.assertAlmostEqual(parse_retry_after_seconds(two_hours_later, now=ref_now), 7200.0, delta=1.0)
        eight_days_later = email.utils.format_datetime(ref_now + timedelta(days=8), usegmt=True)
        self.assertAlmostEqual(
            parse_retry_after_seconds(eight_days_later, now=ref_now), float(eight_days_sec), delta=1.0
        )

        with tempfile.TemporaryDirectory() as scratch:
            db = IntakeDB(Path(scratch) / "intake.db")
            db.init_db()
            cookie_file = Path(scratch) / "cookies.json"
            cookie_file.write_text(
                json.dumps([{"name": "PHPSESSID", "value": "invented", "domain": ".torrentleech.org"}]),
                encoding="utf-8",
            )
            os.chmod(cookie_file, 0o600)

            call_count = 0

            def _handler(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(429, headers={"Retry-After": "7200"}, text="Too Many Requests")

            client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
            scraper = TorrentLeechScraper(
                db=db,
                cookie_file=cookie_file,
                enabled=True,
                min_interval_seconds=0.0,
                http_client=client,
            )
            res = asyncio.run(
                scraper.fetch_listing(
                    indexer_norm="torrentleech",
                    external_id="990001",
                    exact_release_name="Example.Film.2024.2160p.UHD.BluRay.REMUX-GROUP",
                )
            )
            self.assertEqual(res.outcome, "listing_temporary_http")
            self.assertEqual(call_count, 1)

            # 1 hour + 1 minute later, the 2-hour tracker cooldown MUST still be active!
            after_61m = datetime.now(timezone.utc) + timedelta(minutes=61)
            self.assertTrue(db.is_tl_cooldown_active(now=after_61m))
            # A shorter local failure must NOT shorten the already-active 2-hour tracker cooldown
            db.record_tl_cooldown(cooldown_seconds=60.0, reason="shorter_local_backoff")
            self.assertTrue(db.is_tl_cooldown_active(now=after_61m))

            # Immediate follow-up request must respect active cooldown with zero network calls
            res2 = asyncio.run(
                scraper.fetch_listing(
                    indexer_norm="torrentleech",
                    external_id="990001",
                    exact_release_name="Example.Film.2024.2160p.UHD.BluRay.REMUX-GROUP",
                )
            )
            self.assertEqual(res2.outcome, "cooldown_active")
            self.assertEqual(call_count, 1)

    def test_op3_archive_durability_syncs_containing_directories(self) -> None:
        """Op3 & PR #18 Gap 4: verify_archive_root, archive_raw_webhook (including post-failure retries), and atomic_write fsync parent dirs."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            archive_root = root / "webhook-archive"
            synced_dirs: list[Path] = []

            from services.movie_intake_api import archive as archive_mod
            from services.movie_intake_api import security as sec_mod
            from services.movie_intake_api.archive import ArchivePersistenceError, verify_archive_root

            orig_fsync_dir = sec_mod.fsync_directory
            fail_once_path: list[Optional[Path]] = [None]

            def _spy_fsync_dir(path: Path) -> None:
                resolved = path.resolve()
                if fail_once_path[0] is not None and resolved == fail_once_path[0]:
                    fail_once_path[0] = None
                    raise OSError("Simulated transient dir fsync error")
                synced_dirs.append(resolved)
                orig_fsync_dir(path)

            raw = b'{"event":"release.new","data":{"invented":true}}'
            with mock.patch.object(sec_mod, "fsync_directory", side_effect=_spy_fsync_dir), mock.patch.object(
                archive_mod, "fsync_directory", side_effect=_spy_fsync_dir
            ):
                # 1) Startup root creation must fsync archive_root.parent AND archive_root!
                self.assertTrue(verify_archive_root(archive_root))
                self.assertIn(root.resolve(), synced_dirs)
                self.assertIn(archive_root.resolve(), synced_dirs)

                # 2) Simulate transient dir-fsync failure after creating 2026/11/26 -> retry MUST STILL sync parent chain!
                synced_dirs.clear()
                month_dir = (archive_root / "2026" / "11").resolve()
                fail_once_path[0] = month_dir
                with self.assertRaises(ArchivePersistenceError):
                    archive_raw_webhook(
                        archive_root,
                        "occ-op3-fail-1",
                        raw,
                        received_at=datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc),
                    )
                self.assertNotIn(month_dir, synced_dirs)

                # Subsequent write into already-created 2026/11/26 MUST still fsync root parent, root, year, month, and day!
                synced_dirs.clear()
                receipt = archive_raw_webhook(
                    archive_root,
                    "occ-op3-test-1",
                    raw,
                    received_at=datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc),
                )
                target_file = (archive_root / receipt.archive_relpath).resolve()
                self.assertEqual(target_file.read_bytes(), raw)
                self.assertIn(root.resolve(), synced_dirs)
                self.assertIn(archive_root.resolve(), synced_dirs)
                self.assertIn((archive_root / "2026").resolve(), synced_dirs)
                self.assertIn(month_dir, synced_dirs)
                self.assertIn(target_file.parent, synced_dirs)

                synced_dirs.clear()
                other_target = root / "private-state" / "sample.bin"
                atomic_write_private_file(other_target, b"durable-bytes")
                self.assertIn(other_target.parent.resolve(), synced_dirs)


if __name__ == "__main__":
    sys.addaudithook(refuse_network)
    unittest.main(verbosity=2)
