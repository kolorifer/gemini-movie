from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi.testclient import TestClient

from services.movie_intake_api.app import create_app
from services.movie_intake_api.archive import archive_raw_webhook
from services.movie_intake_api.classifier import classify_webhook_payload
from services.movie_intake_api.clients import (
    ActionsApiClient,
    DeliveryCardResponse,
    DeliveryEvalResponse,
    RadarrStatusResponse,
    ShadowStubActionsClient,
    ShadowStubTriageClient,
    TriageApiClient,
    TriageEvalResult,
)
from services.movie_intake_api.config import AlternativeGrabPolicy, IntakeConfig
from services.movie_intake_api.db import IntakeDB, utc_now_iso
from services.movie_intake_api.security import PrivateFileError, redact_secrets
from services.movie_intake_api.tmdb_verifier import (
    TmdbIndependentClient,
    TmdbMovieRecord,
)
from services.movie_intake_api.torrentleech import (
    TorrentLeechResult,
    TorrentLeechScraper,
    build_canonical_tl_url,
    load_tl_cookies,
    parse_mediainfo_file_signals,
    parse_tl_listing_html,
)
from services.movie_intake_api.worker import IntakeWorker


def _make_autobrr_movie_webhook(
    *,
    release_name: str = "Example.Film.2024.2160p.UHD.BluRay.REMUX.DV.HDR10-CiNEPHiLES",
    indexer: str = "TorrentLeech",
    external_id: str = "9001001",
    category: str = "Movies :: 4K",
    release_type: str = "movie",
    title: Optional[str] = "Example Film",
    year: Optional[int] = 2024,
    meta_imdb: Optional[str] = "tt2024001",
    meta_tmdb: Optional[int] = None,
    size: int = 68_500_000_000,
    freeleech: Optional[bool] = False,
    action_rejected: bool = False,
    rejections: Optional[list[str]] = None,
    download_url: str = "https://tracker.invalid/download/9001001/passkey=SUPER_SECRET_PASSKEY_VALUE",
    event: str = "PUSH_APPROVED",
) -> dict[str, Any]:
    rel: dict[str, Any] = {
        "name": release_name,
        "type": release_type,
        "category": category,
        "external_id": external_id,
        "size": size,
        "resolution": "2160p" if "2160p" in release_name else "1080p",
        "source": "UHD.BluRay",
        "hdr": ["DV", "HDR10"] if "DV" in release_name else [],
        "media_processing": "REMUX",
        "group": "CiNEPHiLES" if "CiNEPHiLES" in release_name else "FraMeSToR",
        "download_url": download_url,
        "protocol": "torrent",
        "timestamp": "2026-11-25T12:00:00Z",
    }
    if title is not None:
        rel["title"] = title
    if year is not None:
        rel["year"] = year
    if meta_imdb is not None:
        rel["meta_imdb"] = meta_imdb
    if meta_tmdb is not None:
        rel["meta_tmdb"] = meta_tmdb
    if freeleech is not None:
        rel["freeleech"] = freeleech

    return {
        "event": event,
        "action": {"rejected": action_rejected},
        "data": {
            "indexer": indexer,
            "result": {"rejections": rejections or []},
            "release": rel,
        },
    }


def _make_eval_payload(
    *,
    title: str,
    year: int,
    imdb_id: Optional[str],
    tmdb_id: Optional[int],
    confidence: str = "high",
    verdict: str = "WATCH",
) -> dict[str, Any]:
    return {
        "cached": False,
        "identification": {
            "canonical_title": title,
            "original_title": title,
            "year": year,
            "director": "Director Name",
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "runtime_minutes": 118,
            "genres": ["Drama"],
            "match_confidence": confidence,
            "disambiguation_note": None,
        },
        "ratings": {
            "rotten_tomatoes_critics": 91,
            "rotten_tomatoes_audience": 84,
            "metacritic": 82,
            "imdb": 7.6,
            "letterboxd": 4.0,
            "missing_sources": [],
        },
        "recognition": {
            "awards_highlights": ["Cannes Competition"],
            "critic_consensus": "Sharp and memorable.",
        },
        "decision_aid": {
            "one_line_pitch": "A compelling thriller.",
            "similar_to": ["Film A", "Film B"],
            "red_flags": [],
            "verdict": verdict,
            "verdict_reason": "Strong acclaim and 2160p DV remux.",
        },
    }


class StubTmdbClient(TmdbIndependentClient):
    def __init__(self, tmp_cache: Path, movies: dict[int, TmdbMovieRecord], imdb_map: dict[str, list[int]]) -> None:
        super().__init__(api_token="stub-token", cache_path=tmp_cache)
        self._movies = movies
        self._imdb_map = imdb_map

    async def get_movie_by_tmdb_id(self, tmdb_id: int) -> Optional[TmdbMovieRecord]:
        return self._movies.get(tmdb_id)

    async def find_movies_by_imdb_id(self, imdb_id: str) -> Optional[list[int]]:
        return self._imdb_map.get(imdb_id.lower(), [])


class StubTlScraper(TorrentLeechScraper):
    def __init__(self, db: IntakeDB, cookie_file: Path, handler: Any) -> None:
        super().__init__(db=db, cookie_file=cookie_file, enabled=True, min_interval_seconds=0.0)
        self._handler = handler
        self.calls: list[dict[str, str]] = []

    async def fetch_listing(
        self,
        *,
        indexer_norm: str,
        external_id: str,
        exact_release_name: str,
    ) -> TorrentLeechResult:
        if indexer_norm != "torrentleech":
            return TorrentLeechResult(outcome="not_applicable")
        self.calls.append(
            {
                "indexer_norm": indexer_norm,
                "external_id": external_id,
                "exact_release_name": exact_release_name,
            }
        )
        return self._handler(external_id, exact_release_name)


class StubTriageClient(TriageApiClient):
    is_shadow_stub: bool = True

    def __init__(self, handler: Any) -> None:
        super().__init__(base_url="http://127.0.0.1:40716", internal_token="stub")
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    async def evaluate(self, request_body: dict[str, Any]) -> TriageEvalResult:
        self.calls.append(request_body)
        return self._handler(request_body)


class StubActionsClient(ActionsApiClient):
    is_shadow_stub: bool = True

    def __init__(
        self,
        *,
        radarr_handler: Any,
        card_handler: Optional[Any] = None,
        eval_reply_handler: Optional[Any] = None,
    ) -> None:
        super().__init__(base_url="http://127.0.0.1:40721", internal_token="stub")
        self._radarr_handler = radarr_handler
        self._card_handler = card_handler
        self._eval_reply_handler = eval_reply_handler
        self.radarr_calls: list[dict[str, Any]] = []
        self.card_calls: list[dict[str, Any]] = []
        self.eval_reply_calls: list[dict[str, Any]] = []
        self._next_msg_id = 500
        self._delivered_by_release: dict[str, DeliveryCardResponse] = {}

    async def get_radarr_status(
        self,
        *,
        tmdb_id: Optional[int],
        imdb_id: Optional[str],
        release_title: Optional[str] = None,
    ) -> RadarrStatusResponse:
        self.radarr_calls.append({"tmdb_id": tmdb_id, "imdb_id": imdb_id, "release_title": release_title})
        return self._radarr_handler(tmdb_id, imdb_id)

    async def deliver_card(self, card_payload: dict[str, Any]) -> DeliveryCardResponse:
        self.card_calls.append(card_payload)
        if self._card_handler is not None:
            return self._card_handler(card_payload)
        rel_id = str(card_payload["release_id"])
        rev = int(card_payload["card_revision"])
        # Simulate movie-actions-api idempotent delivery table keyed on release_id
        if rev == 1 and rel_id in self._delivered_by_release:
            return self._delivered_by_release[rel_id]
        if rel_id in self._delivered_by_release:
            prev = self._delivered_by_release[rel_id]
            return prev
        self._next_msg_id += 1
        resp = DeliveryCardResponse(
            status="delivered",
            ack_chat_id="-100123456",
            ack_message_id=self._next_msg_id,
            ack_thread_id=42,
        )
        self._delivered_by_release[rel_id] = resp
        return resp

    async def deliver_evaluation(self, eval_reply_payload: dict[str, Any]) -> DeliveryEvalResponse:
        self.eval_reply_calls.append(eval_reply_payload)
        if self._eval_reply_handler is not None:
            return self._eval_reply_handler(eval_reply_payload)
        return DeliveryEvalResponse(status="sent")


def _make_config(tmpdir: Path, **overrides: Any) -> IntakeConfig:
    base = {
        "host": "127.0.0.1",
        "port": 40720,
        "webhook_secret": "test-webhook-secret",
        "internal_api_secret": "test-internal-secret",
        "tl_metadata_enabled": True,
        "tl_cookie_file": tmpdir / "cookies.json",
        "tmdb_api_token": "stub-tmdb",
        "tmdb_cache_path": tmpdir / "tmdb-cache.json",
        "webhook_archive_dir": tmpdir / "webhook-archive",
        "intake_db_path": tmpdir / "intake.db",
        "alternative_grab_config_path": None,
        "triage_api_url": "http://127.0.0.1:40716",
        "actions_api_url": "http://127.0.0.1:40721",
        "shadow_replay_enabled": False,
        "worker_lease_seconds": 90,
        "worker_poll_interval_seconds": 0.1,
        "tl_min_interval_seconds": 0.0,
        "retry_backoff_seconds": 0.0,
    }
    base.update(overrides)
    return IntakeConfig(**base)


