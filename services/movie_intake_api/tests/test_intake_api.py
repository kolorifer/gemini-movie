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
from services.movie_intake_api.classifier import (
    classify_rejection_reasons,
    classify_webhook_payload,
    is_canonical_tl_download_url,
    validate_legacy_alt_grab_rejections,
)
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
from services.movie_intake_api.config import IntakeConfig
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
    action_rejected: bool = True,
    action_type: Optional[str] = "RADARR",
    rejections: Optional[list[str]] = None,
    download_url: Optional[str] = None,
    event: str = "action.rejected",
    filter_name: Optional[str] = "Movies Remux",
    filter_id: Optional[int] = 7,
    resolution: Optional[str] = None,
    source: Optional[str] = None,
    media_processing: Optional[str] = "REMUX",
) -> dict[str, Any]:
    effective_dl_url = (
        download_url
        if download_url is not None
        else f"https://www.torrentleech.org/rss/download/{external_id}/SUPER_SECRET_PASSKEY_VALUE/{release_name}.torrent"
    )
    inferred_res = "2160p" if "2160p" in release_name else "1080p"
    effective_res = inferred_res if resolution is None else resolution
    effective_src = ("UHD BluRay" if effective_res == "2160p" else "BluRay") if source is None else source
    rel: dict[str, Any] = {
        "name": release_name,
        "type": release_type,
        "category": category,
        "external_id": external_id,
        "size": size,
        "resolution": effective_res,
        "source": effective_src,
        "hdr": ["DV", "HDR10"] if "DV" in release_name else [],
        "media_processing": media_processing,
        "group": "CiNEPHiLES" if "CiNEPHiLES" in release_name else "FraMeSToR",
        "download_url": effective_dl_url,
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

    action_obj: dict[str, Any] = {"rejected": action_rejected}
    if action_type is not None:
        action_obj["type"] = action_type

    effective_rejections = (
        list(rejections)
        if rejections is not None
        else (["Unknown Movie"] if action_rejected else [])
    )
    result_obj: dict[str, Any] = {"rejections": effective_rejections}
    if action_rejected:
        result_obj["status"] = "PUSH_REJECTED"

    data_obj: dict[str, Any] = {
        "indexer": indexer,
        "action": action_obj,
        "result": result_obj,
        "release": rel,
    }
    if filter_name is not None or filter_id is not None:
        filter_obj: dict[str, Any] = {}
        if filter_name is not None:
            filter_obj["name"] = filter_name
        if filter_id is not None:
            filter_obj["id"] = filter_id
        data_obj["filter"] = filter_obj

    return {
        "event": event,
        "data": data_obj,
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
        alternative_grab_configured: bool = False,
        capabilities_handler: Optional[Any] = None,
    ) -> None:
        super().__init__(base_url="http://127.0.0.1:40721", internal_token="stub")
        self._radarr_handler = radarr_handler
        self._card_handler = card_handler
        self._eval_reply_handler = eval_reply_handler
        self.alternative_grab_configured = alternative_grab_configured
        self._capabilities_handler = capabilities_handler
        self.capability_calls: int = 0
        self.radarr_calls: list[dict[str, Any]] = []
        self.card_calls: list[dict[str, Any]] = []
        self.eval_reply_calls: list[dict[str, Any]] = []
        self._next_msg_id = 500
        self._delivered_by_release: dict[str, DeliveryCardResponse] = {}
        self._eval_sent_releases: set[str] = set()

    async def get_capabilities(self) -> Optional[bool]:
        self.capability_calls += 1
        if self._capabilities_handler is not None:
            return self._capabilities_handler()
        return self.alternative_grab_configured

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
            preserved = bool(
                card_payload.get("preserve_eval_reply") is True and rel_id in self._eval_sent_releases
            )
            if not preserved:
                self._eval_sent_releases.discard(rel_id)
            return DeliveryCardResponse(
                status=prev.status,
                ack_chat_id=prev.ack_chat_id,
                ack_message_id=prev.ack_message_id,
                ack_thread_id=prev.ack_thread_id,
                eval_reply_preserved=preserved,
            )
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
            res = self._eval_reply_handler(eval_reply_payload)
        else:
            res = DeliveryEvalResponse(status="sent")
        rel_id = str(eval_reply_payload.get("release_id") or "")
        if rel_id and res.status in ("sent", "ambiguous_reply"):
            self._eval_sent_releases.add(rel_id)
        return res


def _make_config(tmpdir: Path, **overrides: Any) -> IntakeConfig:
    base = {
        "host": "127.0.0.1",
        "port": 40720,
        "webhook_secret": "test-webhook-secret",
        "internal_api_secret": "test-internal-secret",
        "tl_metadata_enabled": True,
        "tl_cookie_file": tmpdir / "cookies.json",
        "tmdb_api_token": "stub-tmdb",
        "tmdb_cache_path": tmpdir / "intake-tmdb-cache.json",
        "webhook_archive_dir": tmpdir / "webhook-archive",
        "intake_db_path": tmpdir / "intake.db",
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
    def test_config_enforces_absolute_paths_loopback_and_least_privilege_isolation(self) -> None:
        with self.assertRaises(ValueError):
            IntakeConfig.from_env({"MOVIE_INTEL_HOST": "0.0.0.0"})
        with self.assertRaises(ValueError):
            IntakeConfig.from_env({"WEBHOOK_ARCHIVE_DIR": "relative/path"})
        with self.assertRaises(ValueError):
            IntakeConfig.from_env({"MOVIE_INTEL_TL_COOKIE_FILE": "~/.config/cookies.json"})

        # Default TMDB cache path is isolated from legacy tmdb-cache.json, and intake NEVER reads alt-grab qBit config
        default_cfg = IntakeConfig.from_env({"MOVIE_INTEL_ALTERNATIVE_GRAB_CONFIG": "/etc/nonexistent-qbit.json"})
        self.assertEqual(default_cfg.tmdb_cache_path.name, "intake-tmdb-cache.json")
        self.assertFalse(hasattr(default_cfg, "alternative_grab_config_path"))

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
            actions = StubActionsClient(
                radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent", alternative_grab_configured=True),
                alternative_grab_configured=True,
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
                    release_name="True.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="9002001",
                    title="True Film",
                    year=2024,
                    meta_imdb="tt0099999",
                    action_rejected=True,
                    rejections=["Existing file meets cutoff: Remux-1080p"],
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

            # Identity conflict MUST render evidence_review ONLY (never actionable_grab or alternative_grab!),
            # routed to Grab Anyway topic via preferred_topic_kind with 0 buttons
            self.assertEqual(len(actions.card_calls), 1)
            card = actions.card_calls[0]
            self.assertEqual(card["delivery_kind"], "evidence_review")
            self.assertEqual(card["identity_gate_status"], "evidence_review")
            self.assertEqual(card.get("preferred_topic_kind"), "alternative_grab")
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

            radarr_next = {
                "resp": RadarrStatusResponse(
                    status="watched_unmonitored", movie_id=77, monitored=False, alternative_grab_configured=True
                )
            }
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
                alternative_grab_configured=True,
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
                radarr_next["resp"] = RadarrStatusResponse(
                    status="monitored_no_file", movie_id=77, monitored=True, alternative_grab_configured=True
                )
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

                owned_snapshot = {
                    "movie_id": 77,
                    "file_id": 888,
                    "monitored": True,
                    "quality_profile_id": 1,
                    "relative_path": "Library.Film.2024.Remux-1080p.mkv",
                    "edition": "",
                    "release_group": "FraMeSToR",
                    "size": 31_000_000_000,
                    "quality": "Remux-1080p",
                }

                # 4c: Already owned (no Alternative Grab trigger) -> suppressed before delivery
                radarr_next["resp"] = RadarrStatusResponse(
                    status="owned_file",
                    movie_id=77,
                    file_id=888,
                    existing_quality="Remux-1080p",
                    monitored=True,
                    movie_file_snapshot=owned_snapshot,
                    alternative_grab_configured=True,
                )
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

                # 4c-alt: Already owned (monitored) + allowed legacy cutoff_met -> alternative_grab bound to full 9-field snapshot
                client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX.DV-CiNEPHiLES",
                        external_id="4004",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                        action_rejected=True,
                        rejections=["Existing file meets cutoff: Remux-1080p"],
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                alt_card = actions.card_calls[-1]
                self.assertEqual(alt_card["delivery_kind"], "alternative_grab")
                self.assertEqual(alt_card["bound_existing_movie_id"], 77)
                self.assertEqual(alt_card["bound_existing_file_id"], 888)
                self.assertEqual(alt_card["bound_rejection_class"], "cutoff_met")
                self.assertEqual(alt_card["external_id"], "4004")
                self.assertEqual(alt_card["rejection_reasons"], ["Existing file meets cutoff: Remux-1080p"])
                self.assertEqual(alt_card["bound_movie_file_snapshot"], owned_snapshot)

                # Unmonitored owned movie MUST NOT mint Alternative Grab (suppressed as already_owned)
                radarr_next["resp"] = RadarrStatusResponse(
                    status="owned_file",
                    movie_id=77,
                    file_id=888,
                    existing_quality="Remux-1080p",
                    monitored=False,
                    movie_file_snapshot=dict(owned_snapshot, monitored=False),
                    alternative_grab_configured=True,
                )
                cards_before_unmon = len(actions.card_calls)
                r_unmon = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.2160p.UHD.BluRay.REMUX.DV-OTHER",
                        external_id="40041",
                        title="Library Film",
                        year=2024,
                        meta_imdb="tt4024001",
                        action_rejected=True,
                        rejections=["Existing file meets cutoff: Remux-1080p"],
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(db.get_release(r_unmon.json()["release_id"])["suppression_reason"], "already_owned")  # type: ignore[index]
                self.assertEqual(len(actions.card_calls), cards_before_unmon)

                # Routine disallowed quality rejection on eligible BD REMUX -> suppressed early (0 TL calls, 0 cards)
                tl_before = len(tl.calls)
                r_qrej = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="Library.Film.2024.1080p.BluRay.REMUX-GRP",
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

    def test_failed_initial_card_followed_by_transient_tl_or_tmdb_outage_does_not_complete_empty(self) -> None:
        """Codex Round 9 P1: Preservation shortcuts must require an acknowledged card and never complete without delivery."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=2024909,
                title="Retry Delivery Film",
                original_title="Retry Delivery Film",
                year=2024,
                imdb_id="tt2024909",
                runtime_minutes=114,
            )

            reverse_box: dict[str, Optional[list[int]]] = {"res": [2024909]}

            class ToggleTmdbClient(StubTmdbClient):
                async def find_movies_by_imdb_id(self, imdb_id: str) -> Optional[list[int]]:
                    return reverse_box["res"]

            tmdb = ToggleTmdbClient(cfg.tmdb_cache_path, {2024909: movie}, {"tt2024909": [2024909]})
            tl_box = {
                "res": TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024909",
                    file_runtime_minutes=114,
                    audio_languages=("English",),
                )
            }
            tl = StubTlScraper(db, cfg.tl_cookie_file, lambda _e, _n: tl_box["res"])
            triage = StubTriageClient(
                lambda _b: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Retry Delivery Film",
                        year=2024,
                        imdb_id="tt2024909",
                        tmdb_id=2024909,
                        confidence="high",
                    ),
                )
            )
            card_fail_box = {"fail": True, "next_id": 910}

            def _card_handler(payload: dict[str, Any]) -> DeliveryCardResponse:
                if card_fail_box["fail"]:
                    card_fail_box["fail"] = False
                    return DeliveryCardResponse(status="failed", detail="actions_connect_error")
                card_fail_box["next_id"] += 1
                return DeliveryCardResponse(
                    status="delivered",
                    ack_chat_id="-100909",
                    ack_message_id=card_fail_box["next_id"],
                    ack_thread_id=9,
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
                # 1) Exact Codex Round 9 repro: initial card definitively fails (rev 0, ack None),
                #    then retry hits transient TL failure -> MUST deliver rev 1 card + eval reply
                w1 = _make_autobrr_movie_webhook(
                    release_name="Retry.Delivery.Film.2024.2160p.UHD.BluRay.REMUX-CiNEPHiLES",
                    external_id="909001",
                    title="Retry Delivery Film",
                    year=2024,
                    meta_imdb="tt2024909",
                    meta_tmdb=2024909,
                )
                rel1_id = client.post("/webhook/test-webhook-secret", json=w1).json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                mid1 = db.get_release(rel1_id)
                assert mid1 is not None
                self.assertEqual(mid1["state"], "queued")
                self.assertEqual(mid1["card_revision"], 0)
                self.assertIsNone(mid1["ack_message_id"])

                # Attempt 2: TL has transient HTTP outage while actions recovers
                tl_box["res"] = TorrentLeechResult(outcome="listing_temporary_http")
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                done1 = db.get_release(rel1_id)
                assert done1 is not None
                self.assertEqual(done1["state"], "completed")
                self.assertEqual(done1["card_revision"], 1)
                self.assertIsNotNone(done1["ack_message_id"])
                self.assertEqual(done1["identity_gate_status"], "evidence_review")
                self.assertIsNone(done1["verified_tmdb_id"])
                self.assertEqual(len(actions.card_calls), 2)
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "evidence_review")
                self.assertEqual(len(actions.eval_reply_calls), 1)

                # 2) Same sequence hitting transient TMDB reverse-lookup outage on retry
                #    (exercises post-eval preservation guard too) -> MUST deliver rev 1 card + eval reply
                tl_box["res"] = TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024909",
                    file_runtime_minutes=114,
                    audio_languages=("English",),
                )
                reverse_box["res"] = [2024909]
                card_fail_box["fail"] = True
                w2 = _make_autobrr_movie_webhook(
                    release_name="Retry.Delivery.Film.2024.1080p.BluRay.REMUX-FraMeSToR",
                    external_id="909002",
                    title="Retry Delivery Film",
                    year=2024,
                    meta_imdb="tt2024909",
                    meta_tmdb=2024909,
                )
                rel2_id = client.post("/webhook/test-webhook-secret", json=w2).json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(db.get_release(rel2_id)["state"], "queued")  # type: ignore[index]

                reverse_box["res"] = None
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                done2 = db.get_release(rel2_id)
                assert done2 is not None
                self.assertEqual(done2["state"], "completed")
                self.assertEqual(done2["card_revision"], 1)
                self.assertIsNotNone(done2["ack_message_id"])
                self.assertEqual(done2["identity_gate_status"], "evidence_review")
                self.assertIsNone(done2["verified_tmdb_id"])
                self.assertEqual(len(actions.eval_reply_calls), 2)

                # 3) Real fast-path ack + crash mid-evaluation, followed by lease recovery & transient TL outage:
                #    proves record_card_ack atomically persists identity_gate_status='verified' + movie IDs
                #    so recovery preserves rev 1 without downgrading, then finishes the reply once TL recovers.
                reverse_box["res"] = [2024909]
                card_fail_box["fail"] = False
                orig_triage_handler = triage._handler

                def _crash_once(body: dict[str, Any]) -> TriageEvalResult:
                    triage._handler = orig_triage_handler
                    raise RuntimeError("simulated_worker_crash_mid_evaluate")

                triage._handler = _crash_once
                w3 = _make_autobrr_movie_webhook(
                    release_name="Retry.Delivery.Film.2024.2160p.UHD.BluRay.REMUX-GRP3",
                    external_id="909003",
                    title="Retry Delivery Film",
                    year=2024,
                    meta_imdb="tt2024909",
                    meta_tmdb=2024909,
                )
                rel3_id = client.post("/webhook/test-webhook-secret", json=w3).json()["release_id"]
                with self.assertRaises(RuntimeError):
                    asyncio.run(app.state.worker.process_next_queued())

                # Immediately after the crash (BEFORE recovery), rev 1 receipt + verified identity MUST exist
                crashed3 = db.get_release(rel3_id)
                assert crashed3 is not None
                self.assertEqual(crashed3["state"], "evaluating")
                self.assertEqual(crashed3["card_revision"], 1)
                self.assertEqual(crashed3["identity_gate_status"], "verified")
                self.assertEqual(crashed3["verified_tmdb_id"], 2024909)
                self.assertEqual(crashed3["verified_imdb_id"], "tt2024909")
                ack_msg_3 = crashed3["ack_message_id"]
                self.assertIsNotNone(ack_msg_3)

                # Simulate fresh worker process startup recovering the orphaned lease during a transient TL outage
                recovered_worker = IntakeWorker(
                    config=cfg,
                    db=db,
                    tl_scraper=tl,
                    tmdb_client=tmdb,
                    triage_client=triage,
                    actions_client=actions,
                    worker_id="recovered-fastpath-worker",
                )
                self.assertEqual(
                    db.recover_orphaned_leases(
                        active_worker_id="recovered-fastpath-worker",
                        recover_dead_owners=True,
                    ),
                    1,
                )
                cards_before = len(actions.card_calls)
                replies_before = len(actions.eval_reply_calls)
                tl_box["res"] = TorrentLeechResult(outcome="listing_temporary_http")
                self.assertTrue(asyncio.run(recovered_worker.process_next_queued()))
                mid3 = db.get_release(rel3_id)
                assert mid3 is not None
                self.assertEqual(mid3["state"], "queued")
                self.assertEqual(mid3["identity_gate_status"], "verified")
                self.assertEqual(mid3["verified_tmdb_id"], 2024909)
                self.assertEqual(mid3["card_revision"], 1)
                self.assertEqual(len(actions.card_calls), cards_before)
                self.assertEqual(len(actions.eval_reply_calls), replies_before)

                # Once TL recovers, finishes evaluation + bound reply reusing ack_msg_3 (0 extra cards)
                tl_box["res"] = TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt2024909",
                    file_runtime_minutes=114,
                    audio_languages=("English",),
                )
                self.assertTrue(asyncio.run(recovered_worker.process_next_queued()))
                done3 = db.get_release(rel3_id)
                assert done3 is not None
                self.assertEqual(done3["state"], "completed")
                self.assertEqual(done3["card_revision"], 1)
                self.assertEqual(done3["identity_gate_status"], "verified")
                self.assertEqual(done3["eval_reply_status"], "sent")
                self.assertEqual(len(actions.card_calls), cards_before)
                self.assertEqual(len(actions.eval_reply_calls), replies_before + 1)
                self.assertEqual(actions.eval_reply_calls[-1]["ack_message_id"], ack_msg_3)

    def test_legacy_alternative_grab_rejection_matrix_and_capability_boundary(self) -> None:
        """Exact 4 legacy Radarr rejection forms, source checks, /internal/capabilities, and zero URL persistence."""
        # 1) Exact 4 legacy forms pass across allowed sources/resolutions
        allowed_cases = [
            (["Existing file meets cutoff: Remux-2160p"], "cutoff_met"),
            (["Existing file meets cutoff: Bluray-1080p"], "cutoff_met"),
            (["Existing file meets cutoff: WEBDL-720p"], "cutoff_met"),
            (["Existing file meets cutoff: WEBRip-1080p"], "cutoff_met"),
            (["Existing file meets cutoff: HDTV-720p"], "cutoff_met"),
            (["Existing file on disk is of equal or higher preference: Remux-1080p v1"], "equal_or_higher_preference"),
            (["Existing file is of equal or higher preference: Bluray-2160p v2"], "equal_or_higher_preference"),
            (["Upgrades are not allowed by the quality profile"], "upgrades_disallowed"),
            (["Quality profile does not allow upgrades."], "upgrades_disallowed"),
            (["Existing file on disk has an equal or higher Custom Format score (100 >= 50)"], "custom_format_score"),
            (
                ["Not a Custom Format upgrade for existing movie file(s). New: [DV] (10) do not improve on Existing: [DV FEL] (20)"],
                "custom_format_score",
            ),
        ]
        for reasons, expected_class in allowed_cases:
            wh = _make_autobrr_movie_webhook(
                external_id="77001",
                action_rejected=True,
                rejections=reasons,
            )
            cw = classify_webhook_payload(wh)
            self.assertTrue(cw.has_canonical_tl_download_ref)
            mode, matched = classify_rejection_reasons(cw)
            self.assertEqual(mode, "legacy_alt_grab_eligible", msg=f"Failed for {reasons}")
            self.assertEqual(matched, expected_class)

        # 2) Disallowed quality forms, v0, >8 reasons, mixed list, non-Radarr action, non-canonical URL, long name
        disallowed_quality_payloads = [
            # Unsupported source/resolution
            _make_autobrr_movie_webhook(
                external_id="77002", action_rejected=True, rejections=["Existing file meets cutoff: DVD-480p"]
            ),
            # v0 version is not positive
            _make_autobrr_movie_webhook(
                external_id="77003",
                action_rejected=True,
                rejections=["Existing file on disk is of equal or higher preference: Remux-1080p v0"],
            ),
            # > 8 rejection reasons
            _make_autobrr_movie_webhook(
                external_id="77004",
                action_rejected=True,
                rejections=["Existing file meets cutoff: Remux-1080p"] * 9,
            ),
            # Mixed quality list (1 allowed + 1 disallowed quality reason)
            _make_autobrr_movie_webhook(
                external_id="77005",
                action_rejected=True,
                rejections=["Existing file meets cutoff: Remux-1080p", "Exceeds maximum size"],
            ),
            # Non-Radarr action type
            _make_autobrr_movie_webhook(
                external_id="77006",
                action_rejected=True,
                action_type="QBITTORRENT",
                rejections=["Existing file meets cutoff: Remux-1080p"],
            ),
            # Non-TorrentLeech indexer
            _make_autobrr_movie_webhook(
                indexer="PrivateHD",
                external_id="77007",
                action_rejected=True,
                rejections=["Existing file meets cutoff: Remux-1080p"],
            ),
            # Non-canonical download URL (mismatched external_id in URL path)
            _make_autobrr_movie_webhook(
                external_id="77008",
                action_rejected=True,
                rejections=["Existing file meets cutoff: Remux-1080p"],
                download_url="https://www.torrentleech.org/rss/download/99999/KEY/Release.torrent",
            ),
            # Non-HTTPS download URL
            _make_autobrr_movie_webhook(
                external_id="77009",
                action_rejected=True,
                rejections=["Existing file meets cutoff: Remux-1080p"],
                download_url="http://www.torrentleech.org/rss/download/77009/KEY/Release.torrent",
            ),
            # Release name > 512 chars
            _make_autobrr_movie_webhook(
                release_name="Example.Film.2024." + ("A" * 510) + ".2160p.UHD.BluRay.REMUX-GRP",
                external_id="77010",
                action_rejected=True,
                rejections=["Existing file meets cutoff: Remux-1080p"],
            ),
        ]
        for payload in disallowed_quality_payloads:
            cw = classify_webhook_payload(payload)
            mode, _ = classify_rejection_reasons(cw)
            self.assertEqual(mode, "quality_rejected_suppress", msg=f"Should suppress: {payload}")

        # Mixed list containing an unknown / non-quality rejection MUST continue to identity + Radarr check (not blindly drop!)
        mixed_unknown = classify_webhook_payload(
            _make_autobrr_movie_webhook(
                external_id="77011",
                action_rejected=True,
                rejections=["Existing file meets cutoff: Remux-1080p", "Unknown movie in Radarr"],
            )
        )
        self.assertEqual(classify_rejection_reasons(mixed_unknown), ("needs_identity_and_radarr_check", None))

        # 3) Worker capability check: False suppresses early (0 TL calls); None re-queues cleanly; zero URL in intake.db
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _ext, _n: TorrentLeechResult(outcome="listing_id_missing"),
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {}, {})
            triage = StubTriageClient(lambda _b: TriageEvalResult(ok=False, error_redacted="unused"))
            cap_state: dict[str, Optional[bool]] = {"val": False}
            actions = StubActionsClient(
                radarr_handler=lambda _t, _i: RadarrStatusResponse(status="absent"),
                capabilities_handler=lambda: cap_state["val"],
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
                # 3a: alternative_grab_configured=False -> suppressed early with 0 TL calls
                r_off = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        external_id="78001",
                        action_rejected=True,
                        rejections=["Existing file meets cutoff: Remux-1080p"],
                    ),
                )
                self.assertEqual(r_off.status_code, 200)
                asyncio.run(app.state.worker.process_next_queued())
                rel_off = db.get_release(r_off.json()["release_id"])
                assert rel_off is not None
                self.assertEqual(rel_off["state"], "suppressed")
                self.assertEqual(rel_off["suppression_reason"], "quality_rejected")
                self.assertEqual(len(tl.calls), 0)
                self.assertEqual(actions.capability_calls, 1)

                # 3b: capabilities endpoint transiently unavailable (None) -> re-queued cleanly (not permanently dropped)
                cap_state["val"] = None
                r_transient = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        external_id="78002",
                        action_rejected=True,
                        rejections=["Existing file meets cutoff: Remux-1080p"],
                    ),
                )
                asyncio.run(app.state.worker.process_next_queued())
                rel_t = db.get_release(r_transient.json()["release_id"])
                assert rel_t is not None
                self.assertEqual(rel_t["state"], "queued")
                self.assertEqual(rel_t["retry_count"], 1)
                self.assertEqual(len(tl.calls), 0)

            # Raw download URL & passkey MUST NEVER appear in intake.db
            db_bytes = cfg.intake_db_path.read_bytes()
            self.assertNotIn(b"SUPER_SECRET_PASSKEY_VALUE", db_bytes)
            self.assertNotIn(b"/rss/download/", db_bytes)

    def test_live_autobrr_release_new_then_action_rejected_shapes_and_eval_reuse(self) -> None:
        """
        Live autobrr shapes & eval reuse regressions (OPS-REPORT #6 + Issue #13 §1):
          1) Global `release.new` archives unchanged as `non_movie_archive_only` (0 TL/eval/cards).
          2) Qualifying `action.rejected` (`data.action.type='RADARR'`, `data.filter.name='Movies Remux'`,
             `data.result.status='PUSH_REJECTED'`) admits eligible 1080p/2160p BD REMUX releases;
             mid-flight follow-up `action.rejected` produces 1 card (rev 1) + 1 eval (complete no-op on pass 2).
          3) `release.new` (archived only) -> `action.rejected` (4-form quality cutoff on owned file)
             produces 1 Alternative Grab card + 1 eval and archives both original payloads.
          4) Radarr action change (`actionable_grab` -> `alternative_grab`) with unchanged movie
             evidence edits card to rev 2 with `preserve_eval_reply=True` and reuses the single eval reply.
          5) Unresolved path (`evidence_review`) follow-up `action.rejected` also avoids a 2nd eval.
          6) Pre-migration intake.db cleanly adds `last_card_digest` / `last_eval_reply_digest`.
        """
        import sqlite3

        def _live_release_obj(
            *,
            name: str,
            external_id: str,
            title: str,
            year: int,
            meta_imdb: Optional[str] = "tt4919268",
        ) -> dict[str, Any]:
            return {
                "name": name,
                "title": title,
                "year": year,
                "type": "movie",
                "category": "Movies :: 4K",
                "external_id": external_id,
                "meta_imdb": meta_imdb,
                "resolution": "2160p",
                "source": "UHD BluRay",
                "hdr": ["DV", "HDR"],
                "media_processing": "REMUX",
                "group": "TRiToN",
                "size": 58_000_000_000,
                "freeleech": True,
                "download_url": f"https://www.torrentleech.org/rss/download/{external_id}/LIVEKEY123/{name.replace(' ', '.')}.torrent",
            }

        def _live_release_new(rel_obj: dict[str, Any], *, evt_id: str = "evt-new-1") -> dict[str, Any]:
            return {
                "event": "release.new",
                "id": evt_id,
                "timestamp": "2026-11-26T17:34:42Z",
                "data": {
                    "indexer": "TorrentLeech",
                    "release": dict(rel_obj),
                },
            }

        def _live_action_rejected(
            rel_obj: dict[str, Any],
            *,
            rejections: list[str],
            action_type: str = "RADARR",
            filter_name: str = "Movies Remux",
            evt_id: str = "evt-rej-1",
        ) -> dict[str, Any]:
            return {
                "event": "action.rejected",
                "id": evt_id,
                "timestamp": "2026-11-26T17:35:04Z",
                "data": {
                    "indexer": "TorrentLeech",
                    "action": {"name": "Radarr 4K", "type": action_type},
                    "filter": {"id": 7, "name": filter_name},
                    "release": dict(rel_obj),
                    "result": {
                        "status": "PUSH_REJECTED",
                        "rejections": list(rejections),
                    },
                },
            }

        elio_name = "Elio 2025 2160p UHD BluRay REMUX DV HDR HEVC Atmos-TRiToN"
        elio_rel = _live_release_obj(
            name=elio_name, external_id="241889121", title="Elio", year=2025, meta_imdb="tt4919268"
        )

        # 1) Global `release.new` ends at archiving; qualifying Radarr `action.rejected` enters movie lane
        cw_new = classify_webhook_payload(_live_release_new(elio_rel))
        self.assertEqual(cw_new.classification, "non_movie_archive_only")
        self.assertFalse(cw_new.action_rejected)
        self.assertFalse(cw_new.action_is_radarr)

        cw_unknown = classify_webhook_payload(
            _live_action_rejected(
                elio_rel,
                rejections=["Unknown Movie. Unable to match to existing movie in Library using release title."],
            )
        )
        self.assertEqual(cw_unknown.classification, "movie_candidate")
        self.assertTrue(cw_unknown.action_rejected)
        self.assertTrue(cw_unknown.action_is_radarr)
        self.assertEqual(classify_rejection_reasons(cw_unknown), ("needs_identity_and_radarr_check", None))

        cw_cutoff = classify_webhook_payload(
            _live_action_rejected(elio_rel, rejections=["Existing file meets cutoff: Remux-2160p"])
        )
        self.assertEqual(cw_cutoff.classification, "movie_candidate")
        self.assertTrue(cw_cutoff.action_rejected)
        self.assertTrue(cw_cutoff.action_is_radarr)
        self.assertEqual(classify_rejection_reasons(cw_cutoff), ("legacy_alt_grab_eligible", "cutoff_met"))

        cw_non_radarr = classify_webhook_payload(
            _live_action_rejected(
                elio_rel,
                action_type="QBITTORRENT",
                rejections=["Existing file meets cutoff: Remux-2160p"],
            )
        )
        self.assertEqual(cw_non_radarr.classification, "non_movie_archive_only")
        self.assertTrue(cw_non_radarr.action_rejected)
        self.assertFalse(cw_non_radarr.action_is_radarr)
        self.assertEqual(classify_rejection_reasons(cw_non_radarr), ("quality_rejected_suppress", "cutoff_met"))

        cw_unwanted = classify_webhook_payload(
            _live_action_rejected(elio_rel, rejections=["Quality is not wanted in profile"])
        )
        self.assertEqual(cw_unwanted.classification, "movie_candidate")
        self.assertTrue(cw_unwanted.action_rejected)
        self.assertEqual(classify_rejection_reasons(cw_unwanted), ("quality_rejected_suppress", "quality_unwanted"))

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            elio_movie = TmdbMovieRecord(
                tmdb_id=1022789,
                title="Elio",
                original_title="Elio",
                year=2025,
                imdb_id="tt4919268",
                runtime_minutes=98,
                poster_path="/elio_poster.jpg",
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {1022789: elio_movie}, {"tt4919268": [1022789]})
            tl_mode: dict[str, TorrentLeechResult] = {
                "241889121": TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt4919268",
                    file_runtime_minutes=98,
                    audio_languages=("English",),
                ),
                "241889122": TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt4919268",
                    file_runtime_minutes=98,
                    audio_languages=("English",),
                ),
                "241889123": TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt4919268",
                    file_runtime_minutes=98,
                    audio_languages=("English",),
                ),
                "241889124": TorrentLeechResult(
                    outcome="listing_id_missing",
                    imdb_id=None,
                    file_runtime_minutes=98,
                    audio_languages=("English",),
                ),
            }
            tl = StubTlScraper(db, cfg.tl_cookie_file, lambda ext, _n: tl_mode[ext])

            triage_hook: dict[str, Any] = {"on_eval": None}

            def _triage_handler(req_body: dict[str, Any]) -> TriageEvalResult:
                if callable(triage_hook["on_eval"]):
                    cb = triage_hook["on_eval"]
                    triage_hook["on_eval"] = None
                    cb()
                if "Mystery.Unidentified.2025" in str(req_body.get("raw_text") or ""):
                    return TriageEvalResult(
                        ok=True,
                        payload=_make_eval_payload(
                            title="Mystery Unidentified",
                            year=2025,
                            imdb_id=None,
                            tmdb_id=None,
                            confidence="low",
                        ),
                    )
                return TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Elio",
                        year=2025,
                        imdb_id="tt4919268",
                        tmdb_id=1022789,
                        confidence="high",
                    ),
                )

            triage = StubTriageClient(_triage_handler)
            owned_snapshot = {
                "movie_id": 501,
                "file_id": 901,
                "monitored": True,
                "quality_profile_id": 4,
                "relative_path": "Elio.2025.2160p.REMUX-OLD.mkv",
                "edition": "",
                "release_group": "OLD",
                "size": 55_000_000_000,
                "quality": "Remux-2160p",
            }
            radarr_state: dict[str, RadarrStatusResponse] = {
                "status": RadarrStatusResponse(status="absent", alternative_grab_configured=True)
            }
            actions = StubActionsClient(
                radarr_handler=lambda _t, _i: radarr_state["status"],
                alternative_grab_configured=True,
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
                # 2) Global `release.new` archives cleanly (0 work), then qualifying `action.rejected` starts Pass 1,
                #    and a mid-flight follow-up `action.rejected` with revised rejection text triggers Pass 2 no-op.
                r0_new = client.post("/webhook/test-webhook-secret", json=_live_release_new(elio_rel))
                self.assertEqual(r0_new.status_code, 200)
                self.assertEqual(r0_new.json()["status"], "archived_non_movie")
                self.assertIsNone(r0_new.json()["release_id"])
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(tl.calls), 0)
                self.assertEqual(len(triage.calls), 0)
                self.assertEqual(len(actions.card_calls), 0)

                r1 = client.post(
                    "/webhook/test-webhook-secret",
                    json=_live_action_rejected(elio_rel, rejections=["Unknown Movie"], evt_id="evt-e1"),
                )
                self.assertEqual(r1.status_code, 200)
                rel_elio = r1.json()["release_id"]
                self.assertIsNotNone(rel_elio)

                # Simulate Occ 2 (updated action.rejected wording) arriving while Pass 1 is in /evaluate!
                def _post_midflight_reject() -> None:
                    r_mid = client.post(
                        "/webhook/test-webhook-secret",
                        json=_live_action_rejected(
                            elio_rel,
                            rejections=[
                                "Unknown Movie. Unable to match to existing movie in Library using release title."
                            ],
                            evt_id="evt-e2",
                        ),
                    )
                    self.assertEqual(r_mid.status_code, 200)
                    self.assertEqual(r_mid.json()["status"], "updated_correction")

                triage_hook["on_eval"] = _post_midflight_reject
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))

                # Pass 1 re-queued due to mid-flight pending_reprocess while PRESERVING eval_reply_status='sent'!
                mid_row = db.get_release(rel_elio)
                assert mid_row is not None
                self.assertEqual(mid_row["state"], "queued")
                self.assertEqual(mid_row["card_revision"], 1)
                self.assertEqual(mid_row["eval_reply_status"], "sent")
                self.assertIsNotNone(mid_row["last_card_digest"])
                self.assertIsNotNone(mid_row["last_eval_reply_digest"])

                # Pass 2 runs for Occ 2 -> MUST be a complete no-op (0 extra card edits, 0 extra evals, 0 extra replies!)
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))
                done_elio = db.get_release(rel_elio)
                assert done_elio is not None
                self.assertEqual(done_elio["state"], "completed")
                self.assertEqual(done_elio["card_revision"], 1)
                self.assertEqual(done_elio["eval_reply_status"], "sent")
                self.assertEqual(len(db.list_occurrences_for_release(rel_elio)), 2)
                self.assertEqual(len([c for c in actions.card_calls if c["release_id"] == rel_elio]), 1)
                self.assertEqual(len([e for e in actions.eval_reply_calls if e["release_id"] == rel_elio]), 1)
                self.assertEqual(len(triage.calls), 1)

                # 3) Live `release.new` (archived only) -> `action.rejected` (4-form quality cutoff on owned file) -> 1 alt-grab card + 1 eval
                radarr_state["status"] = RadarrStatusResponse(
                    status="owned_file",
                    movie_id=501,
                    file_id=901,
                    existing_quality="Remux-2160p",
                    monitored=True,
                    movie_file_snapshot=owned_snapshot,
                    alternative_grab_configured=True,
                )
                alt_rel = _live_release_obj(
                    name=elio_name, external_id="241889122", title="Elio", year=2025, meta_imdb="tt4919268"
                )
                r_alt1 = client.post("/webhook/test-webhook-secret", json=_live_release_new(alt_rel, evt_id="evt-a1"))
                self.assertEqual(r_alt1.json()["status"], "archived_non_movie")
                self.assertIsNone(r_alt1.json()["release_id"])
                self.assertFalse(asyncio.run(app.state.worker.process_next_queued()))

                r_alt2 = client.post(
                    "/webhook/test-webhook-secret",
                    json=_live_action_rejected(
                        alt_rel,
                        rejections=["Existing file meets cutoff: Remux-2160p"],
                        evt_id="evt-a2",
                    ),
                )
                self.assertEqual(r_alt2.json()["status"], "accepted")
                rel_alt = r_alt2.json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                alt_done = db.get_release(rel_alt)
                assert alt_done is not None
                self.assertEqual(alt_done["state"], "completed")
                self.assertEqual(alt_done["card_revision"], 1)
                self.assertEqual(alt_done["eval_reply_status"], "sent")
                alt_cards = [c for c in actions.card_calls if c["release_id"] == rel_alt]
                alt_replies = [e for e in actions.eval_reply_calls if e["release_id"] == rel_alt]
                self.assertEqual(len(alt_cards), 1)
                self.assertEqual(alt_cards[0]["delivery_kind"], "alternative_grab")
                self.assertEqual(len(alt_replies), 1)

                # 4) Codex corrective case: Radarr action changes (actionable_grab -> alternative_grab)
                #    while movie evaluation evidence is unchanged -> edit card to rev 2 with
                #    preserve_eval_reply=True, ZERO extra /evaluate calls, ZERO extra evaluation replies!
                radarr_state["status"] = RadarrStatusResponse(status="absent", alternative_grab_configured=True)
                triage_before_case4 = len(triage.calls)
                trans_rel = _live_release_obj(
                    name=elio_name, external_id="241889123", title="Elio", year=2025, meta_imdb="tt4919268"
                )
                r_tr1 = client.post(
                    "/webhook/test-webhook-secret",
                    json=_live_action_rejected(trans_rel, rejections=["Unknown Movie"], evt_id="evt-t1"),
                )
                rel_trans = r_tr1.json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(triage.calls), triage_before_case4 + 1)

                # Now Radarr has an existing file and autobrr reports 4-form quality cutoff rejection
                radarr_state["status"] = RadarrStatusResponse(
                    status="owned_file",
                    movie_id=501,
                    file_id=901,
                    existing_quality="Remux-2160p",
                    monitored=True,
                    movie_file_snapshot=owned_snapshot,
                    alternative_grab_configured=True,
                )
                r_tr2 = client.post(
                    "/webhook/test-webhook-secret",
                    json=_live_action_rejected(
                        trans_rel,
                        rejections=["Existing file meets cutoff: Remux-2160p"],
                        evt_id="evt-t2",
                    ),
                )
                self.assertEqual(r_tr2.json()["status"], "updated_correction")
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))

                trans_done = db.get_release(rel_trans)
                assert trans_done is not None
                self.assertEqual(trans_done["state"], "completed")
                self.assertEqual(trans_done["card_revision"], 2)
                self.assertEqual(trans_done["eval_reply_status"], "sent")
                trans_cards = [c for c in actions.card_calls if c["release_id"] == rel_trans]
                trans_replies = [e for e in actions.eval_reply_calls if e["release_id"] == rel_trans]
                self.assertEqual(len(trans_cards), 2)
                self.assertEqual(trans_cards[0]["delivery_kind"], "actionable_grab")
                self.assertEqual(trans_cards[1]["delivery_kind"], "alternative_grab")
                self.assertEqual(trans_cards[1]["card_revision"], 2)
                self.assertTrue(trans_cards[1].get("preserve_eval_reply"))
                # Evaluation MUST be reused: still 1 /evaluate call and 1 Telegram reply!
                self.assertEqual(len(triage.calls), triage_before_case4 + 1)
                self.assertEqual(len(trans_replies), 1)

                # 5) Unresolved path (evidence_review) follow-up action.rejected -> 1 card + 1 eval
                triage_before_case5 = len(triage.calls)
                unres_rel = _live_release_obj(
                    name="Mystery.Unidentified.2025.2160p.UHD.BluRay.REMUX-TRiToN",
                    external_id="241889124",
                    title="Mystery Unidentified",
                    year=2025,
                    meta_imdb=None,
                )
                r_u1 = client.post(
                    "/webhook/test-webhook-secret",
                    json=_live_action_rejected(unres_rel, rejections=["Unknown Movie"], evt_id="evt-u1"),
                )
                rel_unres = r_u1.json()["release_id"]
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(triage.calls), triage_before_case5 + 1)

                r_u2 = client.post(
                    "/webhook/test-webhook-secret",
                    json=_live_action_rejected(
                        unres_rel,
                        rejections=[
                            "Unknown Movie. Unable to match to existing movie in Library using release title."
                        ],
                        evt_id="evt-u2",
                    ),
                )
                self.assertEqual(r_u2.json()["status"], "updated_correction")
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                unres_done = db.get_release(rel_unres)
                assert unres_done is not None
                self.assertEqual(unres_done["state"], "completed")
                self.assertEqual(unres_done["card_revision"], 1)
                self.assertEqual(len([c for c in actions.card_calls if c["release_id"] == rel_unres]), 1)
                self.assertEqual(len([e for e in actions.eval_reply_calls if e["release_id"] == rel_unres]), 1)
                self.assertEqual(len(triage.calls), triage_before_case5 + 1)

            # 6) Pre-migration intake.db cleanly adds last_card_digest / last_eval_reply_digest & backfills
            legacy_db_path = tmpdir / "state" / "legacy-intake.db"
            legacy_db_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(legacy_db_path.parent, 0o700)
            conn = sqlite3.connect(str(legacy_db_path))
            conn.executescript(
                """
                CREATE TABLE releases (
                    release_id TEXT PRIMARY KEY,
                    indexer_norm TEXT NOT NULL,
                    external_id_norm TEXT NOT NULL,
                    exact_release_name TEXT NOT NULL,
                    first_occurrence_id TEXT NOT NULL,
                    latest_occurrence_id TEXT NOT NULL,
                    source_occurrence_id TEXT NOT NULL,
                    card_revision INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    pending_reprocess INTEGER NOT NULL DEFAULT 0,
                    suppression_reason TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    incoming_evidence_digest TEXT NOT NULL,
                    incoming_snapshot_json TEXT NOT NULL,
                    full_evidence_digest TEXT,
                    last_evaluated_digest TEXT,
                    tl_outcome TEXT,
                    identity_gate_status TEXT NOT NULL DEFAULT 'pending',
                    identity_conflict_reason TEXT,
                    verified_tmdb_id INTEGER,
                    verified_imdb_id TEXT,
                    verified_title TEXT,
                    verified_year INTEGER,
                    ack_chat_id TEXT,
                    ack_message_id INTEGER,
                    ack_thread_id INTEGER,
                    ack_incoming_digest TEXT,
                    eval_reply_status TEXT NOT NULL DEFAULT 'pending',
                    pending_card_supersede_json TEXT,
                    pending_eval_reply_json TEXT,
                    radarr_status_snapshot TEXT,
                    evaluation_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(indexer_norm, external_id_norm, exact_release_name)
                );
                INSERT INTO releases (
                    release_id, indexer_norm, external_id_norm, exact_release_name,
                    first_occurrence_id, latest_occurrence_id, source_occurrence_id,
                    card_revision, state, incoming_evidence_digest, incoming_snapshot_json,
                    full_evidence_digest, last_evaluated_digest, eval_reply_status,
                    created_at, updated_at
                ) VALUES (
                    'legacy-rel-1', 'torrentleech', '1001', 'Legacy.2025.2160p-GRP',
                    'occ-1', 'occ-1', 'occ-1',
                    1, 'completed', 'in-dig', '{}',
                    'full-dig-1', 'full-dig-1', 'sent',
                    '2026-11-26T17:00:00Z', '2026-11-26T17:01:00Z'
                );
                """
            )
            conn.commit()
            conn.close()
            os.chmod(legacy_db_path, 0o600)

            migrated_db = IntakeDB(legacy_db_path)
            migrated_db.init_db()
            mig_row = migrated_db.get_release("legacy-rel-1")
            assert mig_row is not None
            self.assertIsNone(mig_row["last_card_digest"])
            self.assertEqual(mig_row["last_eval_reply_digest"], "full-dig-1")

    def test_issue_13_3c_same_year_sequel_and_3b_intake_contradiction(self) -> None:
        """
        Issue #13 §3c & §3b regressions:
          - Invented release 'North Crossing' (2021) with no listing IMDb ID MUST NOT pass Intake's
            identity gate for 'North Crossing II' (2021); must land in evidence_review with 0 buttons.
          - Sequel/part/numeral variants ('II', '2', 'Part 2', 'Chapter 2', 'III') never falsely match.
          - Independently supported TMDB alias overrides cleanly.
          - Downstream _eval_contradicts_movie catches title, year, director, model_identification,
            and catalogue_identification conflicts (not just IDs).
        """
        from services.movie_intake_api.tmdb_verifier import _titles_match
        from services.movie_intake_api.worker import _eval_contradicts_movie

        # Unit matrix for sequel/part/numeral separation
        self.assertFalse(_titles_match("North Crossing", ["North Crossing II"]))
        self.assertFalse(_titles_match("North Crossing", ["North Crossing 2"]))
        self.assertFalse(_titles_match("North Crossing", ["North Crossing Part 2"]))
        self.assertFalse(_titles_match("North Crossing", ["North Crossing Chapter 2"]))
        self.assertFalse(_titles_match("North Crossing II", ["North Crossing III"]))
        self.assertFalse(_titles_match("North Crossing Part 1", ["North Crossing Part 2"]))
        # Equivalent numeral notations and explicit TMDB alias match
        self.assertTrue(_titles_match("North Crossing 2", ["North Crossing II"]))
        self.assertTrue(_titles_match("North Crossing Part Two", ["North Crossing Part 2"]))
        self.assertTrue(_titles_match("North Crossing", ["North Crossing II", "North Crossing"]))

        # End-to-end Intake pipeline: North Crossing (2021) without listing IMDb -> evaluator proposes North Crossing II (2021)
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            sequel = TmdbMovieRecord(
                tmdb_id=8820212,
                title="North Crossing II",
                original_title="North Crossing II",
                year=2021,
                imdb_id="tt8820212",
                runtime_minutes=115,
                director="Elena Rostova",
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {sequel.tmdb_id: sequel}, {sequel.imdb_id: [sequel.tmdb_id]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda _ext, _n: TorrentLeechResult(
                    outcome="listing_id_missing",
                    imdb_id=None,
                    file_runtime_minutes=115,
                    audio_languages=("English",),
                ),
            )
            triage = StubTriageClient(
                lambda _body: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="North Crossing II",
                        year=2021,
                        imdb_id="tt8820212",
                        tmdb_id=8820212,
                        confidence="high",
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda *_: RadarrStatusResponse(status="absent"))
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
                r = client.post(
                    "/webhook/test-webhook-secret",
                    json=_make_autobrr_movie_webhook(
                        release_name="North.Crossing.2021.2160p.UHD.BluRay.REMUX-GROUP",
                        external_id="882021",
                        title="North Crossing",
                        year=2021,
                        meta_imdb=None,
                        meta_tmdb=None,
                    ),
                )
                self.assertEqual(r.status_code, 200)
                self.assertTrue(asyncio.run(app.state.worker.process_next_queued()))
                self.assertEqual(len(actions.card_calls), 1)
                card = actions.card_calls[0]
                self.assertEqual(card["delivery_kind"], "evidence_review")
                self.assertEqual(card["identity_gate_status"], "evidence_review")
                self.assertNotIn("verified_tmdb_id", card)
                self.assertIn("does not match", str(card.get("conflict_reason") or "").lower())

        # §3b downstream _eval_contradicts_movie checks beyond IDs
        verified_film = TmdbMovieRecord(
            tmdb_id=50202,
            title="Silent Valley",
            original_title="Silent Valley",
            year=2024,
            imdb_id="tt5020200",
            runtime_minutes=104,
            director="David Kowalski",
        )
        # Same IDs substituted onto wrong title/director -> MUST contradict!
        swapped_eval = _make_eval_payload(
            title="Paper Harbor",
            year=2024,
            imdb_id="tt5020200",
            tmdb_id=50202,
        )
        swapped_eval["identification"]["director"] = "David Mercer"
        contradiction = _eval_contradicts_movie(swapped_eval, verified_film)
        self.assertIsNotNone(contradiction)

        # Same title/IDs but director shares only first name -> MUST contradict!
        dir_mismatch_eval = _make_eval_payload(
            title="Silent Valley",
            year=2024,
            imdb_id="tt5020200",
            tmdb_id=50202,
        )
        dir_mismatch_eval["identification"]["director"] = "David Mercer"
        self.assertIsNotNone(_eval_contradicts_movie(dir_mismatch_eval, verified_film))

        # Explicit catalogue_identification.compatible == False -> MUST contradict!
        cat_conflict_eval = _make_eval_payload(
            title="Silent Valley",
            year=2024,
            imdb_id="tt5020200",
            tmdb_id=50202,
        )
        cat_conflict_eval["identification"]["director"] = "David Kowalski"
        cat_conflict_eval["catalogue_identification"] = {
            "source": "tmdb",
            "canonical_title": "Silent Valley",
            "tmdb_id": 50202,
            "compatible": False,
            "conflict_reason": "contradictory TMDB IDs",
        }
        self.assertIsNotNone(_eval_contradicts_movie(cat_conflict_eval, verified_film))

        # Agreeing title, year, director, and IDs -> None (no contradiction)
        agree_eval = _make_eval_payload(
            title="Silent Valley",
            year=2024,
            imdb_id="tt5020200",
            tmdb_id=50202,
        )
        agree_eval["identification"]["director"] = "David Kowalski"
        self.assertIsNone(_eval_contradicts_movie(agree_eval, verified_film))

    def test_issue_13_2a_late_quality_rejection_and_radarr_suppression_retire_delivered_card(self) -> None:
        """Issue #13 §2a: Later quality rejection or Radarr suppression edits delivered card to retired."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=770011,
                title="Harbor Dusk",
                original_title="Harbor Dusk",
                year=2025,
                imdb_id="tt7700110",
                runtime_minutes=118,
                poster_path="/harbor_dusk.jpg",
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {770011: movie}, {"tt7700110": [770011]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda *_: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt7700110",
                    file_runtime_minutes=118,
                ),
            )
            triage = StubTriageClient(
                lambda _: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Harbor Dusk",
                        year=2025,
                        imdb_id="tt7700110",
                        tmdb_id=770011,
                    ),
                )
            )
            radarr_state = {"status": "absent"}
            actions = StubActionsClient(
                radarr_handler=lambda *_: RadarrStatusResponse(status=radarr_state["status"])
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

            rel_name = "Harbor.Dusk.2025.2160p.UHD.BluRay.REMUX.HEVC-GROUP"
            base_payload = _make_autobrr_movie_webhook(
                external_id="770011",
                release_name=rel_name,
                title="Harbor Dusk",
                year=2025,
                meta_imdb="tt7700110",
                rejections=["Unknown Movie"],
            )
            late_quality_payload = _make_autobrr_movie_webhook(
                external_id="770011",
                release_name=rel_name,
                title="Harbor Dusk",
                year=2025,
                meta_imdb="tt7700110",
                rejections=["Quality is not wanted in profile"],
            )

            with TestClient(app) as client:
                # 1. Initial proposal delivered with actionable_grab rev 1 + 1 eval reply
                r1 = client.post(f"/webhook/{cfg.webhook_secret}", json=base_payload)
                self.assertEqual(r1.status_code, 200)
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(len(actions.card_calls), 1)
                self.assertEqual(actions.card_calls[0]["delivery_kind"], "actionable_grab")
                self.assertEqual(actions.card_calls[0]["card_revision"], 1)
                self.assertEqual(len(actions.eval_reply_calls), 1)

                # 2. Later "Quality is not wanted in profile" MUST retire card (rev 2) with preserve_eval_reply=True
                r2 = client.post(f"/webhook/{cfg.webhook_secret}", json=late_quality_payload)
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(r2.json()["status"], "updated_correction")
                asyncio.run(app.state.worker.process_next_queued())

                self.assertEqual(len(actions.card_calls), 2)
                retired_card = actions.card_calls[1]
                self.assertEqual(retired_card["delivery_kind"], "retired")
                self.assertEqual(retired_card["card_revision"], 2)
                self.assertTrue(retired_card.get("preserve_eval_reply"))
                self.assertIn("Quality is not wanted in profile", str(retired_card.get("radarr_note") or ""))
                # Existing eval reply is preserved; zero duplicate eval replies sent
                self.assertEqual(len(actions.eval_reply_calls), 1)

                rel_row = db.get_release(r1.json()["release_id"])
                assert rel_row is not None
                self.assertEqual(rel_row["state"], "suppressed")
                self.assertEqual(rel_row["suppression_reason"], "quality_rejected")
                self.assertEqual(rel_row["card_revision"], 2)

                # 3. Second release: Radarr transitions to watched_unmonitored before later correction arrives -> retires card
                rel2_name = "Harbor.Dusk.2025.1080p.BluRay.REMUX.AVC-GROUP2"
                p2_initial = _make_autobrr_movie_webhook(
                    external_id="770012",
                    release_name=rel2_name,
                    title="Harbor Dusk",
                    year=2025,
                    meta_imdb="tt7700110",
                    category="Movies :: Bluray",
                    resolution="1080p",
                    source="BluRay",
                    rejections=["Unknown Movie"],
                )
                r3 = client.post(f"/webhook/{cfg.webhook_secret}", json=p2_initial)
                self.assertEqual(r3.status_code, 200)
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "actionable_grab")

                radarr_state["status"] = "watched_unmonitored"
                p2_followup = _make_autobrr_movie_webhook(
                    external_id="770012",
                    release_name=rel2_name,
                    title="Harbor Dusk",
                    year=2025,
                    meta_imdb="tt7700110",
                    category="Movies :: Bluray",
                    resolution="1080p",
                    source="BluRay",
                    freeleech=True,
                    rejections=["Unknown Movie"],
                )
                r4 = client.post(f"/webhook/{cfg.webhook_secret}", json=p2_followup)
                self.assertEqual(r4.status_code, 200)
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(actions.card_calls[-1]["delivery_kind"], "retired")
                self.assertEqual(actions.card_calls[-1]["card_revision"], 2)
                self.assertTrue(actions.card_calls[-1].get("preserve_eval_reply"))
                rel2_row = db.get_release(r3.json()["release_id"])
                assert rel2_row is not None
                self.assertEqual(rel2_row["state"], "suppressed")
                self.assertEqual(rel2_row["suppression_reason"], "watched_unmonitored")

    def test_issue_13_4a_replay_of_superseded_evidence_preserves_newer_correction(self) -> None:
        """Issue #13 §4a: old A -> corrected B -> repeated A archives all 3 and keeps B (queued, posted, restarted)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie_b = TmdbMovieRecord(
                tmdb_id=880022,
                title="Winter Signal",
                original_title="Winter Signal",
                year=2025,
                imdb_id="tt8800220",
                runtime_minutes=110,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {880022: movie_b}, {"tt8800220": [880022]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda *_: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt8800220",
                    file_runtime_minutes=110,
                ),
            )
            triage = StubTriageClient(
                lambda _: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="Winter Signal",
                        year=2025,
                        imdb_id="tt8800220",
                        tmdb_id=880022,
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda *_: RadarrStatusResponse(status="absent"))

            rel_name = "Winter.Signal.2025.2160p.UHD.BluRay.REMUX.HEVC-GROUP"
            payload_a = _make_autobrr_movie_webhook(
                external_id="880022",
                release_name=rel_name,
                title="Winter Signal",
                year=2025,
                meta_imdb="tt1111111",  # old wrong IMDb A
                rejections=["Unknown Movie"],
            )
            payload_b = _make_autobrr_movie_webhook(
                external_id="880022",
                release_name=rel_name,
                title="Winter Signal",
                year=2025,
                meta_imdb="tt8800220",  # corrected IMDb B
                rejections=["Unknown Movie"],
            )

            app1 = create_app(
                cfg,
                db=db,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )
            with TestClient(app1) as client:
                # 1) While queued: old A -> corrected B -> repeated A
                r_a1 = client.post(f"/webhook/{cfg.webhook_secret}", json=payload_a)
                self.assertEqual(r_a1.status_code, 200)
                self.assertEqual(r_a1.json()["status"], "accepted")
                release_id = r_a1.json()["release_id"]

                r_b = client.post(f"/webhook/{cfg.webhook_secret}", json=payload_b)
                self.assertEqual(r_b.status_code, 200)
                self.assertEqual(r_b.json()["status"], "updated_correction")
                self.assertEqual(r_b.json()["release_id"], release_id)

                r_a2 = client.post(f"/webhook/{cfg.webhook_secret}", json=payload_a)
                self.assertEqual(r_a2.status_code, 200)
                self.assertEqual(r_a2.json()["status"], "coalesced_duplicate")

                # All 3 raw archives exist AND queued release still holds corrected B (tt8800220)
                archives_after_queued = list(cfg.webhook_archive_dir.rglob("*.json"))
                self.assertEqual(len(archives_after_queued), 3)
                rel_q = db.get_release(release_id)
                assert rel_q is not None
                snap_q = json.loads(str(rel_q["incoming_snapshot_json"]))
                self.assertEqual(snap_q["meta_imdb"], "tt8800220")
                self.assertEqual(rel_q["state"], "queued")

                # 2) Worker posts verified card for B, then repeated A arrives again
                asyncio.run(app1.state.worker.process_next_queued())
                self.assertEqual(len(actions.card_calls), 1)
                self.assertEqual(actions.card_calls[0]["delivery_kind"], "actionable_grab")
                self.assertEqual(actions.card_calls[0]["verified_imdb_id"], "tt8800220")

                r_a3 = client.post(f"/webhook/{cfg.webhook_secret}", json=payload_a)
                self.assertEqual(r_a3.status_code, 200)
                self.assertEqual(r_a3.json()["status"], "coalesced_duplicate")
                processed_after_a3 = asyncio.run(app1.state.worker.process_next_queued())
                self.assertFalse(processed_after_a3)
                self.assertEqual(len(actions.card_calls), 1)
                rel_done = db.get_release(release_id)
                assert rel_done is not None
                self.assertEqual(rel_done["state"], "completed")
                self.assertEqual(json.loads(str(rel_done["incoming_snapshot_json"]))["meta_imdb"], "tt8800220")

            # 3) After restart (fresh IntakeDB & app instance on same SQLite DB): repeated A still ignored!
            db_restarted = IntakeDB(cfg.intake_db_path)
            db_restarted.init_db()
            app2 = create_app(
                cfg,
                db=db_restarted,
                tl_scraper=tl,
                tmdb_client=tmdb,
                triage_client=triage,
                actions_client=actions,
                start_worker=False,
            )
            with TestClient(app2) as client2:
                r_a4 = client2.post(f"/webhook/{cfg.webhook_secret}", json=payload_a)
                self.assertEqual(r_a4.status_code, 200)
                self.assertEqual(r_a4.json()["status"], "coalesced_duplicate")
                self.assertFalse(asyncio.run(app2.state.worker.process_next_queued()))
                self.assertEqual(len(list(cfg.webhook_archive_dir.rglob("*.json"))), 5)
                rel_post_restart = db_restarted.get_release(release_id)
                assert rel_post_restart is not None
                self.assertEqual(rel_post_restart["state"], "completed")
                self.assertEqual(
                    json.loads(str(rel_post_restart["incoming_snapshot_json"]))["meta_imdb"],
                    "tt8800220",
                )

    def test_issue_13_4b_malformed_rejections_and_missing_action_type_disable_alternative_grab(self) -> None:
        """Issue #13 §4b: Missing action type or unreadable/mixed rejection list must disable Alternative Grab."""
        from services.movie_intake_api.classifier import ClassifiedWebhook

        rel_name = "Harbor.Dusk.2025.2160p.UHD.BluRay.REMUX.HEVC-GROUP"
        valid_dl = "https://www.torrentleech.org/rss/download/770099/deadbeef0123456789/Harbor.Dusk.2025.torrent"

        # 1) Missing action.type -> never treated as Radarr
        missing_action_payload = _make_autobrr_movie_webhook(
            external_id="770099",
            release_name=rel_name,
            title="Harbor Dusk",
            year=2025,
            download_url=valid_dl,
            rejections=["Existing file meets cutoff: Remux-2160p"],
        )
        missing_action_payload["data"].pop("action", None)
        cw_missing = classify_webhook_payload(missing_action_payload)
        self.assertEqual(cw_missing.classification, "non_movie_archive_only")
        self.assertFalse(cw_missing.action_is_radarr)

        # 2) Mixed rejection lists containing unreadable / non-string / blank items MUST set
        # has_malformed_rejections=True and disable BOTH legacy_alt_grab_eligible AND quality_rejected_suppress!
        malformed_cases: list[Any] = [
            ["Existing file meets cutoff: Remux-2160p", 12345],
            ["Existing file meets cutoff: Remux-2160p", None],
            ["Existing file meets cutoff: Remux-2160p", {"nested": "bad"}],
            ["Existing file meets cutoff: Remux-2160p", "   "],
            {"unexpected": "dict"},
            "",
        ]
        for bad_rej in malformed_cases:
            payload = _make_autobrr_movie_webhook(
                external_id="770099",
                release_name=rel_name,
                title="Harbor Dusk",
                year=2025,
                download_url=valid_dl,
                rejections=["Existing file meets cutoff: Remux-2160p"],
            )
            payload["data"]["result"]["rejections"] = bad_rej
            cw = classify_webhook_payload(payload)
            self.assertEqual(cw.classification, "movie_candidate")
            self.assertTrue(cw.has_malformed_rejections, f"Expected malformed flag for {bad_rej!r}")
            self.assertEqual(
                classify_rejection_reasons(cw),
                ("needs_identity_and_radarr_check", None),
                f"Malformed rejection list {bad_rej!r} must disable Alternative Grab & quality suppress",
            )
            # Round-trip through sanitized snapshot preserves has_malformed_rejections
            rt = ClassifiedWebhook.from_sanitized_snapshot(cw.to_sanitized_snapshot(), cw.incoming_evidence_digest)
            self.assertTrue(rt.has_malformed_rejections)
            self.assertEqual(classify_rejection_reasons(rt), ("needs_identity_and_radarr_check", None))

    def test_issue_13_5a_6a_6b_privacy_boundaries_worker_log_and_bounded_evaluator_errors(self) -> None:
        """Issue #13 §5a, §6a, §6b: SQLite snapshot & outbound sanitization, safe worker error logs, card evidence, bounded errors."""
        import logging

        from services.movie_intake_api.clients import MAX_EVALUATOR_ERROR_CHARS, TriageApiClient
        from services.movie_intake_api.security import SecretRedactingLogFilter
        from services.movie_intake_api.worker import build_triage_evaluate_request

        # 1. §5a: Free-form webhook fields with harmless secret markers stay in 0600 raw archive ONLY,
        # NEVER in SQLite (intake.db / -wal) or outbound /evaluate requests!
        secret_markers = [
            "MK_NAME_PASS",
            "MK_TITLE_KEY",
            "MK_CAT_TOK",
            "MK_GRP_COOK",
            "MK_HDR_URL",
            "MK_REJ_AIZA",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cfg = _make_config(tmpdir)
            db = IntakeDB(cfg.intake_db_path)
            db.init_db()

            movie = TmdbMovieRecord(
                tmdb_id=765432,
                title="The Secret Agent",
                original_title="The Secret Agent",
                year=2025,
                imdb_id="tt7654321",
                runtime_minutes=120,
            )
            tmdb = StubTmdbClient(cfg.tmdb_cache_path, {765432: movie}, {"tt7654321": [765432]})
            tl = StubTlScraper(
                db,
                cfg.tl_cookie_file,
                lambda *_: TorrentLeechResult(
                    outcome="listing_id_found",
                    imdb_id="tt7654321",
                    file_runtime_minutes=120,
                ),
            )
            triage = StubTriageClient(
                lambda _: TriageEvalResult(
                    ok=True,
                    payload=_make_eval_payload(
                        title="The Secret Agent",
                        year=2025,
                        imdb_id="tt7654321",
                        tmdb_id=765432,
                    ),
                )
            )
            actions = StubActionsClient(radarr_handler=lambda *_: RadarrStatusResponse(status="absent"))

            payload = _make_autobrr_movie_webhook(
                external_id="765432",
                release_name="The.Secret.Agent.2025.2160p.UHD.BluRay.REMUX.HEVC-GRP passkey=MK_NAME_PASS",
                title="The Secret Agent api_key=MK_TITLE_KEY",
                year=2025,
                category="Movies :: 4K token=MK_CAT_TOK",
                meta_imdb="tt7654321",
                rejections=["Unknown Movie AIzaSyMK_REJ_AIZA_123456789012345"],
            )
            payload["data"]["release"]["group"] = "GRP cookie=MK_GRP_COOK"
            payload["data"]["release"]["hdr"] = ["HDR10 https://tracker.invalid/MK_HDR_URL"]
            raw_bytes = json.dumps(payload).encode("utf-8")

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
                r = client.post(
                    f"/webhook/{cfg.webhook_secret}",
                    content=raw_bytes,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["status"], "accepted")

                # Raw private archive retains exact original bytes
                archives = list(cfg.webhook_archive_dir.rglob("*.json"))
                self.assertEqual(len(archives), 1)
                self.assertEqual(archives[0].read_bytes(), raw_bytes)

                # SQLite database + WAL contain ZERO secret markers!
                db_blob = cfg.intake_db_path.read_bytes()
                wal_path = Path(f"{cfg.intake_db_path}-wal")
                if wal_path.exists():
                    db_blob += wal_path.read_bytes()
                for marker in secret_markers:
                    self.assertNotIn(marker.encode("utf-8"), db_blob, f"Marker {marker} leaked to SQLite!")

                # Process worker & check outbound /evaluate payload AND §6a card evidence fields
                asyncio.run(app.state.worker.process_next_queued())
                self.assertEqual(len(triage.calls), 1)
                eval_serialized = json.dumps(triage.calls[0])
                for marker in secret_markers:
                    self.assertNotIn(marker, eval_serialized, f"Marker {marker} leaked to /evaluate payload!")
                # Title "The Secret Agent" itself was NOT mangled
                self.assertIn("The Secret Agent", eval_serialized)

                self.assertEqual(len(actions.card_calls), 1)
                card = actions.card_calls[0]
                self.assertEqual(card["external_id"], "765432")
                self.assertTrue(card.get("checks_passed"))
                self.assertIn("tt7654321", str(card.get("accepted_evidence") or ""))
                self.assertIn("765432", str(card.get("accepted_evidence") or ""))

        # 2. §5a: Worker loop exception logging MUST NOT emit raw unsanitized exception traceback!
        class _CaptureHandler(logging.Handler):
            def __init__(self) -> None:
                super().__init__()
                self.formatted_records: list[str] = []
                self.raw_exc_infos: list[Any] = []
                self.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))

            def emit(self, record: logging.LogRecord) -> None:
                self.raw_exc_infos.append(record.exc_info)
                self.formatted_records.append(self.format(record))

        worker_logger = logging.getLogger("services.movie_intake_api.worker")
        cap = _CaptureHandler()
        redactor = SecretRedactingLogFilter()
        cap.addFilter(redactor)
        worker_logger.addHandler(cap)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_w = _make_config(Path(tmp))
                db_w = IntakeDB(cfg_w.intake_db_path)
                db_w.init_db()
                app_w = create_app(cfg_w, db=db_w, start_worker=False)
                worker = app_w.state.worker

                async def _boom() -> bool:
                    worker._stop.set()
                    raise RuntimeError(
                        "Failed https://tracker.invalid/dl?passkey=EXC_URL_SECRET_999 token=EXC_RAW_MARKER_888"
                    )

                worker.process_next_queued = _boom  # type: ignore[method-assign]
                asyncio.run(worker._run_loop())
        finally:
            worker_logger.removeHandler(cap)

        self.assertGreaterEqual(len(cap.formatted_records), 1)
        joined_logs = "\n".join(cap.formatted_records)
        self.assertNotIn("EXC_URL_SECRET_999", joined_logs)
        self.assertNotIn("EXC_RAW_MARKER_888", joined_logs)
        self.assertIn("RuntimeError", joined_logs)
        self.assertTrue(all(ei is None for ei in cap.raw_exc_infos))

        # 3. §6b: TriageApiClient retains full bounded sanitized evaluator error (not clipped at 240 chars)
        # and adds explicit truncation label if safety bound is exceeded
        tail_body = ("diagnostic context " * 80) + "api_key=ERR_SECRET_777 END_DIAGNOSTIC_MARKER_42"
        huge_body = ("overflow error block " * 1200) + "passkey=HUGE_SECRET_666"
        resp_mode = {"body": tail_body}

        def _triage_err_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, text=resp_mode["body"])

        async def _run_err_checks() -> tuple[TriageEvalResult, TriageEvalResult]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(_triage_err_handler)) as hc:
                tc = TriageApiClient(base_url="http://127.0.0.1:40716", internal_token="tok", http_client=hc)
                res_medium = await tc.evaluate({"raw_text": "x"})
                resp_mode["body"] = huge_body
                res_huge = await tc.evaluate({"raw_text": "x"})
                return res_medium, res_huge

        res_med, res_huge = asyncio.run(_run_err_checks())
        self.assertFalse(res_med.ok)
        assert res_med.error_redacted is not None
        self.assertIn("END_DIAGNOSTIC_MARKER_42", res_med.error_redacted)
        self.assertNotIn("ERR_SECRET_777", res_med.error_redacted)
        self.assertNotIn("[truncated:", res_med.error_redacted)

        self.assertFalse(res_huge.ok)
        assert res_huge.error_redacted is not None
        self.assertLessEqual(len(res_huge.error_redacted), MAX_EVALUATOR_ERROR_CHARS)
        self.assertIn("[truncated:", res_huge.error_redacted)
        self.assertNotIn("HUGE_SECRET_666", res_huge.error_redacted)


if __name__ == "__main__":
    unittest.main()
