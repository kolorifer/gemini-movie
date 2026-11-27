from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .archive import ArchiveIntegrityError, ArchiveReceipt, read_verified_archive_json
from .classifier import (
    DEFAULT_ALLOWED_FILTER_NAMES,
    ClassifiedWebhook,
    _evaluate_remux_quality_eligibility,
    classify_webhook_payload,
    compute_legacy_occurrence_evidence_digest,
)
from .security import PrivateFileError, ensure_private_dir


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso(dt: Optional[datetime] = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def generate_uuid7() -> str:
    """Generate a time-ordered RFC 9562 UUIDv7 string using os/secrets entropy."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = int.from_bytes(secrets.token_bytes(2), "big") & 0x0FFF
    rand_b = int.from_bytes(secrets.token_bytes(8), "big") & ((1 << 62) - 1)
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=value))


@dataclass(frozen=True)
class IntakeClaimResult:
    status: str  # accepted | coalesced_duplicate | updated_correction | archived_non_movie | diagnostic_only
    occurrence_id: str
    release_id: Optional[str] = None
    card_revision: int = 0


@dataclass(frozen=True)
class OccurrenceArchiveRef:
    occurrence_id: str
    release_id: str
    archive_relpath: str
    archive_sha256: str
    received_at: str
    indexer_norm: str
    external_id_norm: str
    exact_release_name: str


class IntakeDB:
    """
    Durable owner-private SQLite WAL database for movie-intake-api.
    Stores ONLY sanitized metadata, archive pointers + SHA-256 digests, and workflow state.
    NEVER stores download_url, torrent passkeys, cookies, or raw webhook payloads.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(os.path.abspath(str(db_path)))

    def _ensure_private_db_paths(self) -> None:
        ensure_private_dir(self.db_path.parent)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            try:
                st = os.lstat(str(candidate))
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode):
                raise PrivateFileError(f"Refusing symlinked database file: {candidate}")
            if not stat.S_ISREG(st.st_mode):
                raise PrivateFileError(f"SQLite path is not a regular file: {candidate}")
            if st.st_uid != os.getuid():
                raise PrivateFileError(f"SQLite file not owned by current user: {candidate}")
        try:
            fd = os.open(
                str(self.db_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(fd)
        except FileExistsError:
            pass

    def _harden_db_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.db_path}{suffix}")
            try:
                st = os.lstat(str(candidate))
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            if st.st_uid == os.getuid() and (st.st_mode & 0o077) != 0:
                try:
                    os.chmod(str(candidate), 0o600)
                except FileNotFoundError:
                    continue
                except OSError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        self._ensure_private_db_paths()
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        self._harden_db_files()
        return conn

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS releases (
                    release_id                 TEXT PRIMARY KEY,
                    indexer_norm               TEXT NOT NULL,
                    external_id_norm           TEXT NOT NULL,
                    exact_release_name         TEXT NOT NULL,
                    first_occurrence_id        TEXT NOT NULL,
                    latest_occurrence_id       TEXT NOT NULL,
                    source_occurrence_id       TEXT NOT NULL,
                    card_revision              INTEGER NOT NULL DEFAULT 0,
                    state                      TEXT NOT NULL,
                    pending_reprocess          INTEGER NOT NULL DEFAULT 0,
                    suppression_reason         TEXT,
                    lease_owner                TEXT,
                    lease_expires_at           TEXT,
                    retry_count                INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at            TEXT,
                    incoming_evidence_digest   TEXT NOT NULL,
                    incoming_snapshot_json     TEXT NOT NULL,
                    full_evidence_digest       TEXT,
                    last_evaluated_digest      TEXT,
                    tl_outcome                 TEXT,
                    identity_gate_status       TEXT NOT NULL DEFAULT 'pending',
                    identity_conflict_reason   TEXT,
                    verified_tmdb_id           INTEGER,
                    verified_imdb_id           TEXT,
                    verified_title             TEXT,
                    verified_year              INTEGER,
                    ack_chat_id                TEXT,
                    ack_message_id             INTEGER,
                    ack_thread_id              INTEGER,
                    ack_incoming_digest        TEXT,
                    last_card_digest           TEXT,
                    last_eval_reply_digest     TEXT,
                    eval_reply_status          TEXT NOT NULL DEFAULT 'pending',
                    pending_card_supersede_json TEXT,
                    pending_eval_reply_json    TEXT,
                    radarr_status_snapshot     TEXT,
                    evaluation_json            TEXT,
                    created_at                 TEXT NOT NULL,
                    updated_at                 TEXT NOT NULL,
                    UNIQUE(indexer_norm, external_id_norm, exact_release_name)
                );

                CREATE TABLE IF NOT EXISTS occurrences (
                    occurrence_id            TEXT PRIMARY KEY,
                    received_at              TEXT NOT NULL,
                    archive_relpath          TEXT NOT NULL UNIQUE,
                    archive_sha256           TEXT NOT NULL,
                    payload_bytes            INTEGER NOT NULL,
                    classification           TEXT NOT NULL,
                    diagnostic_note          TEXT,
                    release_id               TEXT REFERENCES releases(release_id),
                    incoming_evidence_digest TEXT
                );

                CREATE TABLE IF NOT EXISTS tl_cooldown (
                    singleton_id         INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    cooldown_until       TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_reason          TEXT,
                    updated_at           TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_releases_queue
                    ON releases(state, next_attempt_at, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_occurrences_release
                    ON occurrences(release_id, received_at);
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_cols = {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(releases)").fetchall()
                }
                if "suppression_reason" not in existing_cols:
                    conn.execute("ALTER TABLE releases ADD COLUMN suppression_reason TEXT;")
                if "last_card_digest" not in existing_cols:
                    conn.execute("ALTER TABLE releases ADD COLUMN last_card_digest TEXT;")
                if "last_eval_reply_digest" not in existing_cols:
                    conn.execute("ALTER TABLE releases ADD COLUMN last_eval_reply_digest TEXT;")
                    conn.execute(
                        """
                        UPDATE releases
                        SET last_eval_reply_digest = last_evaluated_digest
                        WHERE last_eval_reply_digest IS NULL
                          AND eval_reply_status IN ('sent', 'ambiguous_reply')
                          AND last_evaluated_digest IS NOT NULL
                          AND last_evaluated_digest = full_evidence_digest
                        """
                    )
                existing_occ_cols = {
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(occurrences)").fetchall()
                }
                if "incoming_evidence_digest" not in existing_occ_cols:
                    conn.execute("ALTER TABLE occurrences ADD COLUMN incoming_evidence_digest TEXT;")
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_occurrences_release_digest
                        ON occurrences(release_id, incoming_evidence_digest);
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_occurrences_release_sha256
                        ON occurrences(release_id, archive_sha256);
                    """
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        self._harden_db_files()

    def backfill_legacy_occurrence_digests(
        self,
        archive_dir: Path,
        *,
        allowed_filter_names: tuple[str, ...] = DEFAULT_ALLOWED_FILTER_NAMES,
        allowed_filter_ids: tuple[int, ...] = (),
    ) -> int:
        """
        PR #18 Blocker 3 & Round 4 Findings 3/4: Recompute `occurrences.incoming_evidence_digest`
        from verified archived payloads for any pre-upgrade movie occurrences whose digest is NULL,
        classify each intact occurrence individually under current notification scope rules
        (keeping eligible ones as `movie_candidate` and marking proven excluded ones as
        `pre_upgrade_ineligible_scope` without erasing proven eligible history), and sync
        `releases.incoming_evidence_digest` for the release's `latest_occurrence_id`.
        """
        conn = self._connect()
        try:
            pending = conn.execute(
                """
                SELECT o.occurrence_id, o.release_id, o.archive_relpath, o.archive_sha256,
                       o.classification, r.latest_occurrence_id
                FROM occurrences o
                JOIN releases r ON r.release_id = o.release_id
                WHERE o.release_id IS NOT NULL
                  AND o.incoming_evidence_digest IS NULL
                ORDER BY o.received_at ASC, o.occurrence_id ASC
                """
            ).fetchall()
        finally:
            conn.close()

        if not pending:
            return 0

        updates: list[tuple[str, str, Optional[str], str, str, bool]] = []
        for row in pending:
            occ_id = str(row["occurrence_id"])
            rel_id = str(row["release_id"])
            relpath = str(row["archive_relpath"])
            expected_sha = str(row["archive_sha256"])
            cur_cls = str(row["classification"] or "movie_candidate")
            is_latest = occ_id == str(row["latest_occurrence_id"])
            try:
                raw_payload = read_verified_archive_json(archive_dir, relpath, expected_sha)
            except (ArchiveIntegrityError, PrivateFileError, OSError, ValueError):
                continue
            digest = compute_legacy_occurrence_evidence_digest(raw_payload)
            if not digest:
                continue
            new_cls = cur_cls
            new_diag: Optional[str] = None
            if cur_cls == "movie_candidate":
                reclassified = classify_webhook_payload(
                    raw_payload,
                    allowed_filter_names=allowed_filter_names,
                    allowed_filter_ids=allowed_filter_ids,
                )
                if reclassified.classification != "movie_candidate":
                    new_cls = "pre_upgrade_ineligible_scope"
                    new_diag = reclassified.diagnostic_note or "pre_upgrade_ineligible_scope"
            updates.append((digest, new_cls, new_diag, occ_id, rel_id, is_latest))

        if not updates:
            return 0

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for digest, new_cls, new_diag, occ_id, rel_id, is_latest in updates:
                conn.execute(
                    """
                    UPDATE occurrences
                    SET incoming_evidence_digest = ?,
                        classification = ?,
                        diagnostic_note = COALESCE(diagnostic_note, ?)
                    WHERE occurrence_id = ? AND incoming_evidence_digest IS NULL
                    """,
                    (digest, new_cls, new_diag, occ_id),
                )
                if is_latest:
                    conn.execute(
                        """
                        UPDATE releases
                        SET incoming_evidence_digest = ?
                        WHERE release_id = ? AND latest_occurrence_id = ?
                        """,
                        (digest, rel_id, occ_id),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return len(updates)

    def is_release_currently_eligible(
        self,
        release_id: str,
        archive_dir: Path,
        *,
        allowed_filter_names: tuple[str, ...] = DEFAULT_ALLOWED_FILTER_NAMES,
        allowed_filter_ids: tuple[int, ...] = (),
    ) -> tuple[bool, Optional[str]]:
        """
        PR #18 Round 2 Blocker 1 & Round 3 Finding 2: Re-check an actionable release
        strictly against the verified archived webhook for `latest_occurrence_id` (the exact
        occurrence whose snapshot the worker uses) under current notification scope rules
        (qualifying Radarr action event, allowed autobrr filter name/ID, movie category/type,
        and 1080p/2160p Blu-ray REMUX). Fails closed if that occurrence's archive is missing
        or fails integrity verification (cannot prove filter admission from snapshot alone).
        """
        conn = self._connect()
        try:
            rel = conn.execute(
                """
                SELECT release_id, latest_occurrence_id, incoming_evidence_digest, incoming_snapshot_json
                FROM releases
                WHERE release_id = ?
                """,
                (release_id,),
            ).fetchone()
            if rel is None:
                return True, None
            latest_occ_id = str(rel["latest_occurrence_id"] or "")
            latest_occ = (
                conn.execute(
                    """
                    SELECT occurrence_id, archive_relpath, archive_sha256
                    FROM occurrences
                    WHERE occurrence_id = ? AND release_id = ?
                    """,
                    (latest_occ_id, release_id),
                ).fetchone()
                if latest_occ_id
                else None
            )
            has_any_occ = (
                latest_occ is not None
                or conn.execute(
                    "SELECT 1 FROM occurrences WHERE release_id = ? LIMIT 1",
                    (release_id,),
                ).fetchone()
                is not None
            )
        finally:
            conn.close()

        if latest_occ is not None:
            relpath = str(latest_occ["archive_relpath"] or "")
            expected_sha = str(latest_occ["archive_sha256"] or "")
            if not relpath or not expected_sha:
                return False, "unverified_occurrence_archive"
            try:
                raw_payload = read_verified_archive_json(archive_dir, relpath, expected_sha)
            except (ArchiveIntegrityError, PrivateFileError, OSError, ValueError):
                # Cannot prove filter admission when the active occurrence's archive is missing/corrupt
                return False, "unverified_occurrence_archive"
            reclassified = classify_webhook_payload(
                raw_payload,
                allowed_filter_names=allowed_filter_names,
                allowed_filter_ids=allowed_filter_ids,
            )
            if reclassified.classification == "movie_candidate":
                return True, None
            return False, reclassified.diagnostic_note or "pre_upgrade_ineligible_scope"

        if has_any_occ:
            return False, "missing_latest_occurrence_record"

        # Pure synthetic row with no occurrence records at all (unit-test fixture only):
        # still reject obviously ineligible snapshots.
        raw_snap = rel["incoming_snapshot_json"]
        if isinstance(raw_snap, str) and raw_snap.strip():
            try:
                snap = json.loads(raw_snap)
            except ValueError:
                snap = None
            if isinstance(snap, dict) and str(snap.get("exact_release_name") or "").strip():
                classified = ClassifiedWebhook.from_sanitized_snapshot(
                    snap,
                    str(rel["incoming_evidence_digest"] or ""),
                )
                if not classified.action_is_radarr or not classified.action_rejected:
                    return False, "pre_upgrade_ineligible_action"
                q_ok, q_reason = _evaluate_remux_quality_eligibility(
                    exact_name=classified.exact_release_name,
                    resolution=classified.resolution,
                    source=classified.source,
                    media_processing=classified.media_processing,
                )
                if not q_ok:
                    return False, f"pre_upgrade_ineligible_quality:{q_reason}"

        return True, None

    def recheck_pre_upgrade_actionable_releases(
        self,
        archive_dir: Path,
        *,
        allowed_filter_names: tuple[str, ...] = DEFAULT_ALLOWED_FILTER_NAMES,
        allowed_filter_ids: tuple[int, ...] = (),
    ) -> int:
        """
        PR #18 Round 2 Blocker 1: At startup, suppress any pre-upgrade queued/in-flight
        or follow-up-bearing release that does not satisfy the current notification scope
        rules so old WEB-DL / global release.new rows never trigger tracker, evaluator,
        or Telegram traffic while keeping their private archives intact.
        """
        conn = self._connect()
        try:
            actionable = conn.execute(
                """
                SELECT release_id, latest_occurrence_id, ack_message_id, pending_card_supersede_json
                FROM releases
                WHERE state IN ('queued', 'enriching', 'evaluating', 'delivering')
                   OR pending_card_supersede_json IS NOT NULL
                   OR pending_eval_reply_json IS NOT NULL
                ORDER BY created_at ASC, release_id ASC
                """
            ).fetchall()
            # PR #18 Round 5 Finding 3: Also scan never-evaluated, never-delivered releases
            # previously suppressed as `pre_upgrade_ineligible_scope` (e.g. while an archive was
            # temporarily unavailable during migration) so restoring the archive + restart
            # automatically recovers the unsent eligible notification once.
            suppressed_unsent = conn.execute(
                """
                SELECT release_id
                FROM releases
                WHERE state = 'suppressed'
                  AND suppression_reason = 'pre_upgrade_ineligible_scope'
                  AND card_revision = 0
                  AND last_evaluated_digest IS NULL
                  AND ack_message_id IS NULL
                ORDER BY created_at ASC, release_id ASC
                """
            ).fetchall()
        finally:
            conn.close()

        to_suppress: list[tuple[str, str, Optional[str], bool]] = []
        for row in actionable:
            rel_id = str(row["release_id"])
            latest_occ_id = str(row["latest_occurrence_id"] or "")
            eligible, reason = self.is_release_currently_eligible(
                rel_id,
                archive_dir,
                allowed_filter_names=allowed_filter_names,
                allowed_filter_ids=allowed_filter_ids,
            )
            if eligible:
                continue
            preserve_retire_supersede = False
            raw_sup = row["pending_card_supersede_json"]
            if row["ack_message_id"] is not None and isinstance(raw_sup, str) and raw_sup.strip():
                try:
                    sup_obj = json.loads(raw_sup)
                    if isinstance(sup_obj, dict) and sup_obj.get("delivery_kind") in ("retired", "evidence_review"):
                        preserve_retire_supersede = True
                except ValueError:
                    preserve_retire_supersede = False
            to_suppress.append((rel_id, latest_occ_id, reason, preserve_retire_supersede))

        to_requeue: list[str] = []
        for row in suppressed_unsent:
            rel_id = str(row["release_id"])
            eligible, _reason = self.is_release_currently_eligible(
                rel_id,
                archive_dir,
                allowed_filter_names=allowed_filter_names,
                allowed_filter_ids=allowed_filter_ids,
            )
            if eligible:
                to_requeue.append(rel_id)

        if not to_suppress and not to_requeue:
            return 0

        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for rel_id, latest_occ_id, reason, preserve_retire in to_suppress:
                if (
                    latest_occ_id
                    and reason not in ("unverified_occurrence_archive", "missing_latest_occurrence_record")
                ):
                    conn.execute(
                        """
                        UPDATE occurrences
                        SET classification = 'pre_upgrade_ineligible_scope',
                            diagnostic_note = COALESCE(diagnostic_note, ?)
                        WHERE occurrence_id = ?
                          AND release_id = ?
                          AND classification = 'movie_candidate'
                        """,
                        (reason or "pre_upgrade_ineligible_scope", latest_occ_id, rel_id),
                    )
                if preserve_retire:
                    conn.execute(
                        """
                        UPDATE releases
                        SET pending_eval_reply_json = NULL,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (now_iso, rel_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE releases
                        SET state = 'suppressed',
                            suppression_reason = 'pre_upgrade_ineligible_scope',
                            pending_reprocess = 0,
                            pending_card_supersede_json = NULL,
                            pending_eval_reply_json = NULL,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (now_iso, rel_id),
                    )
            for rel_id in to_requeue:
                conn.execute(
                    """
                    UPDATE releases
                    SET state = 'queued',
                        suppression_reason = NULL,
                        pending_reprocess = 0,
                        retry_count = 0,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        next_attempt_at = ?,
                        updated_at = ?
                    WHERE release_id = ?
                      AND state = 'suppressed'
                      AND suppression_reason = 'pre_upgrade_ineligible_scope'
                      AND card_revision = 0
                      AND last_evaluated_digest IS NULL
                      AND ack_message_id IS NULL
                    """,
                    (now_iso, now_iso, rel_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return len(to_suppress)

    def record_non_movie_occurrence(
        self,
        *,
        occurrence_id: str,
        received_at_iso: str,
        receipt: ArchiveReceipt,
        classification: str,
        diagnostic_note: Optional[str],
    ) -> IntakeClaimResult:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO occurrences (
                    occurrence_id, received_at, archive_relpath, archive_sha256,
                    payload_bytes, classification, diagnostic_note, release_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    occurrence_id,
                    received_at_iso,
                    receipt.archive_relpath,
                    receipt.archive_sha256,
                    receipt.payload_bytes,
                    classification,
                    diagnostic_note,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        status = (
            "diagnostic_only"
            if classification == "movie_category_type_conflict"
            else "archived_non_movie"
        )
        return IntakeClaimResult(status=status, occurrence_id=occurrence_id, release_id=None)

    def record_movie_occurrence(
        self,
        *,
        occurrence_id: str,
        received_at_iso: str,
        receipt: ArchiveReceipt,
        classified: ClassifiedWebhook,
    ) -> IntakeClaimResult:
        """
        Atomically claim a new release or coalesce a duplicate burst / verified correction
        inside one SQLite BEGIN IMMEDIATE transaction.
        """
        snap_json = json.dumps(classified.to_sanitized_snapshot(), ensure_ascii=False)
        occ_classification = classified.classification
        occ_diagnostic_note: Optional[str] = None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT release_id, state, suppression_reason, card_revision,
                       incoming_evidence_digest, latest_occurrence_id, source_occurrence_id,
                       last_evaluated_digest, ack_message_id
                FROM releases
                WHERE indexer_norm = ? AND external_id_norm = ? AND exact_release_name = ?
                """,
                (
                    classified.indexer_norm,
                    classified.external_id_norm,
                    classified.exact_release_name,
                ),
            ).fetchone()

            if existing is None:
                release_id = generate_uuid7()
                conn.execute(
                    """
                    INSERT INTO releases (
                        release_id, indexer_norm, external_id_norm, exact_release_name,
                        first_occurrence_id, latest_occurrence_id, source_occurrence_id,
                        card_revision, state, pending_reprocess,
                        incoming_evidence_digest, incoming_snapshot_json,
                        next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'queued', 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        release_id,
                        classified.indexer_norm,
                        classified.external_id_norm,
                        classified.exact_release_name,
                        occurrence_id,
                        occurrence_id,
                        occurrence_id,
                        classified.incoming_evidence_digest,
                        snap_json,
                        received_at_iso,
                        received_at_iso,
                        received_at_iso,
                    ),
                )
                result_status = "accepted"
                card_revision = 0
            else:
                release_id = str(existing["release_id"])
                cur_state = str(existing["state"])
                cur_suppression_reason = str(existing["suppression_reason"] or "")
                card_revision = int(existing["card_revision"])
                cur_digest = str(existing["incoming_evidence_digest"])
                latest_occ_id = str(existing["latest_occurrence_id"])
                cur_source_occ = str(existing["source_occurrence_id"])
                never_processed = (
                    card_revision == 0
                    and existing["last_evaluated_digest"] is None
                    and existing["ack_message_id"] is None
                )
                latest_occ_row = conn.execute(
                    "SELECT classification, archive_sha256, incoming_evidence_digest FROM occurrences WHERE occurrence_id = ?",
                    (latest_occ_id,),
                ).fetchone()
                latest_is_ineligible_pre_upgrade = (
                    latest_occ_row is not None
                    and str(latest_occ_row["classification"] or "") == "pre_upgrade_ineligible_scope"
                )
                if classified.has_download_url:
                    source_occ = occurrence_id
                elif latest_is_ineligible_pre_upgrade and cur_source_occ == latest_occ_id:
                    source_occ = occurrence_id
                else:
                    source_occ = cur_source_occ

                # PR #18 Round 4 Findings 3 & 4 + Round 5 Finding 3:
                # 1. Never bypass proven eligible (`movie_candidate`) history for superseded
                #    identities (`cur_digest != incoming`) or missing-archive ordering guards,
                #    and never relabel prior `movie_candidate` records.
                # 2. Only treat `latest_occurrence_id` as an exact duplicate when it was NOT an
                #    intact pre-upgrade excluded delivery (`pre_upgrade_ineligible_scope`).
                # 3. If a never-evaluated, never-delivered release was suppressed ONLY because an
                #    excluded filter's duplicate of the SAME current identity (`cur_digest == incoming`)
                #    became `latest_occurrence_id` before upgrade, allow a fresh qualifying delivery
                #    to wake and process it once without re-triggering on any subsequent duplicate.
                retrying_unsent_pre_upgrade_duplicate = (
                    latest_is_ineligible_pre_upgrade
                    and cur_state == "suppressed"
                    and cur_suppression_reason == "pre_upgrade_ineligible_scope"
                    and never_processed
                    and cur_digest == classified.incoming_evidence_digest
                )

                is_exact_latest_replay = (
                    not latest_is_ineligible_pre_upgrade
                    and (
                        cur_digest == classified.incoming_evidence_digest
                        or (
                            latest_occ_row is not None
                            and str(latest_occ_row["archive_sha256"]) == receipt.archive_sha256
                        )
                    )
                )

                seen_prior_occurrence = (
                    not is_exact_latest_replay
                    and not retrying_unsent_pre_upgrade_duplicate
                    and conn.execute(
                        """
                        SELECT 1 FROM occurrences
                        WHERE release_id = ?
                          AND classification = 'movie_candidate'
                          AND (
                              incoming_evidence_digest = ?
                              OR archive_sha256 = ?
                          )
                        LIMIT 1
                        """,
                        (release_id, classified.incoming_evidence_digest, receipt.archive_sha256),
                    ).fetchone()
                    is not None
                )

                # PR #18 Round 2 Blocker 4 & Round 4 Finding 3: If any historical applied
                # `movie_candidate` occurrence still has a NULL digest (missing or failed-integrity
                # archive), ordering/novelty vs. prior deliveries cannot be proved. Withhold the
                # incoming arrival without poisoning history so archive restoration + restart can
                # still recover cleanly.
                has_unverified_legacy_occurrence = (
                    not is_exact_latest_replay
                    and not seen_prior_occurrence
                    and conn.execute(
                        """
                        SELECT 1 FROM occurrences
                        WHERE release_id = ?
                          AND classification = 'movie_candidate'
                          AND incoming_evidence_digest IS NULL
                        LIMIT 1
                        """,
                        (release_id,),
                    ).fetchone()
                    is not None
                )

                waking_pre_upgrade_ineligible_new = (
                    not is_exact_latest_replay
                    and not seen_prior_occurrence
                    and not has_unverified_legacy_occurrence
                    and cur_state == "suppressed"
                    and cur_suppression_reason == "pre_upgrade_ineligible_scope"
                    and card_revision == 0
                )

                if is_exact_latest_replay:
                    # Exact duplicate (concurrent burst or late duplicate): attach occurrence
                    # without re-queuing, while preserving/upgrading source_occurrence_id if
                    # this delivery supplies a usable download URL.
                    conn.execute(
                        """
                        UPDATE releases
                        SET latest_occurrence_id = ?,
                            source_occurrence_id = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (occurrence_id, source_occ, received_at_iso, release_id),
                    )
                    result_status = "coalesced_duplicate"
                elif seen_prior_occurrence:
                    # Issue #13 §4a & PR #18 Blocker 3 / Round 4 Finding 3: Replayed older
                    # superseded eligible evidence (including across upgrades and suppressed
                    # transitions). Archive the replay occurrence and preserve current state.
                    conn.execute(
                        """
                        UPDATE releases
                        SET updated_at = ?
                        WHERE release_id = ?
                        """,
                        (received_at_iso, release_id),
                    )
                    result_status = "coalesced_duplicate"
                elif has_unverified_legacy_occurrence:
                    # PR #18 Round 2 Blocker 4 & Round 3 Finding 5: Ambiguous arrival while an
                    # earlier pre-upgrade archive is missing/corrupt. Archive the arrival as
                    # withheld (`movie_withheld_unverified_history`) and preserve current state
                    # intact, without poisoning `seen_prior_occurrence` if the archive is later
                    # restored and genuine new evidence is redelivered.
                    occ_classification = "movie_withheld_unverified_history"
                    occ_diagnostic_note = "withheld_unverified_legacy_archive"
                    conn.execute(
                        """
                        UPDATE releases
                        SET updated_at = ?
                        WHERE release_id = ?
                        """,
                        (received_at_iso, release_id),
                    )
                    result_status = "coalesced_duplicate"
                elif waking_pre_upgrade_ineligible_new:
                    conn.execute(
                        """
                        UPDATE releases
                        SET latest_occurrence_id = ?,
                            source_occurrence_id = ?,
                            incoming_evidence_digest = ?,
                            incoming_snapshot_json = ?,
                            state = 'queued',
                            pending_reprocess = 0,
                            retry_count = 0,
                            pending_card_supersede_json = NULL,
                            pending_eval_reply_json = NULL,
                            eval_reply_status = 'pending',
                            suppression_reason = NULL,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            next_attempt_at = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (
                            occurrence_id,
                            source_occ,
                            classified.incoming_evidence_digest,
                            snap_json,
                            received_at_iso,
                            received_at_iso,
                            release_id,
                        ),
                    )
                    result_status = "accepted"
                elif cur_state == "ambiguous_delivery":
                    # Never reopen/resend a possibly delivered grab card while delivery is ambiguous,
                    # but still preserve a newly supplied usable acquisition source occurrence.
                    conn.execute(
                        """
                        UPDATE releases
                        SET latest_occurrence_id = ?,
                            source_occurrence_id = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (occurrence_id, source_occ, received_at_iso, release_id),
                    )
                    result_status = "coalesced_duplicate"
                elif cur_state == "queued":
                    # Still queued: update snapshot cleanly in place before first claim
                    conn.execute(
                        """
                        UPDATE releases
                        SET latest_occurrence_id = ?,
                            source_occurrence_id = ?,
                            incoming_evidence_digest = ?,
                            incoming_snapshot_json = ?,
                            retry_count = 0,
                            pending_card_supersede_json = NULL,
                            pending_eval_reply_json = NULL,
                            eval_reply_status = CASE WHEN card_revision >= 1 THEN eval_reply_status ELSE 'pending' END,
                            next_attempt_at = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (
                            occurrence_id,
                            source_occ,
                            classified.incoming_evidence_digest,
                            snap_json,
                            received_at_iso,
                            received_at_iso,
                            release_id,
                        ),
                    )
                    result_status = "updated_correction"
                elif cur_state in ("enriching", "evaluating", "delivering"):
                    # Mid-flight: flag pending_reprocess so the worker re-runs once current pass yields
                    conn.execute(
                        """
                        UPDATE releases
                        SET latest_occurrence_id = ?,
                            source_occurrence_id = ?,
                            incoming_evidence_digest = ?,
                            incoming_snapshot_json = ?,
                            pending_reprocess = 1,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (
                            occurrence_id,
                            source_occ,
                            classified.incoming_evidence_digest,
                            snap_json,
                            received_at_iso,
                            release_id,
                        ),
                    )
                    result_status = "updated_correction"
                else:
                    # completed / suppressed / failed_terminal -> re-queue for correction evaluation
                    conn.execute(
                        """
                        UPDATE releases
                        SET latest_occurrence_id = ?,
                            source_occurrence_id = ?,
                            incoming_evidence_digest = ?,
                            incoming_snapshot_json = ?,
                            state = 'queued',
                            pending_reprocess = 0,
                            retry_count = 0,
                            pending_card_supersede_json = NULL,
                            pending_eval_reply_json = NULL,
                            eval_reply_status = CASE WHEN card_revision >= 1 THEN eval_reply_status ELSE 'pending' END,
                            suppression_reason = NULL,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            next_attempt_at = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (
                            occurrence_id,
                            source_occ,
                            classified.incoming_evidence_digest,
                            snap_json,
                            received_at_iso,
                            received_at_iso,
                            release_id,
                        ),
                    )
                    result_status = "updated_correction"

            conn.execute(
                """
                INSERT INTO occurrences (
                    occurrence_id, received_at, archive_relpath, archive_sha256,
                    payload_bytes, classification, diagnostic_note, release_id,
                    incoming_evidence_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    received_at_iso,
                    receipt.archive_relpath,
                    receipt.archive_sha256,
                    receipt.payload_bytes,
                    occ_classification,
                    occ_diagnostic_note,
                    release_id,
                    classified.incoming_evidence_digest,
                ),
            )
            conn.execute("COMMIT")
            return IntakeClaimResult(
                status=result_status,
                occurrence_id=occurrence_id,
                release_id=release_id,
                card_revision=card_revision,
            )
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def recover_orphaned_leases(self, *, active_worker_id: str, recover_dead_owners: bool = False) -> int:
        """
        Reclaim releases stuck in enriching/evaluating/delivering whose lease expired
        (or on startup, owned by a previous dead worker process).
        Never touches ambiguous_delivery.
        """
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if recover_dead_owners:
                cur = conn.execute(
                    """
                    UPDATE releases
                    SET state = 'queued',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        next_attempt_at = ?,
                        updated_at = ?
                    WHERE state IN ('enriching', 'evaluating', 'delivering')
                      AND (lease_owner IS NULL OR lease_owner != ? OR lease_expires_at <= ?)
                    """,
                    (now_iso, now_iso, active_worker_id, now_iso),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE releases
                    SET state = 'queued',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        next_attempt_at = ?,
                        updated_at = ?
                    WHERE state IN ('enriching', 'evaluating', 'delivering')
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    """,
                    (now_iso, now_iso, now_iso),
                )
            recovered = cur.rowcount
            conn.execute("COMMIT")
            return recovered
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def claim_next_queued_release(self, *, worker_id: str, lease_seconds: int) -> Optional[dict[str, Any]]:
        now = utc_now()
        now_iso = utc_now_iso(now)
        lease_until_iso = utc_now_iso(now + timedelta(seconds=lease_seconds))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM releases
                WHERE state = 'queued'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now_iso,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None

            release_id = str(row["release_id"])
            conn.execute(
                """
                UPDATE releases
                SET state = 'enriching',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE release_id = ? AND state = 'queued'
                """,
                (worker_id, lease_until_iso, now_iso, release_id),
            )
            claimed = conn.execute(
                "SELECT * FROM releases WHERE release_id = ?", (release_id,)
            ).fetchone()
            conn.execute("COMMIT")
            return dict(claimed) if claimed is not None else None
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def renew_lease(self, release_id: str, *, worker_id: str, lease_seconds: int, new_state: Optional[str] = None) -> bool:
        now = utc_now()
        now_iso = utc_now_iso(now)
        lease_until_iso = utc_now_iso(now + timedelta(seconds=lease_seconds))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if new_state is not None:
                cur = conn.execute(
                    """
                    UPDATE releases
                    SET state = ?, lease_expires_at = ?, updated_at = ?
                    WHERE release_id = ? AND lease_owner = ?
                    """,
                    (new_state, lease_until_iso, now_iso, release_id, worker_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE releases
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE release_id = ? AND lease_owner = ?
                    """,
                    (lease_until_iso, now_iso, release_id, worker_id),
                )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def record_card_ack(
        self,
        release_id: str,
        *,
        worker_id: str,
        card_revision: int,
        ack_chat_id: Optional[str],
        ack_message_id: int,
        ack_thread_id: Optional[int],
        ack_incoming_digest: str,
        last_card_digest: Optional[str] = None,
        pending_eval_reply: Any = ...,
        full_evidence_digest: Optional[str] = None,
        last_evaluated_digest: Optional[str] = None,
        tl_outcome: Optional[str] = None,
        identity_gate_status: Optional[str] = None,
        identity_conflict_reason: Any = ...,
        verified_tmdb_id: Any = ...,
        verified_imdb_id: Any = ...,
        verified_title: Any = ...,
        verified_year: Any = ...,
        radarr_status_snapshot: Optional[dict[str, Any]] = None,
        evaluation_json: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Persist acknowledged Telegram card receipt and pre-reply identity/snapshot metadata atomically."""
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur_row = conn.execute(
                """
                SELECT identity_gate_status, identity_conflict_reason,
                       verified_tmdb_id, verified_imdb_id, verified_title, verified_year,
                       pending_eval_reply_json
                FROM releases
                WHERE release_id = ? AND lease_owner = ?
                """,
                (release_id, worker_id),
            ).fetchone()
            if cur_row is None:
                conn.execute("COMMIT")
                return False

            final_pending_reply = (
                cur_row["pending_eval_reply_json"]
                if pending_eval_reply is ...
                else (
                    json.dumps(pending_eval_reply, ensure_ascii=False)
                    if pending_eval_reply is not None
                    else None
                )
            )
            final_gate = identity_gate_status or str(cur_row["identity_gate_status"])
            final_conflict = (
                cur_row["identity_conflict_reason"]
                if identity_conflict_reason is ...
                else identity_conflict_reason
            )
            final_tmdb = cur_row["verified_tmdb_id"] if verified_tmdb_id is ... else verified_tmdb_id
            final_imdb = cur_row["verified_imdb_id"] if verified_imdb_id is ... else verified_imdb_id
            final_title = cur_row["verified_title"] if verified_title is ... else verified_title
            final_year = cur_row["verified_year"] if verified_year is ... else verified_year
            cur = conn.execute(
                """
                UPDATE releases
                SET card_revision = ?,
                    ack_chat_id = ?,
                    ack_message_id = ?,
                    ack_thread_id = ?,
                    ack_incoming_digest = ?,
                    last_card_digest = COALESCE(?, last_card_digest),
                    last_eval_reply_digest = NULL,
                    eval_reply_status = 'pending',
                    pending_eval_reply_json = ?,
                    full_evidence_digest = COALESCE(?, full_evidence_digest),
                    last_evaluated_digest = COALESCE(?, last_evaluated_digest),
                    tl_outcome = COALESCE(?, tl_outcome),
                    identity_gate_status = ?,
                    identity_conflict_reason = ?,
                    verified_tmdb_id = ?,
                    verified_imdb_id = ?,
                    verified_title = ?,
                    verified_year = ?,
                    radarr_status_snapshot = COALESCE(?, radarr_status_snapshot),
                    evaluation_json = COALESCE(?, evaluation_json),
                    updated_at = ?
                WHERE release_id = ? AND lease_owner = ?
                """,
                (
                    card_revision,
                    ack_chat_id,
                    ack_message_id,
                    ack_thread_id,
                    ack_incoming_digest,
                    last_card_digest,
                    final_pending_reply,
                    full_evidence_digest,
                    last_evaluated_digest,
                    tl_outcome,
                    final_gate,
                    final_conflict,
                    final_tmdb,
                    final_imdb,
                    final_title,
                    final_year,
                    json.dumps(radarr_status_snapshot, ensure_ascii=False)
                    if radarr_status_snapshot is not None
                    else None,
                    json.dumps(evaluation_json, ensure_ascii=False)
                    if evaluation_json is not None
                    else None,
                    now_iso,
                    release_id,
                    worker_id,
                ),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def finalize_release(
        self,
        release_id: str,
        *,
        worker_id: str,
        state: str,
        expected_incoming_digest: str,
        suppression_reason: Any = ...,
        card_revision: Optional[int] = None,
        full_evidence_digest: Optional[str] = None,
        last_evaluated_digest: Optional[str] = None,
        last_card_digest: Optional[str] = None,
        last_eval_reply_digest: Any = ...,
        tl_outcome: Optional[str] = None,
        identity_gate_status: Optional[str] = None,
        identity_conflict_reason: Any = ...,
        verified_tmdb_id: Any = ...,
        verified_imdb_id: Any = ...,
        verified_title: Any = ...,
        verified_year: Any = ...,
        ack_chat_id: Optional[str] = None,
        ack_message_id: Optional[int] = None,
        ack_thread_id: Optional[int] = None,
        eval_reply_status: Optional[str] = None,
        pending_card_supersede: Any = ...,
        pending_eval_reply: Any = ...,
        radarr_status_snapshot: Optional[dict[str, Any]] = None,
        evaluation_json: Optional[dict[str, Any]] = None,
        next_attempt_at: Optional[str] = None,
        increment_retry: bool = False,
        reset_retry: bool = False,
        allow_downgrade_on_conflict: bool = False,
    ) -> str:
        """
        Atomically commit release outcome. If a newer correction arrived while the worker
        was mid-flight (pending_reprocess=1 or incoming_evidence_digest changed) and the
        outcome is NOT ambiguous_delivery, transitions cleanly back to queued so the
        newer correction is never lost.
        """
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur_row = conn.execute(
                """
                SELECT pending_reprocess, incoming_evidence_digest, card_revision,
                       suppression_reason, identity_gate_status, identity_conflict_reason,
                       verified_tmdb_id, verified_imdb_id,
                       verified_title, verified_year, retry_count,
                       ack_chat_id, ack_message_id, ack_thread_id,
                       last_card_digest, last_eval_reply_digest, eval_reply_status,
                       pending_card_supersede_json, pending_eval_reply_json
                FROM releases
                WHERE release_id = ? AND lease_owner = ?
                """,
                (release_id, worker_id),
            ).fetchone()
            if cur_row is None:
                conn.execute("COMMIT")
                return "lease_lost"

            has_newer = (
                int(cur_row["pending_reprocess"]) == 1
                or str(cur_row["incoming_evidence_digest"]) != expected_incoming_digest
            )

            # Never downgrade an already-delivered verified card on a temporary lookup failure;
            # only allow superseding to evidence_review on a genuine verified conflict.
            prev_verified = (
                str(cur_row["identity_gate_status"]) == "verified"
                and int(cur_row["card_revision"] or 0) >= 1
                and cur_row["ack_message_id"] is not None
                and int(cur_row["ack_message_id"]) > 0
            )
            final_gate = identity_gate_status or str(cur_row["identity_gate_status"])
            final_suppression = (
                cur_row["suppression_reason"] if suppression_reason is ... else suppression_reason
            )
            final_conflict_reason = (
                cur_row["identity_conflict_reason"]
                if identity_conflict_reason is ...
                else identity_conflict_reason
            )
            final_tmdb = cur_row["verified_tmdb_id"] if verified_tmdb_id is ... else verified_tmdb_id
            final_imdb = cur_row["verified_imdb_id"] if verified_imdb_id is ... else verified_imdb_id
            final_title = cur_row["verified_title"] if verified_title is ... else verified_title
            final_year = cur_row["verified_year"] if verified_year is ... else verified_year
            if prev_verified and final_gate != "verified" and not allow_downgrade_on_conflict:
                final_gate = "verified"
                final_tmdb = cur_row["verified_tmdb_id"]
                final_imdb = cur_row["verified_imdb_id"]
                final_title = cur_row["verified_title"]
                final_year = cur_row["verified_year"]

            final_pending_supersede = (
                cur_row["pending_card_supersede_json"]
                if pending_card_supersede is ...
                else (
                    json.dumps(pending_card_supersede, ensure_ascii=False)
                    if pending_card_supersede is not None
                    else None
                )
            )
            final_pending_reply = (
                cur_row["pending_eval_reply_json"]
                if pending_eval_reply is ...
                else (
                    json.dumps(pending_eval_reply, ensure_ascii=False)
                    if pending_eval_reply is not None
                    else None
                )
            )
            final_card_rev = (
                card_revision if card_revision is not None else int(cur_row["card_revision"])
            )
            final_eval_reply_status = eval_reply_status or str(cur_row["eval_reply_status"])
            final_last_card_digest = (
                last_card_digest if last_card_digest is not None else cur_row["last_card_digest"]
            )
            final_last_eval_reply_digest = (
                cur_row["last_eval_reply_digest"]
                if last_eval_reply_digest is ...
                else last_eval_reply_digest
            )

            final_state = state
            final_pending = 0
            final_next_attempt = next_attempt_at
            base_retries = 0 if (reset_retry or state == "completed") else int(cur_row["retry_count"])
            final_retries = base_retries + (1 if increment_retry else 0)
            if has_newer and state != "ambiguous_delivery":
                final_state = "queued"
                final_next_attempt = now_iso
                final_retries = 0
                final_pending_supersede = None
                final_pending_reply = None
                if final_card_rev < 1:
                    final_eval_reply_status = "pending"

            final_ack_chat = ack_chat_id if ack_chat_id is not None else cur_row["ack_chat_id"]
            final_ack_msg = ack_message_id if ack_message_id is not None else cur_row["ack_message_id"]
            final_ack_thread = ack_thread_id if ack_thread_id is not None else cur_row["ack_thread_id"]

            conn.execute(
                """
                UPDATE releases
                SET state = ?,
                    pending_reprocess = ?,
                    suppression_reason = ?,
                    card_revision = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    retry_count = ?,
                    next_attempt_at = ?,
                    full_evidence_digest = COALESCE(?, full_evidence_digest),
                    last_evaluated_digest = COALESCE(?, last_evaluated_digest),
                    last_card_digest = ?,
                    last_eval_reply_digest = ?,
                    tl_outcome = COALESCE(?, tl_outcome),
                    identity_gate_status = ?,
                    identity_conflict_reason = ?,
                    verified_tmdb_id = ?,
                    verified_imdb_id = ?,
                    verified_title = ?,
                    verified_year = ?,
                    ack_chat_id = ?,
                    ack_message_id = ?,
                    ack_thread_id = ?,
                    eval_reply_status = ?,
                    pending_card_supersede_json = ?,
                    pending_eval_reply_json = ?,
                    radarr_status_snapshot = COALESCE(?, radarr_status_snapshot),
                    evaluation_json = COALESCE(?, evaluation_json),
                    updated_at = ?
                WHERE release_id = ?
                """,
                (
                    final_state,
                    final_pending,
                    final_suppression,
                    final_card_rev,
                    final_retries,
                    final_next_attempt,
                    full_evidence_digest,
                    last_evaluated_digest,
                    final_last_card_digest,
                    final_last_eval_reply_digest,
                    tl_outcome,
                    final_gate,
                    final_conflict_reason,
                    final_tmdb,
                    final_imdb,
                    final_title,
                    final_year,
                    final_ack_chat,
                    final_ack_msg,
                    final_ack_thread,
                    final_eval_reply_status,
                    final_pending_supersede,
                    final_pending_reply,
                    json.dumps(radarr_status_snapshot, ensure_ascii=False)
                    if radarr_status_snapshot is not None
                    else None,
                    json.dumps(evaluation_json, ensure_ascii=False)
                    if evaluation_json is not None
                    else None,
                    now_iso,
                    release_id,
                ),
            )
            conn.execute("COMMIT")
            return final_state
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_occurrence_archive_ref(
        self, release_id: str, source_occurrence_id: str
    ) -> Optional[OccurrenceArchiveRef]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT o.occurrence_id, o.release_id, o.archive_relpath, o.archive_sha256,
                       o.received_at, r.indexer_norm, r.external_id_norm, r.exact_release_name
                FROM occurrences o
                JOIN releases r ON r.release_id = o.release_id
                WHERE o.release_id = ? AND o.occurrence_id = ?
                """,
                (release_id, source_occurrence_id),
            ).fetchone()
            if row is None:
                return None
            return OccurrenceArchiveRef(
                occurrence_id=str(row["occurrence_id"]),
                release_id=str(row["release_id"]),
                archive_relpath=str(row["archive_relpath"]),
                archive_sha256=str(row["archive_sha256"]),
                received_at=str(row["received_at"]),
                indexer_norm=str(row["indexer_norm"]),
                external_id_norm=str(row["external_id_norm"]),
                exact_release_name=str(row["exact_release_name"]),
            )
        finally:
            conn.close()

    def get_release(self, release_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM releases WHERE release_id = ?", (release_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def list_occurrences_for_release(self, release_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM occurrences WHERE release_id = ? ORDER BY received_at ASC",
                (release_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_all_occurrences(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM occurrences ORDER BY received_at ASC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_all_releases(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM releases ORDER BY created_at ASC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_tl_cooldown(self) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tl_cooldown WHERE singleton_id = 1").fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def is_tl_cooldown_active(self, *, now: Optional[datetime] = None) -> bool:
        row = self.get_tl_cooldown()
        if not row or not row.get("cooldown_until"):
            return False
        until = parse_iso(str(row["cooldown_until"]))
        return until is not None and until > (now or utc_now())

    def record_tl_cooldown(
        self,
        *,
        cooldown_seconds: float,
        reason: str,
        now: Optional[datetime] = None,
    ) -> str:
        cur_time = now or utc_now()
        until = cur_time + timedelta(seconds=max(1.0, cooldown_seconds))
        until_iso = utc_now_iso(until)
        now_iso = utc_now_iso(cur_time)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT cooldown_until, consecutive_failures FROM tl_cooldown WHERE singleton_id = 1"
            ).fetchone()
            existing_until = (
                parse_iso(str(row["cooldown_until"]))
                if row and row["cooldown_until"]
                else None
            )
            effective_until = (
                existing_until
                if existing_until is not None and existing_until > until
                else until
            )
            effective_until_iso = utc_now_iso(effective_until)
            failures = (int(row["consecutive_failures"]) if row else 0) + 1
            conn.execute(
                """
                INSERT INTO tl_cooldown (singleton_id, cooldown_until, consecutive_failures, last_reason, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    cooldown_until = excluded.cooldown_until,
                    consecutive_failures = excluded.consecutive_failures,
                    last_reason = excluded.last_reason,
                    updated_at = excluded.updated_at
                """,
                (effective_until_iso, failures, reason[:120], now_iso),
            )
            conn.execute("COMMIT")
            return effective_until_iso
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def clear_tl_cooldown(self) -> None:
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO tl_cooldown (singleton_id, cooldown_until, consecutive_failures, last_reason, updated_at)
                VALUES (1, NULL, 0, NULL, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                    cooldown_until = NULL,
                    consecutive_failures = 0,
                    last_reason = NULL,
                    updated_at = excluded.updated_at
                """,
                (now_iso,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_health_counts(self) -> dict[str, Any]:
        now = utc_now()
        conn = self._connect()
        try:
            queued = conn.execute(
                "SELECT COUNT(*) AS cnt FROM releases WHERE state = 'queued'"
            ).fetchone()
            in_flight = conn.execute(
                "SELECT COUNT(*) AS cnt FROM releases WHERE state IN ('enriching', 'evaluating', 'delivering')"
            ).fetchone()
            ambiguous = conn.execute(
                "SELECT COUNT(*) AS cnt FROM releases WHERE state = 'ambiguous_delivery'"
            ).fetchone()
            cd_row = conn.execute(
                "SELECT cooldown_until FROM tl_cooldown WHERE singleton_id = 1"
            ).fetchone()
            until = parse_iso(str(cd_row["cooldown_until"])) if cd_row and cd_row["cooldown_until"] else None
            return {
                "queued_releases": int(queued["cnt"]) if queued else 0,
                "in_flight_releases": int(in_flight["cnt"]) if in_flight else 0,
                "ambiguous_deliveries": int(ambiguous["cnt"]) if ambiguous else 0,
                "tl_cooldown_active": until is not None and until > now,
            }
        finally:
            conn.close()