class TestMovieIntakeApi(unittest.TestCase):
    def test_config_enforces_absolute_paths_loopback_and_quality_only_policy(self) -> None:
        with self.assertRaises(ValueError):
            IntakeConfig.from_env({"MOVIE_INTEL_HOST": "0.0.0.0"})
        with self.assertRaises(ValueError):
            IntakeConfig.from_env({"WEBHOOK_ARCHIVE_DIR": "relative/path"})
        with self.assertRaises(ValueError):
            IntakeConfig.from_env({"MOVIE_INTEL_TL_COOKIE_FILE": "~/.config/cookies.json"})

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "alt-policy.json"
            p.write_text(json.dumps({"enabled": True, "allowed_rejection_classes": ["cutoff_met"]}))
            os.chmod(p, 0o600)
            policy = AlternativeGrabPolicy.load_from_file(p)
            self.assertTrue(policy.enabled)
            self.assertEqual(policy.allowed_rejection_classes, frozenset({"cutoff_met"}))

            # Non-quality rejection class must be rejected
            p.write_text(json.dumps({"enabled": True, "allowed_rejection_classes": ["identity_conflict"]}))
            with self.assertRaises(ValueError):
                AlternativeGrabPolicy.load_from_file(p)

            # World-readable policy file must be rejected
            p.write_text(json.dumps({"enabled": True, "allowed_rejection_classes": ["cutoff_met"]}))
            os.chmod(p, 0o644)
            with self.assertRaises(PrivateFileError):
                AlternativeGrabPolicy.load_from_file(p)

        # Redaction scrubs webhook/action secrets and passkeys
        redacted = redact_secrets(
            "POST /webhook/my_secret_123 GET /action/tok_abc https://tl.org/dl?passkey=xyz123"
        )
        self.assertIn("/webhook/[REDACTED]", redacted)
        self.assertIn("/action/[REDACTED]", redacted)
        self.assertNotIn("my_secret_123", redacted)
        self.assertNotIn("tok_abc", redacted)
        self.assertNotIn("xyz123", redacted)

    def test_webhook_auth_limits_strict_json_and_archive_before_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            # 1) Missing webhook secret -> 503
            app_unconf = create_app(_make_config(tmpdir, webhook_secret=""), start_worker=False)
            with TestClient(app_unconf) as client:
                r503 = client.post("/webhook/anything", json={"event": "TEST"})
                self.assertEqual(r503.status_code, 503)

            # 2) Configured secret: wrong -> 404; >1MB -> 413; NaN/Infinity -> 400; non-movie -> 200 archived
            cfg = _make_config(tmpdir)
            app = create_app(cfg, start_worker=False)
            with TestClient(app) as client:
                self.assertEqual(client.post("/webhook/wrong", json={"a": 1}).status_code, 404)

                oversized = b'{"x":"' + (b"a" * 1_000_005) + b'"}'
                r413 = client.post(
                    "/webhook/test-webhook-secret",
                    content=oversized,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(r413.status_code, 413)

                r_nan = client.post(
                    "/webhook/test-webhook-secret",
                    content=b'{"x": NaN}',
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(r_nan.status_code, 400)

                r_arr = client.post(
                    "/webhook/test-webhook-secret",
                    content=b"[1, 2, 3]",
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(r_arr.status_code, 400)

                # Non-movie / announce / IRC event archived BEFORE filtering -> 200, no release created
                r_irc = client.post(
                    "/webhook/test-webhook-secret",
                    json={"event": "IRC", "data": {"message": "announce ping"}},
                )
                self.assertEqual(r_irc.status_code, 200)
                self.assertEqual(r_irc.json()["status"], "archived_non_movie")
                self.assertIsNone(r_irc.json()["release_id"])

                # Movie category + conflicting TV release type -> diagnostic only, archived, no release
                tv_in_movie_cat = _make_autobrr_movie_webhook(
                    release_name="Some.Show.S01E02.2160p.WEB-DL-GRP",
                    category="Movies :: 4K",
                    release_type="tv",
                )
                r_diag = client.post("/webhook/test-webhook-secret", json=tv_in_movie_cat)
                self.assertEqual(r_diag.status_code, 200)
                self.assertEqual(r_diag.json()["status"], "diagnostic_only")
                self.assertIsNone(r_diag.json()["release_id"])

                # Verify both archived on disk with 0700 dirs and 0600 files
                db: IntakeDB = app.state.db
                occs = db.list_all_occurrences()
                self.assertEqual(len(occs), 2)
                self.assertEqual(len(db.list_all_releases()), 0)
                for occ in occs:
                    arch_file = cfg.webhook_archive_dir / occ["archive_relpath"]
                    self.assertTrue(arch_file.exists())
                    self.assertEqual(stat.S_IMODE(arch_file.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(arch_file.parent.stat().st_mode), 0o700)

            # 3) Failed required archive write (symlinked archive root) -> 503
            bad_root = tmpdir / "symlink-archive"
            os.symlink(tmpdir / "webhook-archive", bad_root)
            app_bad = create_app(_make_config(tmpdir, webhook_archive_dir=bad_root), start_worker=False)
            with TestClient(app_bad) as client:
                r_arch_fail = client.post("/webhook/test-webhook-secret", json={"event": "TEST"})
                self.assertEqual(r_arch_fail.status_code, 503)

    def test_push_payload_fail_closed_auth_sha256_integrity_and_zero_secrets_in_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            app = create_app(cfg, start_worker=False)
            with TestClient(app) as client:
                webhook = _make_autobrr_movie_webhook(
                    download_url="https://tracker.invalid/dl/9001001/passkey=TOP_SECRET_PASSKEY_999"
                )
                r_in = client.post("/webhook/test-webhook-secret", json=webhook)
                self.assertEqual(r_in.status_code, 200)
                rel_id = r_in.json()["release_id"]
                occ_id = r_in.json()["occurrence_id"]

                # Zero secrets in intake.db!
                raw_db_bytes = cfg.intake_db_path.read_bytes()
                self.assertNotIn(b"TOP_SECRET_PASSKEY_999", raw_db_bytes)
                self.assertNotIn(b"tracker.invalid", raw_db_bytes)

                # Missing or wrong X-Internal-Token -> 401
                self.assertEqual(
                    client.post(
                        f"/internal/releases/{rel_id}/push-payload",
                        json={"source_occurrence_id": occ_id},
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    client.post(
                        f"/internal/releases/{rel_id}/push-payload",
                        json={"source_occurrence_id": occ_id},
                        headers={"X-Internal-Token": "wrong"},
                    ).status_code,
                    401,
                )

                # Valid token returns ephemeral push payload with Cache-Control: no-store
                r_ok = client.post(
                    f"/internal/releases/{rel_id}/push-payload",
                    json={"source_occurrence_id": occ_id},
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r_ok.status_code, 200)
                self.assertEqual(r_ok.headers.get("cache-control"), "no-store")
                pp = r_ok.json()["push_payload"]
                self.assertEqual(
                    pp["downloadUrl"],
                    "https://tracker.invalid/dl/9001001/passkey=TOP_SECRET_PASSKEY_999",
                )
                self.assertEqual(pp["guid"], "torrentleech:9001001")

                # Tampering with archived file causes SHA-256 mismatch -> 409
                occ = app.state.db.list_all_occurrences()[0]
                arch_file = cfg.webhook_archive_dir / occ["archive_relpath"]
                arch_file.write_text('{"tampered": true}')
                os.chmod(arch_file, 0o600)
                r_tamper = client.post(
                    f"/internal/releases/{rel_id}/push-payload",
                    json={"source_occurrence_id": occ_id},
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r_tamper.status_code, 409)

                # Shadow replay disabled by default -> 404 even with valid token
                r_rep = client.post(
                    "/internal/replay",
                    json=webhook,
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r_rep.status_code, 404)

            # Unconfigured INTERNAL_API_SECRET fails closed with 503
            app_no_internal = create_app(_make_config(tmpdir, internal_api_secret=""), start_worker=False)
            with TestClient(app_no_internal) as client:
                r_503 = client.post(
                    f"/internal/releases/{rel_id}/push-payload",
                    json={"source_occurrence_id": occ_id},
                    headers={"X-Internal-Token": "anything"},
                )
                self.assertEqual(r_503.status_code, 503)

    def test_torrentleech_cookies_allowlist_html_mediainfo_and_cooldown_invariants(self) -> None:
        # Canonical HTTPS allowlist
        self.assertEqual(
            build_canonical_tl_url("123456"),
            "https://www.torrentleech.org/torrent/123456",
        )
        self.assertIsNone(build_canonical_tl_url("012345"))
        self.assertIsNone(build_canonical_tl_url("12345/download"))
        self.assertIsNone(build_canonical_tl_url("https://evil.test/1"))

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cookie_file = tmpdir / "cookies.json"
            cookie_file.write_text(
                json.dumps(
                    [
                        {"name": "tluid", "value": "123", "domain": ".torrentleech.org"},
                        {"name": "other", "value": "ignore", "domain": "evil.example"},
                    ]
                )
            )
            # Unsafe 0644 rejected
            os.chmod(cookie_file, 0o644)
            with self.assertRaises(PrivateFileError):
                load_tl_cookies(cookie_file)

            # Owner-only 0600 accepted and filters foreign domain
            os.chmod(cookie_file, 0o600)
            loaded = load_tl_cookies(cookie_file)
            self.assertEqual(loaded, {"tluid": "123"})

            # Symlink cookie rejected
            sym_cookie = tmpdir / "sym-cookies.json"
            os.symlink(cookie_file, sym_cookie)
            with self.assertRaises(PrivateFileError):
                load_tl_cookies(sym_cookie)

            # Oversized cookie (>16KB) rejected
            big_cookie = tmpdir / "big-cookies.json"
            big_cookie.write_bytes(b"{" + b" " * 17000 + b"}")
            os.chmod(big_cookie, 0o600)
            with self.assertRaises(PrivateFileError):
                load_tl_cookies(big_cookie)

            # Login HTML on HTTP 200 -> login_required (never listing_id_missing)
            login_html = '<html><head><title>Login</title></head><body><form action="/user/account/login"><input type="password" name="pw"></form></body></html>'
            res_login = parse_tl_listing_html(login_html, exact_release_name="Example.2024")
            self.assertEqual(res_login.outcome, "login_required")

            # Verified heading stripping ONLY span.new.label.labelfreeleech + section-aware MediaInfo
            rel_name = "True.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES"
            valid_html = f"""
            <html><body>
              <h1 id="torrentnameid">  {rel_name} <span class="new label labelfreeleech">FREELEECH</span></h1>
              <div id="torrentName">{rel_name}</div>
              <a href="https://www.imdb.com/title/tt2024999/">IMDb</a>
              <div id="description">Catalogue runtime: 999 min. See https://www.torrentleech.org/download?passkey=secret</div>
              <pre id="nfo">
General
Duration : 2 h 4 min
Language : Spanish

Video
Duration : 2 h 4 min

Audio #1
Language : English

Audio #2
Language : Japanese (Original)

Text #1
Language : French

Text #2
Language : German
              </pre>
            </body></html>
            """
            res_valid = parse_tl_listing_html(valid_html, exact_release_name=rel_name)
            self.assertEqual(res_valid.outcome, "listing_id_found")
            self.assertEqual(res_valid.imdb_id, "tt2024999")
            self.assertTrue(res_valid.freeleech_badge)
            self.assertEqual(res_valid.file_runtime_minutes, 124)
            self.assertEqual(res_valid.audio_languages, ("English", "Japanese"))
            self.assertNotIn("secret", res_valid.sanitized_description or "")

            # Arbitrary non-freeleech span in heading MUST NOT be stripped -> listing_mismatch
            bad_span_html = f'<h1 id="torrentnameid">{rel_name} <span class="other">EXTRA</span></h1><a href="https://www.imdb.com/title/tt2024999/">IMDb</a>'
            res_bad_span = parse_tl_listing_html(bad_span_html, exact_release_name=rel_name)
            self.assertEqual(res_bad_span.outcome, "listing_mismatch")
            self.assertIsNone(res_bad_span.imdb_id)

            # Second mismatched heading fails ALL-heading rule
            two_headings_html = f'<h1 id="torrentnameid">{rel_name}</h1><div id="torrentName">Other.Release.2024</div>'
            self.assertEqual(
                parse_tl_listing_html(two_headings_html, exact_release_name=rel_name).outcome,
                "listing_mismatch",
            )

            # Distinct IMDb outcomes: missing, malformed, conflict
            self.assertEqual(
                parse_tl_listing_html(f'<h1 id="torrentnameid">{rel_name}</h1>', exact_release_name=rel_name).outcome,
                "listing_id_missing",
            )
            self.assertEqual(
                parse_tl_listing_html(
                    f'<h1 id="torrentnameid">{rel_name}</h1><a href="https://www.imdb.com/title/tt123/">bad</a>',
                    exact_release_name=rel_name,
                ).outcome,
                "listing_id_malformed",
            )
            self.assertEqual(
                parse_tl_listing_html(
                    f'<h1 id="torrentnameid">{rel_name}</h1>'
                    '<a href="https://www.imdb.com/title/tt1111111/">1</a>'
                    '<a href="https://www.imdb.com/title/tt2222222/">2</a>',
                    exact_release_name=rel_name,
                ).outcome,
                "listing_id_conflict",
            )

            # HTTP transport tests: redirect disabled, 429 Retry-After persistent cooldown, >1MB body cap
            db = IntakeDB(tmpdir / "intake.db")
            db.init_db()

            call_counter = {"n": 0}

            def _mock_transport(request: httpx.Request) -> httpx.Response:
                call_counter["n"] += 1
                if request.url.path.endswith("/111"):
                    return httpx.Response(302, headers={"Location": "/user/account/login"})
                if request.url.path.endswith("/222"):
                    return httpx.Response(200, content=b"x" * 1_000_050)
                if request.url.path.endswith("/333"):
                    return httpx.Response(429, headers={"Retry-After": "120"})
                return httpx.Response(200, text=valid_html)

            async def _run_http_checks() -> None:
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(_mock_transport), follow_redirects=False
                ) as http_client:
                    scraper = TorrentLeechScraper(
                        db=db,
                        cookie_file=cookie_file,
                        enabled=True,
                        min_interval_seconds=0.0,
                        http_client=http_client,
                    )
                    r_redir = await scraper.fetch_listing(
                        indexer_norm="torrentleech", external_id="111", exact_release_name=rel_name
                    )
                    self.assertEqual(r_redir.outcome, "login_required")

                    r_big = await scraper.fetch_listing(
                        indexer_norm="torrentleech", external_id="222", exact_release_name=rel_name
                    )
                    self.assertEqual(r_big.outcome, "listing_temporary_http")

                    r_429 = await scraper.fetch_listing(
                        indexer_norm="torrentleech", external_id="333", exact_release_name=rel_name
                    )
                    self.assertEqual(r_429.outcome, "listing_temporary_http")
                    self.assertTrue(db.is_tl_cooldown_active())

                    # New scraper instance (simulating restart) honors persisted cooldown without network call!
                    before = call_counter["n"]
                    scraper2 = TorrentLeechScraper(
                        db=db,
                        cookie_file=cookie_file,
                        enabled=True,
                        min_interval_seconds=0.0,
                        http_client=http_client,
                    )
                    r_cool = await scraper2.fetch_listing(
                        indexer_norm="torrentleech", external_id="444", exact_release_name=rel_name
                    )
                    self.assertEqual(r_cool.outcome, "cooldown_active")
                    self.assertEqual(call_counter["n"], before)

            asyncio.run(_run_http_checks())

    def test_shadow_case_1_duplicate_burst_restart_late_noncollapse_and_correction(self) -> None:
        """Mandatory Shadow Case 1 (1a-1e) from docs/ARCHITECTURE.md §8."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024001,
                title="Example Film",
                original_title="Example Film",
                year=2024,
                imdb_id="tt2024001",
                runtime_minutes=120,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024001: movie}, {"tt2024001": [2024001]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda ext_id, _name: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024001",
                    file_runtime_minutes=120,
                    audio_languages=("English",),
                    freeleech_badge=False,
                ),
            )
            triage = StubTriageClient(
                lambda _body: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Example Film", year=2024, imdb_id="tt2024001", tmdb_id=2024001
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))

            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 1a: 3 identical burst deliveries -> 3 archive files, 3 occurrences, 1 release, 1 card
                payload = _make_autobrr_movie_webhook()
                r1 = client.post("/webhook/test-webhook-secret", json=payload)
                r2 = client.post("/webhook/test-webhook-secret", json=payload)
                r3 = client.post("/webhook/test-webhook-secret", json=payload)
                self.assertEqual(r1.json()["status"], "accepted")
                self.assertEqual(r2.json()["status"], "coalesced_duplicate")
                self.assertEqual(r3.json()["status"], "coalesced_duplicate")
                rel_id = r1.json()["release_id"]
                self.assertEqual(r2.json()["release_id"], rel_id)
                self.assertEqual(r3.json()["release_id"], rel_id)
                self.assertEqual(len(db.list_occurrences_for_release(rel_id)), 3)

                # Process queue once
                asyncio.run(app.state.worker.process_next_queued())
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(tl.calls), 1)
                self.assertEqual(len(triage.calls), 1)
                self.assertEqual(len(actions.card_calls), 1)
                self.assertEqual(len(actions.eval_reply_calls), 1)
                self.assertEqual(actions.card_calls[0]["delivery_kind"], "actionable_grab")

                # 1c: Late duplicate after completion -> archived as 4th occurrence, zero re-work
                r4 = client.post("/webhook/test-webhook-secret", json=payload)
                self.assertEqual(r4.json()["status"], "coalesced_duplicate")
                self.assertEqual(len(db.list_occurrences_for_release(rel_id)), 4)
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.card_calls), 1)

                # 1d: Non-collapse across different encode/ID and different tracker for SAME movie/IMDb
                encode_1080 = _make_autobrr_movie_webhook(
                    release_name="Example.Film.2024.1080p.BluRay.REMUX.AVC-FraMeSToR",
                    external_id="9001002",
                )
                other_tracker = _make_autobrr_movie_webhook(
                    indexer="OtherTracker",
                    external_id="555",
                )
                r_1080 = client.post("/webhook/test-webhook-secret", json=encode_1080)
                r_other = client.post("/webhook/test-webhook-secret", json=other_tracker)
                self.assertEqual(len({rel_id, r_1080.json()["release_id"], r_other.json()["release_id"]}), 3)

                # 1b: Mid-flight worker crash & restart lease recovery (no duplicate release/card)
                rel_1080 = r_1080.json()["release_id"]
                with sqlite3.connect(str(cfg.intake_db_path)) as conn:
                    conn.execute(
                        "UPDATE releases SET state='enriching', lease_owner='dead-worker', lease_expires_at='2020-01-01T00:00:00+00:00' WHERE release_id=?",
                        (rel_1080,),
                    )
                fresh_worker = IntakeWorker(
                    config=cfg,
                    db=db,
                    tl_scraper=tl,
                    tmdb_client=tmdb,
                    triage_client=triage,
                    actions_client=actions,
                    worker_id="recovered-worker",
                )
                recovered = db.recover_orphaned_leases(
                    active_worker_id="recovered-worker", recover_dead_owners=True
                )
                self.assertEqual(recovered, 1)
                while asyncio.run(fresh_worker.process_next_queued()):
                    pass
                self.assertEqual(db.get_release(rel_1080)["state"], "completed")  # type: ignore[index]

                # 1e: Verified correction after initial evidence_review updates bound card (card_revision 1 -> 2)
                corr_name = "Correction.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES"
                corr_movie = TmdbMovieRecord(
                    tmdb_id=2024555,
                    title="Correction Film",
                    original_title="Correction Film",
                    year=2024,
                    imdb_id="tt2024555",
                    runtime_minutes=110,
                )
                tmdb._movies[2024555] = corr_movie
                tmdb._imdb_map["tt2024555"] = [2024555]
                tl_mode = {"fail": True}

                def _corr_tl(_ext: str, _name: str) -> TorrentLeechResult:
                    if tl_mode["fail"]:
                        return TorrentLeechResult(outcome="listing_temporary_http")
                    return TorrentLeechResult(
                        outcome="listing_id_found",
                        imdb_id="tt2024555",
                        file_runtime_minutes=110,
                        audio_languages=("English",),
                    )

                tl._handler = _corr_tl
                triage._handler = lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Correction Film",
                        year=2024,
                        imdb_id="tt2024555",
                        tmdb_id=2024555,
                        confidence="medium",
                    ),
                )
                w_initial = _make_autobrr_movie_webhook(
                    release_name=corr_name,
                    external_id="9005555",
                    title="Correction Film",
                    year=2024,
                    meta_imdb=None,
                )
                rc1 = client.post("/webhook/test-webhook-secret", json=w_initial)
                corr_rel_id = rc1.json()["release_id"]
                asyncio.run(app.state.worker.process_next_queued())
                rev1_card = [c for c in actions.card_calls if c["release_id"] == corr_rel_id][-1]
                self.assertEqual(rev1_card["card_revision"], 1)
                self.assertEqual(rev1_card["delivery_kind"], "evidence_review")

                # Newer delivery arrives with corrected meta_imdb and working TL
                tl_mode["fail"] = False
                w_corrected = _make_autobrr_movie_webhook(
                    release_name=corr_name,
                    external_id="9005555",
                    title="Correction Film",
                    year=2024,
                    meta_imdb="tt2024555",
                )
                rc2 = client.post("/webhook/test-webhook-secret", json=w_corrected)
                self.assertEqual(rc2.json()["status"], "updated_correction")
                asyncio.run(app.state.worker.process_next_queued())
                rev2_card = [c for c in actions.card_calls if c["release_id"] == corr_rel_id][-1]
                self.assertEqual(rev2_card["card_revision"], 2)
                self.assertEqual(rev2_card["delivery_kind"], "actionable_grab")
                self.assertEqual(rev2_card["verified_tmdb_id"], 2024555)
                self.assertTrue(triage.calls[-1]["force_refresh"])

    def test_shadow_case_2_wrong_tracker_imdb_forces_evidence_review(self) -> None:
        """Mandatory Shadow Case 2: poisoned tracker IMDb rejected, never converted to Grab Anyway."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            wrong_2018 = TmdbMovieRecord(
                tmdb_id=99999,
                title="Unrelated Old Film",
                original_title="Unrelated Old Film",
                year=2018,
                imdb_id="tt0099999",
                runtime_minutes=89,
            )
            true_2024 = TmdbMovieRecord(
                tmdb_id=2024999,
                title="True Film",
                original_title="True Film",
                year=2024,
                imdb_id="tt2024999",
                runtime_minutes=124,
            )
            tmdb = StubTmdbClient(
                cfg.tmdb_cache_path,
                {99999: wrong_2018, 2024999: true_2024},
                {"tt0099999": [99999], "tt2024999": [2024999]},
            )
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _ext, _name: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt0099999",
                    file_runtime_minutes=124,
                    audio_languages=("English",),
                    freeleech_badge=False,
                    sanitized_nfo="General Duration: 2h 4min",
                ),
            )
            triage = StubTriageClient(
                lambda _body: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="True Film", year=2024, imdb_id="tt2024999", tmdb_id=2024999
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            alt_policy = AlternativeGrabPolicy(
                enabled=True, allowed_rejection_classes=frozenset({"cutoff_met"})
            )

            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                alt_grab_policy=alt_policy,
                start_worker=False,
            )
            with TestClient(app) as client:
                webhook = _make_autobrr_movie_webhook(
                    release_name="True.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="9002001",
                    title="True Film",
                    year=2024,
                    meta_imdb="tt0099999",
                    action_rejected=True,
                    rejections=["Quality cutoff already met"],
                )
                r = client.post("/webhook/test-webhook-secret", json=webhook)
                self.assertEqual(r.status_code, 200)
                asyncio.run(app.state.worker.process_next_queued())

            # Evaluator received force_refresh=True, MediaInfo 124m, audio ["English"], and conflict note
            self.assertEqual(len(triage.calls), 1)
            self.assertTrue(triage.calls[0]["force_refresh"])
            self.assertEqual(triage.calls[0]["torrent_info"]["runtime_minutes"], 124)
            self.assertEqual(triage.calls[0]["torrent_info"]["audio_languages"], ["English"])
            self.assertEqual(triage.calls[0]["torrent_info"]["freeleech"], False)
            self.assertIn("extra_context", triage.calls[0])

            # Identity conflict MUST render evidence_review ONLY (never actionable_grab or alternative_grab!)
            self.assertEqual(len(actions.card_calls), 1)
            card = actions.card_calls[0]
            self.assertEqual(card["delivery_kind"], "evidence_review")
            self.assertEqual(card["identity_gate_status"], "evidence_review")
            self.assertNotIn("verified_tmdb_id", card)
            self.assertEqual(len(actions.eval_reply_calls), 1)
            self.assertEqual(actions.eval_reply_calls[0]["ack_message_id"], 501)

    def test_shadow_case_3_unidentified_movie_gate_pass_and_negative_controls(self) -> None:
        """Mandatory Shadow Case 3 (3a pass, 3b failed TL control, 3c runtime/confidence failure)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            gem = TmdbMovieRecord(
                tmdb_id=3025001,
                title="Festival Gem",
                original_title="Festival Gem",
                year=2025,
                imdb_id="tt3025001",
                runtime_minutes=114,
            )
            long_wrong = TmdbMovieRecord(
                tmdb_id=3025002,
                title="Festival Gem",
                original_title="Festival Gem",
                year=2025,
                imdb_id="tt3025002",
                runtime_minutes=168,
            )
            tmdb = StubTmdbClient(
                cfg.tmdb_cache_path,
                {3025001: gem, 3025002: long_wrong},
                {"tt3025001": [3025001], "tt3025002": [3025002]},
            )
            tl_outcomes: dict[str, TorrentLeechResult] = {
                "3001": TorrentLeechResult(
                    outcome="listing_id_missing",
                    file_runtime_minutes=112,
                    audio_languages=("French",),
                ),
                "3002": TorrentLeechResult(outcome="login_required"),
                "3003": TorrentLeechResult(
                    outcome="listing_id_missing",
                    file_runtime_minutes=112,
                    audio_languages=("French",),
                ),
            }
            tl = StubTlScraper(db, cfg.tl_cookie_file, lambda ext, _n: tl_outcomes[ext])

            eval_map = {
                "3001": _make_eval_payload(
                    title="Festival Gem", year=2025, imdb_id="tt3025001", tmdb_id=3025001, confidence="high"
                ),
                "3002": _make_eval_payload(
                    title="Festival Gem", year=2025, imdb_id="tt3025001", tmdb_id=3025001, confidence="high"
                ),
                "3003": _make_eval_payload(
                    title="Festival Gem", year=2025, imdb_id="tt3025002", tmdb_id=3025002, confidence="high"
                ),
            }
            eval_idx = {"key": "3001"}
            triage = StubTriageClient(lambda _b: TriageEvalResult(ok=True, payload=eval_map[eval_idx["key"]]))
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 3a: Unidentified (listing_id_missing) + high-conf evaluator + all checks pass -> actionable_grab
                eval_idx["key"] = "3001"
                w_3a = _make_autobrr_movie_webhook(
                    release_name="Festival.Gem.2025.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                    external_id="3001",
                    title="Festival Gem",
                    year=2025,
                    meta_imdb=None,
                )
                client.post("/webhook/test-webhook-secret", json=w_3a)
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "actionable_grab")
                self.assertEqual(actions.card_calls[-1]["verified_tmdb_id"], 3025001)

                # 3b: Failed TL lookup (login_required) CANNOT authorize title-only guess -> evidence_review
                eval_idx["key"] = "3002"
                w_3b = _make_autobrr_movie_webhook(
                    release_name="Festival.Gem.2025.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                    external_id="3002",
                    title="Festival Gem",
                    year=2025,
                    meta_imdb=None,
                )
                client.post("/webhook/test-webhook-secret", json=w_3b)
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertNotIn("verified_tmdb_id", actions.card_calls[-1])

                # 3c: MediaInfo runtime mismatch (112m vs 168m) fails independent gate -> evidence_review
                eval_idx["key"] = "3003"
                w_3c = _make_autobrr_movie_webhook(
                    release_name="Festival.Gem.2025.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                    external_id="3003",
                    title="Festival Gem",
                    year=2025,
                    meta_imdb=None,
                )
                client.post("/webhook/test-webhook-secret", json=w_3c)
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertIn("runtime", (actions.card_calls[-1]["conflict_reason"] or "").lower())

    def test_shadow_case_4_radarr_suppression_alternative_grab_and_ambiguous_delivery(self) -> None:
        """Mandatory Shadow Case 4 (4a-4d) + Alternative Grab + quality suppression + ambiguous_delivery."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=4024001,
                title="Library Film",
                original_title="Library Film",
                year=2024,
                imdb_id="tt4024001",
                runtime_minutes=110,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {4024001: movie}, {"tt4024001": [4024001]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _ext, _n: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt4024001",
                    file_runtime_minutes=110,
                    audio_languages=("English",),
                ),
            )
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Library Film", year=2024, imdb_id="tt4024001", tmdb_id=4024001
                    ),
                )
            )

            radarr_next = {"resp": RadarrStatusResponse(status="watched_unmonitored", movie_id=77)}
            card_override: dict[str, Optional[DeliveryCardResponse]] = {"resp": None}

            def _card_handler(payload: dict[str, Any]) -> DeliveryCardResponse:
                if card_override["resp"] is not None:
                    return card_override["resp"]
                return DeliveryCardResponse(
                    status="delivered",
                    ack_chat_id="-100123",
                    ack_message_id=900 + len(actions.card_calls),
                    ack_thread_id=10,
                )

            actions = StubActionsClient(
                radarr_handler=lambda _t, _i: radarr_next["resp"],
                card_handler=_card_handler,
            )
            alt_policy = AlternativeGrabPolicy(
                enabled=True, allowed_rejection_classes=frozenset({"cutoff_met"})
            )
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                alt_grab_policy=alt_policy,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 4a: Watched + unmonitored -> suppressed BEFORE delivery, 0 Telegram sends, 0 evaluator calls
                r_4a = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX-GRP",
                        external_id="4001",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                rel_4a = db.get_release(r_4a.json()["release_id"])
                assert rel_4a is not None
                self.assertEqual(rel_4a["state"], "suppressed")
                self.assertEqual(rel_4a["suppression_reason"], "watched_unmonitored")
                self.assertEqual(len(actions.card_calls), 0)
                self.assertEqual(len(triage.calls), 0)

                # 4b: Monitored, hasFile=false -> NOT suppressed, labeled explicitly
                radarr_next["resp"] = RadarrStatusResponse(status="monitored_no_file", movie_id=77)
                client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX-GRP",
                        external_id="4002",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "actionable_grab")
                self.assertIn("Monitored in Radarr", actions.card_calls[-1]["radarr_note"])

                # 4c: Already owned (no Alternative Grab trigger) -> suppressed before delivery
                radarr_next["resp"] = RadarrStatusResponse(status="owned_file", movie_id=77, file_id=888)
                cards_before = len(actions.card_calls)
                r_4c = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX-GRP",
                        external_id="4003",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(db.get_release(r_4c.json()["release_id"])["suppression_reason"], "already_owned")  # type: ignore[index]
                self.assertEqual(len(actions.card_calls), cards_before)

                # 4c-alt: Already owned + allowed quality-only cutoff_met -> alternative_grab bound to exact movie/file
                client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                        external_id="4004",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                        action_rejected=True,
                        rejections=["Quality cutoff already met"],
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                alt_card = actions.card_calls[-1]
                self.assertEqual(alt_card["delivery_kind"], "alternative_grab")
                self.assertEqual(alt_card["bound_existing_movie_id"], 77)
                self.assertEqual(alt_card["bound_existing_file_id"], 888)
                self.assertEqual(alt_card["bound_rejection_class"], "cutoff_met")

                # Routine disallowed quality rejection -> suppressed early (0 TL calls, 0 cards)
                tl_before = len(tl.calls)
                r_qrej = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.1080p.WEB-DL-GRP",
                        external_id="4005",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                        action_rejected=True,
                        rejections=["Quality is not wanted in profile"],
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(db.get_release(r_qrej.json()["release_id"])["suppression_reason"], "quality_rejected")  # type: ignore[index]
                self.assertEqual(len(tl.calls), tl_before)

                # 4d: Failed Radarr lookup -> does NOT establish absence -> evidence_review
                radarr_next["resp"] = RadarrStatusResponse(status="lookup_failed", detail="http_503")
                client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX-GRP",
                        external_id="4006",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertIn("unverified", actions.card_calls[-1]["radarr_note"].lower())

                # Ambiguous delivery lock -> no evaluation reply sent, never reopened on later webhook
                radarr_next["resp"] = RadarrStatusResponse(status="absent")
                card_override["resp"] = DeliveryCardResponse(status="ambiguous_delivery", detail="timeout")
                replies_before = len(actions.eval_reply_calls)
                r_amb = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX-GRP",
                        external_id="4007",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                amb_id = r_amb.json()["release_id"]
                self.assertEqual(db.get_release(amb_id)["state"], "ambiguous_delivery")  # type: ignore[index]
                self.assertEqual(len(actions.eval_reply_calls), replies_before)

                # Even a modified webhook for the same release does NOT reopen ambiguous_delivery
                r_amb2 = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX-GRP",
                        external_id="4007",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                        freeleech=True,
                    ),
                )
                self.assertEqual(r_amb2.json()["status"], "coalesced_duplicate")
                self.assertEqual(db.get_release(amb_id)["state"], "ambiguous_delivery")  # type: ignore[index]

    def test_concurrent_burst_coalescing_imdb_field_variants_fast_path_conflict_and_shadow_replay(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        # 1) #imdb with rating link text resolves cleanly; N/A -> missing
        rel = "Edge.Case.2024.2160p.UHD.BluRay.REMUX-GRP"
        html_with_rating = f'<h1 id="torrentnameid">{rel}</h1><div id="imdb"><a href="https://www.imdb.com/title/tt2024888/">7.8 / 10 (IMDb)</a></div>'
        r_rating = parse_tl_listing_html(html_with_rating, exact_release_name=rel)
        self.assertEqual(r_rating.outcome, "listing_id_found")
        self.assertEqual(r_rating.imdb_id, "tt2024888")

        html_na = f'<h1 id="torrentnameid">{rel}</h1><div id="imdb">N/A</div>'
        self.assertEqual(parse_tl_listing_html(html_na, exact_release_name=rel).outcome, "listing_id_missing")

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir, shadow_replay_enabled=True)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            # 2) True concurrent multi-threaded SQLite BEGIN IMMEDIATE burst (6 simultaneous arrivals)
            payload = _make_autobrr_movie_webhook(release_name=rel, external_id="777001", meta_imdb="tt2024888")
            raw_bytes = json.dumps(payload).encode("utf-8")
            classified = classify_webhook_payload(payload)

            def _submit(i: int) -> Any:
                occ_id = f"occ-burst-{i}"
                receipt = archive_raw_webhook(cfg.webhook_archive_dir, occ_id, raw_bytes)
                return db.record_movie_occurrence(
                    occurrence_id=occ_id,
                    received_at_iso=utc_now_iso(),
                    receipt=receipt,
                    classified=classified,
                )

            with ThreadPoolExecutor(max_workers=6) as pool:
                claims = list(pool.map(_submit, range(6)))

            statuses = [c.status for c in claims]
            self.assertEqual(statuses.count("accepted"), 1)
            self.assertEqual(statuses.count("coalesced_duplicate"), 5)
            self.assertEqual(len({c.release_id for c in claims}), 1)
            self.assertEqual(len(db.list_all_occurrences()), 6)
            self.assertEqual(len(db.list_all_releases()), 1)

            # 3) Fast-path evaluator contradiction supersedes card to evidence_review (rev 1 -> 2)
            # and /internal/replay executes cleanly in shadow mode
            movie = TmdbMovieRecord(
                tmdb_id=2024888,
                title="Edge Case",
                original_title="Edge Case",
                year=2024,
                imdb_id="tt2024888",
                runtime_minutes=105,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024888: movie}, {"tt2024888": [2024888]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _e, _n: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024888",
                    file_runtime_minutes=105,
                    audio_languages=("English",),
                ),
            )
            # Evaluator returns contradicting IMDb ID tt9999888
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Different Film", year=2024, imdb_id="tt9999888", tmdb_id=9999888
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )
            with TestClient(app) as client:
                # Drain the burst release
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(len(actions.card_calls), 2)
                self.assertEqual(actions.card_calls[0]["card_revision"], 1)
                self.assertEqual(actions.card_calls[0]["delivery_kind"], "actionable_grab")
                self.assertEqual(actions.card_calls[1]["card_revision"], 2)
                self.assertEqual(actions.card_calls[1]["delivery_kind"], "evidence_review")

                # Shadow /internal/replay works with X-Internal-Token when SHADOW_REPLAY_ENABLED=True
                r_rep = client.post(
                    "/internal/replay",
                    json=_make_autobrr_movie_webhook(
                        release_name="Edge.Case.2024.1080p.BluRay.REMUX-GRP",
                        external_id="777002",
                        meta_imdb="tt2024888",
                    ),
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r_rep.status_code, 200)
                self.assertEqual(r_rep.json()["ingest"]["status"], "accepted")
                self.assertEqual(r_rep.json()["release"]["state"], "completed")

    def test_shadow_replay_isolation_default_stubs_and_prod_state_guards(self) -> None:
        """Codex P1 #1: SHADOW_REPLAY_ENABLED=true enforces isolated state and stubbed writers by default."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            prod_state = Path.home() / ".local" / "state" / "movie-intel"
            prod_cfg = Path.home() / ".config" / "movie-intel"

            # 1) Default create_app with isolated paths auto-wires zero-mutation shadow stubs
            cfg_isolated = _make_config(
                tmpdir,
                shadow_replay_enabled=True,
                tl_metadata_enabled=False,
                tmdb_api_token="",
            )
            app = create_app(cfg_isolated, start_worker=False)
            self.assertIsInstance(app.state.triage_client, ShadowStubTriageClient)
            self.assertIsInstance(app.state.actions_client, ShadowStubActionsClient)
            with TestClient(app) as client:
                r = client.post(
                    "/internal/replay",
                    json=_make_autobrr_movie_webhook(
                        release_name="Shadow.Safe.2025.2160p.UHD.BluRay.REMUX-GRP",
                        external_id="808001",
                    ),
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["ingest"]["status"], "accepted")
                self.assertEqual(r.json()["release"]["state"], "completed")
                self.assertEqual(len(app.state.triage_client.calls), 1)
                self.assertEqual(len(app.state.actions_client.card_calls), 1)
                self.assertEqual(len(app.state.actions_client.eval_reply_calls), 1)

            # 2) Any state path inside default production ~/.local/state/movie-intel fails fast
            for field_name, bad_path in (
                ("intake_db_path", prod_state / "intake.db"),
                ("webhook_archive_dir", prod_state / "webhook-archive"),
                ("tmdb_cache_path", prod_state / "tmdb-cache.json"),
            ):
                with self.assertRaises(ValueError):
                    create_app(
                        _make_config(
                            tmpdir,
                            shadow_replay_enabled=True,
                            tl_metadata_enabled=False,
                            **{field_name: bad_path},
                        ),
                        start_worker=False,
                    )

            # 3) Production TL cookie path rejected when TL scraping is enabled without a stub
            with self.assertRaises(ValueError):
                create_app(
                    _make_config(
                        tmpdir,
                        shadow_replay_enabled=True,
                        tl_metadata_enabled=True,
                        tl_cookie_file=prod_cfg / "torrentleech-cookies-pr42.json",
                    ),
                    start_worker=False,
                )

            # 4) Explicit real non-stub writers are refused in shadow mode
            with self.assertRaises(ValueError):
                create_app(
                    cfg_isolated,
                    triage_client=TriageApiClient(base_url="http://127.0.0.1:40716", internal_token="tok"),
                    start_worker=False,
                )
            with self.assertRaises(ValueError):
                create_app(
                    cfg_isolated,
                    actions_client=ActionsApiClient(base_url="http://127.0.0.1:40721", internal_token="tok"),
                    start_worker=False,
                )

    def test_torrentleech_missing_external_id_fails_verification_mandatory(self) -> None:
        """Codex P1 #2: TorrentLeech webhook missing external_id must fail Check 1 and yield evidence_review."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir, tl_metadata_enabled=True)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2025111,
                title="Missing Id Film",
                original_title="Missing Id Film",
                year=2025,
                imdb_id="tt2025111",
                runtime_minutes=115,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2025111: movie}, {"tt2025111": [2025111]})
            # Use the REAL TorrentLeechScraper (no network call expected when external_id is empty)
            real_tl_scraper = TorrentLeechScraper(
                db=db,
                cookie_file=cfg.tl_cookie_file,
                enabled=True,
                min_interval_seconds=0.0,
            )
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Missing Id Film",
                        year=2025,
                        imdb_id="tt2025111",
                        tmdb_id=2025111,
                        confidence="high",
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))

            app = create_app(
                cfg,
                db=db,
                tl_scraper=real_tl_scraper,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )
            with TestClient(app) as client:
                webhook = _make_autobrr_movie_webhook(
                    release_name="Missing.Id.Film.2025.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    indexer="TorrentLeech",
                    external_id="",
                    title="Missing Id Film",
                    year=2025,
                    meta_imdb="tt2025111",
                )
                r = client.post("/webhook/test-webhook-secret", json=webhook)
                self.assertEqual(r.status_code, 200)
                rel_id = r.json()["release_id"]
                asyncio.run(app.state.worker.process_next_queued())

            rel = db.get_release(rel_id)
            assert rel is not None
            self.assertEqual(rel["state"], "completed")
            self.assertEqual(rel["tl_outcome"], "listing_mismatch")
            self.assertEqual(rel["identity_gate_status"], "evidence_review")
            self.assertIsNone(rel["verified_tmdb_id"])
            self.assertEqual(len(actions.card_calls), 1)
            card = actions.card_calls[0]
            self.assertEqual(card["delivery_kind"], "evidence_review")
            self.assertNotIn("verified_tmdb_id", card)

    def test_failed_conflict_supersession_persists_pending_and_retries_independently(self) -> None:
        """Codex P1 #3: Failed fast-path conflict supersession must stay queued and retry before completing."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024777,
                title="Conflict Retry Film",
                original_title="Conflict Retry Film",
                year=2024,
                imdb_id="tt2024777",
                runtime_minutes=108,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024777: movie}, {"tt2024777": [2024777]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _e, _n: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024777",
                    file_runtime_minutes=108,
                    audio_languages=("English",),
                ),
            )
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Different Film",
                        year=2024,
                        imdb_id="tt9999777",
                        tmdb_id=9999777,
                    ),
                )
            )

            fail_supersede_once = {"fail": True}

            def _card_handler(payload: dict[str, Any]) -> DeliveryCardResponse:
                rev = int(payload["card_revision"])
                if rev == 1:
                    return DeliveryCardResponse(
                        status="delivered",
                        ack_chat_id="-100777",
                        ack_message_id=701,
                        ack_thread_id=12,
                    )
                if fail_supersede_once["fail"]:
                    fail_supersede_once["fail"] = False
                    return DeliveryCardResponse(status="failed", detail="actions_connect_error")
                return DeliveryCardResponse(
                    status="delivered",
                    ack_chat_id="-100777",
                    ack_message_id=701,
                    ack_thread_id=12,
                )

            actions = StubActionsClient(
                radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"),
                card_handler=_card_handler,
            )
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )
            with TestClient(app) as client:
                webhook = _make_autobrr_movie_webhook(
                    release_name="Conflict.Retry.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="700777",
                    title="Conflict Retry Film",
                    year=2024,
                    meta_imdb="tt2024777",
                )
                r1 = client.post("/webhook/test-webhook-secret", json=webhook)
                rel_id = r1.json()["release_id"]

                # First worker pass: rev 1 succeeds, evaluator contradicts, rev 2 supersession fails
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                mid_rel = db.get_release(rel_id)
                assert mid_rel is not None
                self.assertEqual(mid_rel["state"], "queued")
                self.assertEqual(mid_rel["card_revision"], 1)
                self.assertEqual(mid_rel["identity_gate_status"], "evidence_review")
                self.assertIsNone(mid_rel["verified_tmdb_id"])
                self.assertIsNotNone(mid_rel["pending_card_supersede_json"])
                self.assertIsNotNone(mid_rel["pending_eval_reply_json"])
                self.assertEqual(len(actions.eval_reply_calls), 0)

                # Duplicate webhook coalesces without dropping pending supersession
                r_dup = client.post("/webhook/test-webhook-secret", json=webhook)
                self.assertEqual(r_dup.json()["status"], "coalesced_duplicate")
                self.assertIsNotNone(db.get_release(rel_id)["pending_card_supersede_json"])  # type: ignore[index]

                # Second worker pass: retries ONLY supersession (rev 2) + bound reply; 0 extra TL/triage calls
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(tl.calls), 1)
                self.assertEqual(len(triage.calls), 1)
                self.assertEqual(len(actions.card_calls), 3)  # rev1, failed rev2, succeeded rev2
                self.assertEqual(actions.card_calls[-1]["card_revision"], 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertEqual(len(actions.eval_reply_calls), 1)
                self.assertEqual(actions.eval_reply_calls[0]["ack_message_id"], 701)
                self.assertEqual(actions.eval_reply_calls[0]["card_revision"], 2)

                done_rel = db.get_release(rel_id)
                assert done_rel is not None
                self.assertEqual(done_rel["state"], "completed")
                self.assertEqual(done_rel["card_revision"], 2)
                self.assertEqual(done_rel["identity_gate_status"], "evidence_review")
                self.assertEqual(done_rel["eval_reply_status"], "sent")
                self.assertIsNone(done_rel["pending_card_supersede_json"])
                self.assertIsNone(done_rel["pending_eval_reply_json"])

    def test_failed_eval_reply_persists_pending_and_retries_without_resending_card(self) -> None:
        """Codex P1 #4: Failed evaluation reply retries independently on both fast and unresolved paths."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024666,
                title="Reply Retry Film",
                original_title="Reply Retry Film",
                year=2024,
                imdb_id="tt2024666",
                runtime_minutes=116,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024666: movie}, {"tt2024666": [2024666]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda ext, _n: (
                    TorrentLeechResult(
                        outcome="listing_id_found",
                        imdb_id="tt2024666",
                        file_runtime_minutes=116,
                        audio_languages=("English",),
                    )
                    if ext == "6661"
                    else TorrentLeechResult(outcome="listing_temporary_http")
                ),
            )
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Reply Retry Film",
                        year=2024,
                        imdb_id="tt2024666",
                        tmdb_id=2024666,
                        confidence="medium",
                    ),
                )
            )

            reply_sequence: list[DeliveryEvalResponse] = [
                DeliveryEvalResponse(status="failed", detail="actions_connect_error"),
                DeliveryEvalResponse(status="sent"),
                DeliveryEvalResponse(status="failed", detail="actions_connect_error"),
                DeliveryEvalResponse(status="sent"),
                DeliveryEvalResponse(status="ambiguous_reply", detail="timeout"),
            ]
            actions = StubActionsClient(
                radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"),
                eval_reply_handler=lambda _p: reply_sequence.pop(0),
            )
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 1) Fast path: card succeeds, first eval reply fails -> stays queued, retries reply ONLY
                w_fast = _make_autobrr_movie_webhook(
                    release_name="Reply.Retry.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="6661",
                    title="Reply Retry Film",
                    year=2024,
                    meta_imdb="tt2024666",
                )
                r_fast = client.post("/webhook/test-webhook-secret", json=w_fast)
                fast_id = r_fast.json()["release_id"]

                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                mid_fast = db.get_release(fast_id)
                assert mid_fast is not None
                self.assertEqual(mid_fast["state"], "queued")
                self.assertEqual(mid_fast["card_revision"], 1)
                self.assertEqual(mid_fast["ack_message_id"], 501)
                self.assertEqual(mid_fast["eval_reply_status"], "pending")
                self.assertEqual(mid_fast["verified_tmdb_id"], 2024666)
                self.assertIsNotNone(mid_fast["pending_eval_reply_json"])

                # Duplicate webhook while reply is pending coalesces cleanly without losing pending reply
                self.assertEqual(
                    client.post("/webhook/test-webhook-secret", json=w_fast).json()["status"],
                    "coalesced_duplicate",
                )

                # Retry pass: 0 extra cards, 0 extra TL scrapes, 0 extra /evaluate calls
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.card_calls), 1)
                self.assertEqual(len(tl.calls), 1)
                self.assertEqual(len(triage.calls), 1)
                self.assertEqual(len(actions.eval_reply_calls), 2)
                self.assertEqual(actions.eval_reply_calls[-1]["ack_message_id"], 501)

                done_fast = db.get_release(fast_id)
                assert done_fast is not None
                self.assertEqual(done_fast["state"], "completed")
                self.assertEqual(done_fast["eval_reply_status"], "sent")
                self.assertIsNone(done_fast["pending_eval_reply_json"])
                self.assertEqual(done_fast["verified_tmdb_id"], 2024666)

                # 2) Unresolved path: card succeeds, first eval reply fails -> retries reply ONLY
                w_unres = _make_autobrr_movie_webhook(
                    release_name="Reply.Retry.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="6662",
                    title="Reply Retry Film",
                    year=2024,
                    meta_imdb=None,
                )
                r_unres = client.post("/webhook/test-webhook-secret", json=w_unres)
                unres_id = r_unres.json()["release_id"]

                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                mid_unres = db.get_release(unres_id)
                assert mid_unres is not None
                self.assertEqual(mid_unres["state"], "queued")
                self.assertEqual(mid_unres["eval_reply_status"], "pending")
                self.assertIsNotNone(mid_unres["pending_eval_reply_json"])
                cards_before = len(actions.card_calls)
                tl_before = len(tl.calls)
                triage_before = len(triage.calls)

                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.card_calls), cards_before)
                self.assertEqual(len(tl.calls), tl_before)
                self.assertEqual(len(triage.calls), triage_before)
                done_unres = db.get_release(unres_id)
                assert done_unres is not None
                self.assertEqual(done_unres["state"], "completed")
                self.assertEqual(done_unres["eval_reply_status"], "sent")
                self.assertIsNone(done_unres["pending_eval_reply_json"])

                # 3) Ambiguous reply: marks completed with eval_reply_status='ambiguous_reply' (no blind resend)
                w_amb = _make_autobrr_movie_webhook(
                    release_name="Reply.Retry.Film.2024.1080p.BluRay.REMUX-GRP2",
                    external_id="6663",
                    title="Reply Retry Film",
                    year=2024,
                    meta_imdb=None,
                )
                r_amb = client.post("/webhook/test-webhook-secret", json=w_amb)
                amb_id = r_amb.json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                done_amb = db.get_release(amb_id)
                assert done_amb is not None
                self.assertEqual(done_amb["state"], "completed")
                self.assertEqual(done_amb["eval_reply_status"], "ambiguous_reply")
                self.assertIsNone(done_amb["pending_eval_reply_json"])
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))

            # 4) Verify ActionsApiClient.deliver_evaluation HTTP/transport classification
            def _reply_transport(request: httpx.Request) -> httpx.Response:
                mode = json.loads(request.content.decode("utf-8")).get("mode")
                if mode == "ok":
                    return httpx.Response(200, json={"status": "sent"})
                if mode == "400":
                    return httpx.Response(400, json={"detail": "bad request"})
                if mode == "502":
                    return httpx.Response(502, text="bad gateway")
                if mode == "connect":
                    raise httpx.ConnectError("refused")
                raise httpx.ReadTimeout("timed out")

            async def _check_client() -> None:
                async with httpx.AsyncClient(transport=httpx.MockTransport(_reply_transport)) as hc:
                    ac = ActionsApiClient(
                        base_url="http://127.0.0.1:40721", internal_token="tok", http_client=hc
                    )
                    self.assertEqual((await ac.deliver_evaluation({"mode": "ok"})).status, "sent")
                    self.assertEqual((await ac.deliver_evaluation({"mode": "400"})).status, "failed")
                    self.assertEqual((await ac.deliver_evaluation({"mode": "connect"})).status, "failed")
                    self.assertEqual((await ac.deliver_evaluation({"mode": "502"})).status, "ambiguous_reply")
                    self.assertEqual((await ac.deliver_evaluation({"mode": "timeout"})).status, "ambiguous_reply")

            asyncio.run(_check_client())

    def test_genuine_tracker_conflict_supersedes_verified_card_while_transient_tl_preserves_it(self) -> None:
        """Codex Round 2 P1 #1: listing_id_conflict / listing_mismatch must supersede verified card; transient failures preserve it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024901,
                title="Verified Guard Film",
                original_title="Verified Guard Film",
                year=2024,
                imdb_id="tt2024901",
                runtime_minutes=112,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024901: movie}, {"tt2024901": [2024901]})

            tl_state = {
                "outcome": TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024901",
                    file_runtime_minutes=112,
                    audio_languages=("English",),
                )
            }
            tl = StubTlScraper(db, cfg.tl_cookie_file, lambda _e, _n: tl_state["outcome"])
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Verified Guard Film",
                        year=2024,
                        imdb_id="tt2024901",
                        tmdb_id=2024901,
                        confidence="high",
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 1) Initial verified release completes with actionable rev 1 card
                w1 = _make_autobrr_movie_webhook(
                    release_name="Verified.Guard.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="901001",
                    title="Verified Guard Film",
                    year=2024,
                    meta_imdb="tt2024901",
                    freeleech=False,
                )
                r1 = client.post("/webhook/test-webhook-secret", json=w1)
                rel_id = r1.json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.card_calls), 1)
                self.assertEqual(actions.card_calls[0]["delivery_kind"], "actionable_grab")
                self.assertEqual(actions.card_calls[0]["card_revision"], 1)

                # 2) Correction arrives while TL has a transient HTTP outage -> preserves verified rev 1 card
                tl_state["outcome"] = TorrentLeechResult(outcome="listing_temporary_http")
                w2_transient = _make_autobrr_movie_webhook(
                    release_name="Verified.Guard.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="901001",
                    title="Verified Guard Film",
                    year=2024,
                    meta_imdb="tt2024901",
                    freeleech=True,
                )
                self.assertEqual(
                    client.post("/webhook/test-webhook-secret", json=w2_transient).json()["status"],
                    "updated_correction",
                )
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                rel_after_transient = db.get_release(rel_id)
                assert rel_after_transient is not None
                self.assertEqual(rel_after_transient["state"], "completed")
                self.assertEqual(rel_after_transient["identity_gate_status"], "verified")
                self.assertEqual(rel_after_transient["verified_tmdb_id"], 2024901)
                self.assertEqual(rel_after_transient["card_revision"], 1)
                self.assertEqual(len(actions.card_calls), 1)

                # 3) Next correction hits a genuine TL conflict (listing_id_conflict) -> MUST supersede to evidence_review rev 2!
                tl_state["outcome"] = TorrentLeechResult(
                    outcome="listing_id_conflict",
                    diagnostic="conflicting_imdb_ids",
                )
                w3_conflict = _make_autobrr_movie_webhook(
                    release_name="Verified.Guard.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="901001",
                    title="Verified Guard Film",
                    year=2024,
                    meta_imdb="tt2024901",
                    freeleech=False,
                    size=69_000_000_000,
                )
                self.assertEqual(
                    client.post("/webhook/test-webhook-secret", json=w3_conflict).json()["status"],
                    "updated_correction",
                )
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                rel_after_conflict = db.get_release(rel_id)
                assert rel_after_conflict is not None
                self.assertEqual(rel_after_conflict["state"], "completed")
                self.assertEqual(rel_after_conflict["identity_gate_status"], "evidence_review")
                self.assertIsNone(rel_after_conflict["verified_tmdb_id"])
                self.assertEqual(rel_after_conflict["card_revision"], 2)
                self.assertEqual(len(actions.card_calls), 2)
                self.assertEqual(actions.card_calls[-1]["card_revision"], 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertNotIn("verified_tmdb_id", actions.card_calls[-1])
                self.assertTrue(triage.calls[-1]["force_refresh"])

                # 4) Verify listing_mismatch also supersedes a previously verified release
                tl_state["outcome"] = TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024901",
                    file_runtime_minutes=112,
                    audio_languages=("English",),
                )
                w_m1 = _make_autobrr_movie_webhook(
                    release_name="Verified.Guard.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="901002",
                    title="Verified Guard Film",
                    year=2024,
                    meta_imdb="tt2024901",
                )
                rel_m_id = client.post("/webhook/test-webhook-secret", json=w_m1).json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(rel_m_id)["identity_gate_status"], "verified")  # type: ignore[index]

                tl_state["outcome"] = TorrentLeechResult(outcome="listing_mismatch", diagnostic="heading_mismatch")
                w_m2 = _make_autobrr_movie_webhook(
                    release_name="Verified.Guard.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="901002",
                    title="Verified Guard Film",
                    year=2024,
                    meta_imdb="tt2024901",
                    freeleech=True,
                )
                client.post("/webhook/test-webhook-secret", json=w_m2)
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                rel_m_after = db.get_release(rel_m_id)
                assert rel_m_after is not None
                self.assertEqual(rel_m_after["identity_gate_status"], "evidence_review")
                self.assertIsNone(rel_m_after["verified_tmdb_id"])
                self.assertEqual(rel_m_after["card_revision"], 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")

    def test_coalesced_duplicate_preserves_usable_download_url_source_before_and_after_delivery(self) -> None:
        """Codex Round 2 P1 #2: Duplicate coalescing must preserve a usable download_url acquisition source."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024902,
                title="Download Source Film",
                original_title="Download Source Film",
                year=2024,
                imdb_id="tt2024902",
                runtime_minutes=104,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024902: movie}, {"tt2024902": [2024902]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _e, _n: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024902",
                    file_runtime_minutes=104,
                    audio_languages=("English",),
                ),
            )
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Download Source Film",
                        year=2024,
                        imdb_id="tt2024902",
                        tmdb_id=2024902,
                        confidence="high",
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # Subcase A: Delivery 1 lacks download_url; Delivery 2 supplies it BEFORE worker claims
                w_no_url = _make_autobrr_movie_webhook(
                    release_name="Download.Source.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="902001",
                    title="Download Source Film",
                    year=2024,
                    meta_imdb="tt2024902",
                    download_url="",
                )
                w_with_url = _make_autobrr_movie_webhook(
                    release_name="Download.Source.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="902001",
                    title="Download Source Film",
                    year=2024,
                    meta_imdb="tt2024902",
                    download_url="https://tracker.invalid/dl/902001/passkey=USABLE_KEY_A",
                )
                w_non_http = _make_autobrr_movie_webhook(
                    release_name="Download.Source.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="902001",
                    title="Download Source Film",
                    year=2024,
                    meta_imdb="tt2024902",
                    download_url="magnet:?xt=urn:btih:deadbeef",
                )
                r1 = client.post("/webhook/test-webhook-secret", json=w_no_url)
                r2 = client.post("/webhook/test-webhook-secret", json=w_with_url)
                r3 = client.post("/webhook/test-webhook-secret", json=w_non_http)
                rel_a = r1.json()["release_id"]
                occ_a2 = r2.json()["occurrence_id"]
                self.assertEqual(r1.json()["status"], "accepted")
                self.assertEqual(r2.json()["status"], "coalesced_duplicate")
                self.assertEqual(r3.json()["status"], "coalesced_duplicate")
                self.assertEqual(r2.json()["release_id"], rel_a)
                # Usable HTTP(S) source preserved and NOT overwritten by later non-HTTP/empty delivery
                self.assertEqual(db.get_release(rel_a)["source_occurrence_id"], occ_a2)  # type: ignore[index]

                # Worker runs ONCE and binds card directly to occ_a2
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.card_calls), 1)
                self.assertEqual(actions.card_calls[0]["source_occurrence_id"], occ_a2)
                r_push_a = client.post(
                    f"/internal/releases/{rel_a}/push-payload",
                    json={"source_occurrence_id": occ_a2},
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r_push_a.status_code, 200)
                self.assertEqual(
                    r_push_a.json()["push_payload"]["downloadUrl"],
                    "https://tracker.invalid/dl/902001/passkey=USABLE_KEY_A",
                )

                # Subcase B: Delivery 1 lacks URL and card is delivered BEFORE Delivery 2 arrives with URL
                w_b1 = _make_autobrr_movie_webhook(
                    release_name="Download.Source.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="902002",
                    title="Download Source Film",
                    year=2024,
                    meta_imdb="tt2024902",
                    download_url="",
                )
                w_b2 = _make_autobrr_movie_webhook(
                    release_name="Download.Source.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="902002",
                    title="Download Source Film",
                    year=2024,
                    meta_imdb="tt2024902",
                    download_url="https://tracker.invalid/dl/902002/passkey=LATE_USABLE_KEY_B",
                )
                rb1 = client.post("/webhook/test-webhook-secret", json=w_b1)
                rel_b = rb1.json()["release_id"]
                occ_b1 = rb1.json()["occurrence_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(actions.card_calls[-1]["source_occurrence_id"], occ_b1)

                # Late duplicate supplies usable download_url without triggering extra evaluation/card
                rb2 = client.post("/webhook/test-webhook-secret", json=w_b2)
                occ_b2 = rb2.json()["occurrence_id"]
                self.assertEqual(rb2.json()["status"], "coalesced_duplicate")
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(rel_b)["source_occurrence_id"], occ_b2)  # type: ignore[index]

                # Confirming grab with the card's original bound occ_b1 resolves verified sibling occ_b2
                r_push_b = client.post(
                    f"/internal/releases/{rel_b}/push-payload",
                    json={"source_occurrence_id": occ_b1},
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r_push_b.status_code, 200)
                self.assertEqual(r_push_b.json()["source_occurrence_id"], occ_b2)
                self.assertEqual(
                    r_push_b.json()["push_payload"]["downloadUrl"],
                    "https://tracker.invalid/dl/902002/passkey=LATE_USABLE_KEY_B",
                )

                # Tampering with occ_b1's archive file MUST still fail closed with 409 (no bypass!)
                occ_b1_ref = db.get_occurrence_archive_ref(rel_b, occ_b1)
                assert occ_b1_ref is not None
                arch_b1 = cfg.webhook_archive_dir / occ_b1_ref.archive_relpath
                arch_b1.write_text('{"tampered": true}')
                os.chmod(arch_b1, 0o600)
                r_tamper = client.post(
                    f"/internal/releases/{rel_b}/push-payload",
                    json={"source_occurrence_id": occ_b1},
                    headers={"X-Internal-Token": "test-internal-secret"},
                )
                self.assertEqual(r_tamper.status_code, 409)

    def test_reverse_tmdb_lookup_outage_preserves_verified_card_while_disagreement_supersedes(self) -> None:
        """Codex Round 3 P1: Reverse TMDB /find outage (None) must NOT downgrade a verified card, while real disagreement must supersede."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024903,
                title="Reverse Check Film",
                original_title="Reverse Check Film",
                year=2024,
                imdb_id="tt2024903",
                runtime_minutes=118,
            )

            reverse_mode: dict[str, Optional[list[int]]] = {"result": [2024903]}

            class FaultyReverseTmdbClient(StubTmdbClient):
                async def find_movies_by_imdb_id(self, imdb_id: str) -> Optional[list[int]]:
                    return reverse_mode["result"]

            tmdb = FaultyReverseTmdbClient(cfg.tmdb_cache_path, {2024903: movie}, {"tt2024903": [2024903]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _e, _n: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024903",
                    file_runtime_minutes=118,
                    audio_languages=("English",),
                ),
            )
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Reverse Check Film",
                        year=2024,
                        imdb_id="tt2024903",
                        tmdb_id=2024903,
                        confidence="high",
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 1) Initial release verifies cleanly with actionable rev 1 card
                w1 = _make_autobrr_movie_webhook(
                    release_name="Reverse.Check.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="903001",
                    title="Reverse Check Film",
                    year=2024,
                    meta_imdb="tt2024903",
                    meta_tmdb=2024903,
                    freeleech=False,
                )
                rel_id = client.post("/webhook/test-webhook-secret", json=w1).json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.card_calls), 1)
                self.assertEqual(actions.card_calls[0]["delivery_kind"], "actionable_grab")
                self.assertEqual(db.get_release(rel_id)["identity_gate_status"], "verified")  # type: ignore[index]

                # 2) Correction arrives supplying meta_tmdb while reverse /find/{imdb_id} fails transiently (None)
                #    -> MUST preserve verified rev 1 card (0 supersession cards)
                reverse_mode["result"] = None
                w2_outage = _make_autobrr_movie_webhook(
                    release_name="Reverse.Check.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="903001",
                    title="Reverse Check Film",
                    year=2024,
                    meta_imdb="tt2024903",
                    meta_tmdb=2024903,
                    freeleech=True,
                )
                self.assertEqual(
                    client.post("/webhook/test-webhook-secret", json=w2_outage).json()["status"],
                    "updated_correction",
                )
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                rel_after_outage = db.get_release(rel_id)
                assert rel_after_outage is not None
                self.assertEqual(rel_after_outage["state"], "completed")
                self.assertEqual(rel_after_outage["identity_gate_status"], "verified")
                self.assertEqual(rel_after_outage["verified_tmdb_id"], 2024903)
                self.assertEqual(rel_after_outage["card_revision"], 1)
                self.assertEqual(len(actions.card_calls), 1)

                # 3) Correction arrives where reverse /find/{imdb_id} actually disagrees ([9999999])
                #    -> MUST supersede verified rev 1 card to evidence_review rev 2
                reverse_mode["result"] = [9999999]
                w3_disagree = _make_autobrr_movie_webhook(
                    release_name="Reverse.Check.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="903001",
                    title="Reverse Check Film",
                    year=2024,
                    meta_imdb="tt2024903",
                    meta_tmdb=2024903,
                    freeleech=False,
                    size=69_500_000_000,
                )
                client.post("/webhook/test-webhook-secret", json=w3_disagree)
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                rel_after_disagree = db.get_release(rel_id)
                assert rel_after_disagree is not None
                self.assertEqual(rel_after_disagree["state"], "completed")
                self.assertEqual(rel_after_disagree["identity_gate_status"], "evidence_review")
                self.assertIsNone(rel_after_disagree["verified_tmdb_id"])
                self.assertEqual(rel_after_disagree["card_revision"], 2)
                self.assertEqual(len(actions.card_calls), 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertEqual(actions.card_calls[-1]["card_revision"], 2)

    def test_known_identity_conflicts_not_masked_by_reverse_or_tl_outages(self) -> None:
        """Codex Round 4 P1: Known source-ID / movie conflicts must supersede verified cards even during reverse-lookup or TL outages."""
        from services.movie_intake_api.tmdb_verifier import run_independent_identity_gate

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024904,
                title="Conflict Mask Film",
                original_title="Conflict Mask Film",
                year=2024,
                imdb_id="tt2024904",
                runtime_minutes=118,
            )

            reverse_mode: dict[str, Optional[list[int]]] = {"result": [2024904]}

            class OutageReverseTmdbClient(StubTmdbClient):
                async def find_movies_by_imdb_id(self, imdb_id: str) -> Optional[list[int]]:
                    return reverse_mode["result"]

            tmdb = OutageReverseTmdbClient(cfg.tmdb_cache_path, {2024904: movie}, {"tt2024904": [2024904]})
            tl_outcome_box = {
                "res": TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024904",
                    file_runtime_minutes=118,
                    audio_languages=("English",),
                )
            }
            tl = StubTlScraper(db, cfg.tl_cookie_file, lambda _e, _n: tl_outcome_box["res"])
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Conflict Mask Film",
                        year=2024,
                        imdb_id="tt2024904",
                        tmdb_id=2024904,
                        confidence="high",
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 1) Exact Codex Round 4 repro: verified rev 1 -> correction changes meta_imdb to contradict
                #    TL IMDb while meta_tmdb still resolves to movie AND reverse /find fails transiently (None)
                w1 = _make_autobrr_movie_webhook(
                    release_name="Conflict.Mask.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="904001",
                    title="Conflict Mask Film",
                    year=2024,
                    meta_imdb="tt2024904",
                    meta_tmdb=2024904,
                )
                rel_id = client.post("/webhook/test-webhook-secret", json=w1).json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(rel_id)["identity_gate_status"], "verified")  # type: ignore[index]
                self.assertEqual(len(actions.card_calls), 1)

                reverse_mode["result"] = None
                w2_source_conflict = _make_autobrr_movie_webhook(
                    release_name="Conflict.Mask.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="904001",
                    title="Conflict Mask Film",
                    year=2024,
                    meta_imdb="tt9999904",  # Contradicts TL tt2024904!
                    meta_tmdb=2024904,
                )
                client.post("/webhook/test-webhook-secret", json=w2_source_conflict)
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                rel_after = db.get_release(rel_id)
                assert rel_after is not None
                self.assertEqual(rel_after["state"], "completed")
                self.assertEqual(rel_after["identity_gate_status"], "evidence_review")
                self.assertIsNone(rel_after["verified_tmdb_id"])
                self.assertEqual(rel_after["card_revision"], 2)
                self.assertEqual(len(actions.card_calls), 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertEqual(actions.card_calls[-1]["card_revision"], 2)

                # 2) Movie-evidence conflict (MediaInfo runtime mismatch) during reverse /find outage
                #    is STILL detected as is_genuine_conflict=True before reverse lookup runs
                gate_rt = asyncio.run(
                    run_independent_identity_gate(
                        tmdb_client=tmdb,
                        exact_release_name="Conflict.Mask.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                        proposed_imdb_id="tt2024904",
                        proposed_tmdb_id=2024904,
                        proposal_confidence="high",
                        from_evaluator=False,
                        tl_applicable=True,
                        tl_outcome="listing_id_found",
                        observed_meta_imdb="tt2024904",
                        observed_meta_tmdb=2024904,
                        observed_tl_imdb="tt2024904",
                        file_runtime_minutes=195,
                    )
                )
                self.assertEqual(gate_rt.status, "evidence_review")
                self.assertTrue(gate_rt.is_genuine_conflict)
                self.assertIn("mediainfo_runtime_mismatch", gate_rt.checks_failed)

                # 3) Correction contradicting previously verified IMDb during a transient TL outage
                #    MUST supersede verified rev 1 card to evidence_review rev 2
                reverse_mode["result"] = [2024904]
                w_tl1 = _make_autobrr_movie_webhook(
                    release_name="Conflict.Mask.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="904002",
                    title="Conflict Mask Film",
                    year=2024,
                    meta_imdb="tt2024904",
                    meta_tmdb=2024904,
                )
                rel_tl_id = client.post("/webhook/test-webhook-secret", json=w_tl1).json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(rel_tl_id)["identity_gate_status"], "verified")  # type: ignore[index]

                tl_outcome_box["res"] = TorrentLeechResult(outcome="listing_temporary_http")
                w_tl2 = _make_autobrr_movie_webhook(
                    release_name="Conflict.Mask.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="904002",
                    title="Conflict Mask Film",
                    year=2024,
                    meta_imdb="tt8888904",  # Contradicts previously verified tt2024904
                    meta_tmdb=2024904,
                )
                client.post("/webhook/test-webhook-secret", json=w_tl2)
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                rel_tl_after = db.get_release(rel_tl_id)
                assert rel_tl_after is not None
                self.assertEqual(rel_tl_after["identity_gate_status"], "evidence_review")
                self.assertIsNone(rel_tl_after["verified_tmdb_id"])
                self.assertEqual(rel_tl_after["card_revision"], 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")

    def test_pre_gate_runtime_conflict_preserved_when_evaluator_returns_low_confidence(self) -> None:
        """Codex Round 5 P1: Pre-gate runtime conflict must supersede verified card even if evaluator returns low confidence."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024905,
                title="Runtime Drift Film",
                original_title="Runtime Drift Film",
                year=2024,
                imdb_id="tt2024905",
                runtime_minutes=118,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024905: movie}, {"tt2024905": [2024905]})
            tl_runtime = {"minutes": 118}
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _e, _n: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024905",
                    file_runtime_minutes=tl_runtime["minutes"],
                    audio_languages=("English",),
                ),
            )
            eval_conf = {"level": "high"}
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Runtime Drift Film",
                        year=2024,
                        imdb_id="tt2024905",
                        tmdb_id=2024905,
                        confidence=eval_conf["level"],
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"))
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )

            with TestClient(app) as client:
                # 1) Initial 118-minute movie verifies cleanly at rev 1
                w1 = _make_autobrr_movie_webhook(
                    release_name="Runtime.Drift.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="905001",
                    title="Runtime Drift Film",
                    year=2024,
                    meta_imdb="tt2024905",
                    meta_tmdb=2024905,
                    freeleech=False,
                )
                rel_id = client.post("/webhook/test-webhook-secret", json=w1).json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(rel_id)["identity_gate_status"], "verified")  # type: ignore[index]
                self.assertEqual(len(actions.card_calls), 1)

                # 2) Correction exposes a 195-minute MediaInfo runtime conflict while evaluator returns
                #    matching IDs with low confidence -> MUST supersede rev 1 with rev 2 evidence_review
                #    and preserve the MediaInfo runtime conflict reason!
                tl_runtime["minutes"] = 195
                eval_conf["level"] = "low"
                w2 = _make_autobrr_movie_webhook(
                    release_name="Runtime.Drift.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="905001",
                    title="Runtime Drift Film",
                    year=2024,
                    meta_imdb="tt2024905",
                    meta_tmdb=2024905,
                    freeleech=True,
                )
                client.post("/webhook/test-webhook-secret", json=w2)
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))

                rel_after = db.get_release(rel_id)
                assert rel_after is not None
                self.assertEqual(rel_after["state"], "completed")
                self.assertEqual(rel_after["identity_gate_status"], "evidence_review")
                self.assertIsNone(rel_after["verified_tmdb_id"])
                self.assertEqual(rel_after["card_revision"], 2)
                self.assertIn("195m", str(rel_after["identity_conflict_reason"] or ""))
                self.assertEqual(len(actions.card_calls), 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertEqual(actions.card_calls[-1]["card_revision"], 2)
                self.assertIn("195m", str(actions.card_calls[-1]["conflict_reason"] or ""))

    def test_tl_imdb_field_text_vs_href_conflict_and_rating_labels(self) -> None:
        """Codex Round 6 P1: Explicit textual IMDb IDs in #imdb must be checked even when a link is present."""
        from services.movie_intake_api.tmdb_verifier import run_independent_identity_gate

        rel = "Link.Conflict.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES"

        # 1) Exact Codex repro: conflicting textual ID alongside valid IMDb link -> listing_id_conflict
        html_conflict = (
            f'<h1 id="torrentnameid">{rel}</h1>'
            '<div id="imdb">tt9999999 <a href="https://www.imdb.com/title/tt2024001/">IMDb</a></div>'
        )
        res_conflict = parse_tl_listing_html(html_conflict, exact_release_name=rel)
        self.assertEqual(res_conflict.outcome, "listing_id_conflict")
        self.assertIsNone(res_conflict.imdb_id)

        # 2) No-space boundary variant: tt9999999<a href="...">IMDb</a> -> listing_id_conflict
        html_nospace = (
            f'<h1 id="torrentnameid">{rel}</h1>'
            '<div id="imdb">tt9999999<a href="https://www.imdb.com/title/tt2024001/">IMDb</a></div>'
        )
        self.assertEqual(parse_tl_listing_html(html_nospace, exact_release_name=rel).outcome, "listing_id_conflict")

        # 3) Malformed textual ID alongside valid IMDb link -> listing_id_malformed
        html_malformed = (
            f'<h1 id="torrentnameid">{rel}</h1>'
            '<div id="imdb">tt123 <a href="https://www.imdb.com/title/tt2024001/">IMDb</a></div>'
        )
        self.assertEqual(parse_tl_listing_html(html_malformed, exact_release_name=rel).outcome, "listing_id_malformed")

        # 4) Matching textual ID + link + ordinary rating label -> listing_id_found
        html_agree = (
            f'<h1 id="torrentnameid">{rel}</h1>'
            '<div id="imdb">tt2024001 <a href="https://www.imdb.com/title/tt2024001/">7.8 / 10 (IMDb)</a></div>'
        )
        res_agree = parse_tl_listing_html(html_agree, exact_release_name=rel)
        self.assertEqual(res_agree.outcome, "listing_id_found")
        self.assertEqual(res_agree.imdb_id, "tt2024001")

        # 5) <a id="imdb" href="..."> with rating label vs conflicting text
        html_anchor_rating = (
            f'<h1 id="torrentnameid">{rel}</h1>'
            '<a id="imdb" href="https://www.imdb.com/title/tt2024001/">7.8 / 10 (IMDb)</a>'
        )
        res_anchor = parse_tl_listing_html(html_anchor_rating, exact_release_name=rel)
        self.assertEqual(res_anchor.outcome, "listing_id_found")
        self.assertEqual(res_anchor.imdb_id, "tt2024001")

        html_anchor_conflict = (
            f'<h1 id="torrentnameid">{rel}</h1>'
            '<a id="imdb" href="https://www.imdb.com/title/tt2024001/">tt9999999</a>'
        )
        self.assertEqual(
            parse_tl_listing_html(html_anchor_conflict, exact_release_name=rel).outcome,
            "listing_id_conflict",
        )

        # 5b) Void HTML elements (<br>, <wbr>, <br/>) inside #imdb preserve word boundaries
        for void_tag, expected_outcome in (
            ("<br>", "listing_id_conflict"),
            ("<wbr>", "listing_id_conflict"),
            ("<br/>", "listing_id_conflict"),
        ):
            html_void = (
                f'<h1 id="torrentnameid">{rel}</h1>'
                f'<div id="imdb"><a href="https://www.imdb.com/title/tt2024001/">IMDb{void_tag}tt9999999</a></div>'
            )
            res_void = parse_tl_listing_html(html_void, exact_release_name=rel)
            self.assertEqual(res_void.outcome, expected_outcome)
            self.assertIsNone(res_void.imdb_id)

        html_void_malformed = (
            f'<h1 id="torrentnameid">{rel}</h1>'
            '<div id="imdb"><a href="https://www.imdb.com/title/tt2024001/">IMDb<br/>tt123</a></div>'
        )
        self.assertEqual(
            parse_tl_listing_html(html_void_malformed, exact_release_name=rel).outcome,
            "listing_id_malformed",
        )

        # 6) Verify conflicting parse result fails the independent identity gate (evidence_review only)
        with tempfile.TemporaryDirectory() as tmp:
            movie = TmdbMovieRecord(
                tmdb_id=2024001,
                title="Link Conflict",
                original_title="Link Conflict",
                year=2024,
                imdb_id="tt2024001",
                runtime_minutes=110,
            )
            tmdb = StubTmdbClient(Path(tmp) / "tmdb.json", {2024001: movie}, {"tt2024001": [2024001]})
            gate = asyncio.run(
                run_independent_identity_gate(
                    tmdb_client=tmdb,
                    exact_release_name=rel,
                    proposed_imdb_id="tt2024001",
                    proposed_tmdb_id=2024001,
                    proposal_confidence="high",
                    from_evaluator=False,
                    tl_applicable=True,
                    tl_outcome=res_conflict.outcome,
                    observed_meta_imdb="tt2024001",
                    observed_meta_tmdb=2024001,
                    observed_tl_imdb=res_conflict.imdb_id,
                    file_runtime_minutes=110,
                )
            )
            self.assertEqual(gate.status, "evidence_review")
            self.assertTrue(gate.is_genuine_conflict)

    def test_year_like_movie_titles_parse_and_verify_cleanly(self) -> None:
        """Codex Round 7 P1: Year-like numeric title tokens must not be misidentified as the release year."""
        from services.movie_intake_api.tmdb_verifier import (
            parse_release_title_year,
            run_independent_identity_gate,
        )

        cases = [
            ("Blade.Runner.2049.2017.2160p.UHD.BluRay.REMUX-CiNEPHiLES", ("Blade Runner 2049", 2017)),
            ("Wonder.Woman.1984.2020.2160p.UHD.BluRay.REMUX-GRP", ("Wonder Woman 1984", 2020)),
            ("2001.A.Space.Odyssey.1968.2160p.UHD.BluRay.REMUX-GRP", ("2001 A Space Odyssey", 1968)),
            ("1917.2019.2160p.UHD.BluRay.REMUX-GRP", ("1917", 2019)),
            ("1984.1984.1080p.BluRay.REMUX-GRP", ("1984", 1984)),
            ("Blade.Runner.2049.2160p.UHD.BluRay.REMUX-GRP", ("Blade Runner 2049", None)),
            ("Example.Film.REMASTERED.2024.2160p.UHD.BluRay.REMUX-GRP", ("Example Film", 2024)),
        ]
        for rel_name, expected in cases:
            self.assertEqual(parse_release_title_year(rel_name), expected, msg=rel_name)

        with tempfile.TemporaryDirectory() as tmp:
            br2049 = TmdbMovieRecord(
                tmdb_id=335984,
                title="Blade Runner 2049",
                original_title="Blade Runner 2049",
                year=2017,
                imdb_id="tt1856101",
                runtime_minutes=164,
            )
            ww84 = TmdbMovieRecord(
                tmdb_id=464052,
                title="Wonder Woman 1984",
                original_title="Wonder Woman 1984",
                year=2020,
                imdb_id="tt7126948",
                runtime_minutes=151,
            )
            tmdb = StubTmdbClient(
                Path(tmp) / "tmdb.json",
                {335984: br2049, 464052: ww84},
                {"tt1856101": [335984], "tt7126948": [464052]},
            )

            gate_br = asyncio.run(
                run_independent_identity_gate(
                    tmdb_client=tmdb,
                    exact_release_name="Blade.Runner.2049.2017.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    proposed_imdb_id="tt1856101",
                    proposed_tmdb_id=335984,
                    proposal_confidence="high",
                    from_evaluator=False,
                    tl_applicable=True,
                    tl_outcome="listing_id_found",
                    observed_meta_imdb="tt1856101",
                    observed_meta_tmdb=335984,
                    observed_tl_imdb="tt1856101",
                    file_runtime_minutes=164,
                )
            )
            self.assertEqual(gate_br.status, "verified")
            self.assertEqual(gate_br.verified_movie.tmdb_id, 335984)  # type: ignore[union-attr]

            gate_ww = asyncio.run(
                run_independent_identity_gate(
                    tmdb_client=tmdb,
                    exact_release_name="Wonder.Woman.1984.2020.2160p.UHD.BluRay.REMUX-GRP",
                    proposed_imdb_id="tt7126948",
                    proposed_tmdb_id=464052,
                    proposal_confidence="high",
                    from_evaluator=False,
                    tl_applicable=True,
                    tl_outcome="listing_id_found",
                    observed_meta_imdb="tt7126948",
                    observed_meta_tmdb=464052,
                    observed_tl_imdb="tt7126948",
                    file_runtime_minutes=151,
                )
            )
            self.assertEqual(gate_ww.status, "verified")
            self.assertEqual(gate_ww.verified_movie.tmdb_id, 464052)  # type: ignore[union-attr]

    def test_unusable_delivery_acks_treated_as_ambiguous_never_retried(self) -> None:
        """Codex Round 8 P1: Unusable card/eval acks must become ambiguous_delivery / ambiguous_reply, never failed+retry."""
        def _card_transport(request: httpx.Request) -> httpx.Response:
            mode = json.loads(request.content.decode("utf-8")).get("mode")
            if mode == "ok":
                return httpx.Response(200, json={"status": "delivered", "ack_chat_id": "-1001", "ack_message_id": 42})
            if mode == "delivered_no_msg":
                return httpx.Response(200, json={"status": "delivered"})
            if mode == "delivered_nonnumeric_msg":
                return httpx.Response(200, json={"status": "delivered", "ack_message_id": "abc"})
            if mode == "delivered_bool_msg":
                return httpx.Response(200, json={"status": "delivered", "ack_message_id": True})
            if mode == "invalid_json":
                return httpx.Response(200, text="<html>not json</html>")
            if mode == "list_json":
                return httpx.Response(200, json=[1, 2, 3])
            if mode == "unknown_status":
                return httpx.Response(200, json={"status": "accepted_maybe"})
            if mode == "explicit_failed":
                return httpx.Response(200, json={"status": "failed", "detail": "rejected_before_send"})
            if mode == "http_400":
                return httpx.Response(400, json={"detail": "bad payload"})
            if mode == "http_502":
                return httpx.Response(502, text="bad gateway")
            if mode == "connect":
                raise httpx.ConnectError("refused")
            raise httpx.ReadTimeout("timed out")

        async def _check_client() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_card_transport)) as hc:
                ac = ActionsApiClient(base_url="http://127.0.0.1:40721", internal_token="tok", http_client=hc)
                ok_resp = await ac.deliver_card({"mode": "ok"})
                self.assertEqual(ok_resp.status, "delivered")
                self.assertEqual(ok_resp.ack_message_id, 42)

                for amb_mode in (
                    "delivered_no_msg",
                    "delivered_nonnumeric_msg",
                    "delivered_bool_msg",
                    "invalid_json",
                    "list_json",
                    "unknown_status",
                    "http_502",
                    "timeout",
                ):
                    r = await ac.deliver_card({"mode": amb_mode})
                    self.assertEqual(r.status, "ambiguous_delivery", msg=amb_mode)

                for fail_mode in ("explicit_failed", "http_400", "connect"):
                    r = await ac.deliver_card({"mode": fail_mode})
                    self.assertEqual(r.status, "failed", msg=fail_mode)

                # Invalid JSON / unknown status on deliver_evaluation also yields ambiguous_reply (never 'sent')
                self.assertEqual((await ac.deliver_evaluation({"mode": "invalid_json"})).status, "ambiguous_reply")
                self.assertEqual((await ac.deliver_evaluation({"mode": "list_json"})).status, "ambiguous_reply")
                self.assertEqual((await ac.deliver_evaluation({"mode": "unknown_status"})).status, "ambiguous_reply")
                self.assertEqual((await ac.deliver_evaluation({"mode": "explicit_failed"})).status, "failed")

        asyncio.run(_check_client())

        # End-to-end worker check: HTTP 200 {"status": "delivered"} without ack_message_id enters
        # ambiguous_delivery (0 retries, 0 eval replies) on both fast and unresolved paths
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024908,
                title="Unusable Ack Film",
                original_title="Unusable Ack Film",
                year=2024,
                imdb_id="tt2024908",
                runtime_minutes=110,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {2024908: movie}, {"tt2024908": [2024908]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda ext, _n: (
                    TorrentLeechResult(
                        outcome="listing_id_found",
                        imdb_id="tt2024908",
                        file_runtime_minutes=110,
                        audio_languages=("English",),
                    )
                    if ext == "9081"
                    else TorrentLeechResult(outcome="listing_temporary_http")
                ),
            )
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Unusable Ack Film", year=2024, imdb_id="tt2024908", tmdb_id=2024908
                    ),
                )
            )
            actions = StubActionsClient(
                radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"),
                card_handler=lambda _p: DeliveryCardResponse(status="delivered", ack_message_id=None),
            )
            app = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )
            with TestClient(app) as client:
                r_fast = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Unusable.Ack.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                        external_id="9081",
                        title="Unusable Ack Film",
                        year=2024,
                        meta_imdb="tt2024908",
                    ),
                )
                fast_id = r_fast.json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(fast_id)["state"], "ambiguous_delivery")  # type: ignore[index]
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.eval_reply_calls), 0)

                r_unres = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Unusable.Ack.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                        external_id="9082",
                        title="Unusable Ack Film",
                        year=2024,
                        meta_imdb=None,
                    ),
                )
                unres_id = r_unres.json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(unres_id)["state"], "ambiguous_delivery")  # type: ignore[index]
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.eval_reply_calls), 0)


if __name__ == "__main__":
    unittest.main()
