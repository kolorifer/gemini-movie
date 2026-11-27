from __future__ import annotations

import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .security import PrivateFileError, ensure_private_dir, redact_secrets

ACTION_TYPES = frozenset(
    {"add_grab", "alternative_grab", "watched_good", "watched_bad", "watched_legacy_unrated", "skip"}
)
RADARR_PHASES = frozenset(
    {"radarr_add_movie", "radarr_release_push", "radarr_watched", "qbit_alt_preflight", "qbit_alternative_grab"}
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso(dt: Optional[datetime] = None) -> str:
    value = dt or utc_now()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def generate_uuid7() -> str:
    """Generate an RFC 9562 UUIDv7 string."""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    hex_str = f"{value:032x}"
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


@dataclass(frozen=True)
class NewActionRecord:
    action_id: str
    token_sha256: str
    release_id: str
    card_revision: int
    source_occurrence_id: str
    action_type: str
    completion_group: str
    bound_tmdb_id: Optional[int]
    bound_imdb_id: Optional[str]
    bound_title: str
    bound_year: Optional[int]
    bound_release_name: str
    bound_existing_movie_id: Optional[int]
    bound_existing_file_id: Optional[int]
    bound_existing_file_quality: Optional[str]
    bound_rejection_class: Optional[str]
    expires_at: str
    bound_snapshot_json: Optional[str] = None


@dataclass(frozen=True)
class CardDeliveryPlan:
    mode: str
    # send_new | edit | already_delivered | locked_completed_radarr | ambiguous | in_flight
    ack_chat_id: Optional[str] = None
    ack_message_id: Optional[int] = None
    ack_thread_id: Optional[int] = None
    used_poster: bool = False
    previous_card_revision: int = 0
    previous_delivery_kind: str = ""
    superseded_action_ids: tuple[str, ...] = ()
    newly_minted_action_ids: tuple[str, ...] = ()
    eval_reply_status: str = "pending"
    detail: Optional[str] = None


@dataclass(frozen=True)
class EvalReplyPlan:
    mode: str
    # send | already_sent | ambiguous | in_flight | reject_missing | reject_unanchored | reject_receipt_mismatch
    ack_chat_id: Optional[str] = None
    ack_message_id: Optional[int] = None
    ack_thread_id: Optional[int] = None
    existing_eval_reply_message_id: Optional[int] = None
    reply_attempt_id: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class ActionGroupSnapshot:
    action: Optional[dict[str, Any]]
    group_actions: tuple[dict[str, Any], ...]
    group_attempts: tuple[dict[str, Any], ...]
    promoted_stale_attempts: int = 0
    delivery: Optional[dict[str, Any]] = None
    release_actions: tuple[dict[str, Any], ...] = ()


class ActionsDB:
    """Owner-private SQLite WAL persistence for movie-actions-api (zero secrets stored)."""

    def __init__(self, db_path: Path) -> None:
        if not db_path.is_absolute():
            raise PrivateFileError("ACTIONS_DB_PATH must be an absolute path")
        self.db_path = db_path

    def _ensure_private_db_files(self) -> None:
        ensure_private_dir(self.db_path.parent)
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{self.db_path}{suffix}")
            is_sidecar = suffix in ("-wal", "-shm")
            try:
                st = os.lstat(str(p))
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode):
                raise PrivateFileError(f"Refusing symlinked SQLite file: {p.name}")
            if not stat.S_ISREG(st.st_mode):
                raise PrivateFileError(f"SQLite path is not a regular file: {p.name}")
            if st.st_uid != os.getuid():
                raise PrivateFileError(f"SQLite file not owned by current user: {p.name}")
            if (st.st_mode & 0o077) != 0:
                try:
                    os.chmod(str(p), 0o600)
                except FileNotFoundError:
                    if is_sidecar:
                        continue
                    raise

    def _connect(self) -> sqlite3.Connection:
        self._ensure_private_db_files()
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        self._ensure_private_db_files()
        return conn

    def init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    release_id                      TEXT PRIMARY KEY,
                    card_revision                   INTEGER NOT NULL,
                    delivery_kind                   TEXT NOT NULL,
                    status                          TEXT NOT NULL,
                    process_instance_id             TEXT,
                    lease_expires_at                TEXT,
                    target_chat_id                  TEXT NOT NULL,
                    target_thread_id                INTEGER,
                    ack_chat_id                     TEXT,
                    ack_message_id                  INTEGER,
                    ack_thread_id                   INTEGER,
                    used_poster                     INTEGER NOT NULL DEFAULT 0,
                    eval_reply_status               TEXT NOT NULL DEFAULT 'pending',
                    eval_reply_card_revision        INTEGER NOT NULL DEFAULT 0,
                    eval_reply_attempt_id           TEXT,
                    eval_reply_process_instance_id  TEXT,
                    eval_reply_lease_expires_at     TEXT,
                    eval_reply_message_id           INTEGER,
                    eval_doc_message_id             INTEGER,
                    last_error_redacted             TEXT,
                    created_at                      TEXT NOT NULL,
                    updated_at                      TEXT NOT NULL
                );
                """
            )
            existing_del_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(deliveries)").fetchall()
            }
            if "eval_reply_attempt_id" not in existing_del_cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN eval_reply_attempt_id TEXT;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    action_id                   TEXT PRIMARY KEY,
                    token_sha256                TEXT NOT NULL UNIQUE,
                    release_id                  TEXT NOT NULL REFERENCES deliveries(release_id),
                    card_revision               INTEGER NOT NULL,
                    source_occurrence_id        TEXT NOT NULL,
                    action_type                 TEXT NOT NULL,
                    completion_group            TEXT NOT NULL,
                    bound_tmdb_id               INTEGER,
                    bound_imdb_id               TEXT,
                    bound_title                 TEXT NOT NULL,
                    bound_year                  INTEGER,
                    bound_release_name          TEXT NOT NULL,
                    bound_existing_movie_id     INTEGER,
                    bound_existing_file_id      INTEGER,
                    bound_existing_file_quality TEXT,
                    bound_rejection_class       TEXT,
                    bound_snapshot_json         TEXT,
                    selected_root_folder        TEXT,
                    selected_quality_profile_id INTEGER,
                    status                      TEXT NOT NULL,
                    radarr_movie_id             INTEGER,
                    last_reconciled_at          TEXT,
                    feedback_status             TEXT NOT NULL DEFAULT 'not_applicable',
                    feedback_decision_saved     TEXT,
                    last_error_redacted         TEXT,
                    expires_at                  TEXT NOT NULL,
                    created_at                  TEXT NOT NULL,
                    updated_at                  TEXT NOT NULL
                );
                """
            )
            existing_act_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(actions)").fetchall()
            }
            if "bound_snapshot_json" not in existing_act_cols:
                conn.execute("ALTER TABLE actions ADD COLUMN bound_snapshot_json TEXT;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS write_attempts (
                    attempt_id              TEXT PRIMARY KEY,
                    action_id               TEXT NOT NULL REFERENCES actions(action_id),
                    completion_group        TEXT NOT NULL,
                    phase                   TEXT NOT NULL,
                    status                  TEXT NOT NULL,
                    process_instance_id     TEXT NOT NULL,
                    lease_expires_at        TEXT NOT NULL,
                    request_fingerprint     TEXT NOT NULL,
                    outcome_summary         TEXT,
                    started_at              TEXT NOT NULL,
                    finished_at             TEXT
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_group ON actions(completion_group, status);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_release_rev ON actions(release_id, card_revision);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attempts_action ON write_attempts(action_id, started_at);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attempts_status ON write_attempts(status, lease_expires_at);"
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()
        self._ensure_private_db_files()

    def _promote_stale_attempts_locked(
        self,
        conn: sqlite3.Connection,
        *,
        current_process_instance_id: str,
        now_iso: str,
        completion_group: Optional[str] = None,
        release_id: Optional[str] = None,
        reason: str = "stale_lease_promoted_to_ambiguous",
    ) -> int:
        """Promote orphaned or expired-lease `attempting` rows atomically inside an active transaction."""
        where_parts = [
            "wa.status = 'attempting'",
            "(wa.process_instance_id != ? OR wa.lease_expires_at <= ?)",
        ]
        params: list[Any] = [current_process_instance_id, now_iso]
        if completion_group is not None:
            where_parts.append("wa.completion_group = ?")
            params.append(completion_group)
        if release_id is not None:
            where_parts.append("a.release_id = ?")
            params.append(release_id)

        rows = conn.execute(
            f"""
            SELECT wa.attempt_id, wa.action_id, wa.phase, a.action_type, a.status AS action_status
            FROM write_attempts wa
            JOIN actions a ON a.action_id = wa.action_id
            WHERE {' AND '.join(where_parts)}
            """,
            params,
        ).fetchall()

        promoted = 0
        for row in rows:
            promoted += 1
            attempt_id = str(row["attempt_id"])
            action_id = str(row["action_id"])
            phase = str(row["phase"])
            action_type = str(row["action_type"])
            action_status = str(row["action_status"])
            conn.execute(
                """
                UPDATE write_attempts
                SET status = 'ambiguous',
                    outcome_summary = ?,
                    finished_at = ?
                WHERE attempt_id = ?
                """,
                (reason, now_iso, attempt_id),
            )
            if phase in RADARR_PHASES:
                if action_status == "attempting":
                    conn.execute(
                        """
                        UPDATE actions
                        SET status = 'ambiguous_radarr',
                            last_error_redacted = ?,
                            updated_at = ?
                        WHERE action_id = ?
                        """,
                        (reason, now_iso, action_id),
                    )
            elif phase == "evaluator_feedback":
                if action_type == "skip" and action_status == "attempting":
                    conn.execute(
                        """
                        UPDATE actions
                        SET status = 'active',
                            feedback_status = 'failed',
                            last_error_redacted = ?,
                            updated_at = ?
                        WHERE action_id = ?
                        """,
                        (reason, now_iso, action_id),
                    )
                else:
                    # Keep Radarr action permanently sealed at completed_radarr
                    conn.execute(
                        """
                        UPDATE actions
                        SET feedback_status = 'failed',
                            last_error_redacted = ?,
                            updated_at = ?
                        WHERE action_id = ?
                        """,
                        (reason, now_iso, action_id),
                    )

        # Safeguard: any action stuck in 'attempting' with zero live attempting write_attempts
        extra_where = ["status = 'attempting'"]
        extra_params: list[Any] = []
        if completion_group is not None:
            extra_where.append("completion_group = ?")
            extra_params.append(completion_group)
        if release_id is not None:
            extra_where.append("release_id = ?")
            extra_params.append(release_id)
        dangling_rows = conn.execute(
            f"""
            SELECT a.action_id, a.action_type
            FROM actions a
            WHERE {' AND '.join(extra_where)}
              AND NOT EXISTS (
                  SELECT 1 FROM write_attempts wa
                  WHERE wa.action_id = a.action_id
                    AND wa.status = 'attempting'
                    AND wa.process_instance_id = ?
                    AND wa.lease_expires_at > ?
              )
            """,
            extra_params + [current_process_instance_id, now_iso],
        ).fetchall()
        for row in dangling_rows:
            promoted += 1
            if str(row["action_type"]) == "skip":
                conn.execute(
                    """
                    UPDATE actions
                    SET status = 'active',
                        feedback_status = 'failed',
                        last_error_redacted = ?,
                        updated_at = ?
                    WHERE action_id = ?
                    """,
                    (reason, now_iso, str(row["action_id"])),
                )
            else:
                conn.execute(
                    """
                    UPDATE actions
                    SET status = 'ambiguous_radarr',
                        last_error_redacted = ?,
                        updated_at = ?
                    WHERE action_id = ?
                    """,
                    (reason, now_iso, str(row["action_id"])),
                )
        return promoted

    def recover_orphaned_state(self, current_process_instance_id: str) -> dict[str, int]:
        """
        §6.4 Startup orphan recovery executed synchronously inside FastAPI lifespan
        BEFORE accepting HTTP traffic.
        """
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            promoted_attempts = self._promote_stale_attempts_locked(
                conn,
                current_process_instance_id=current_process_instance_id,
                now_iso=now_iso,
                reason="orphaned_attempt_after_process_restart",
            )
            cur_del = conn.execute(
                """
                UPDATE deliveries
                SET status = 'ambiguous_delivery',
                    process_instance_id = NULL,
                    lease_expires_at = NULL,
                    last_error_redacted = 'orphaned_send_after_process_restart',
                    updated_at = ?
                WHERE status = 'sending'
                  AND (process_instance_id IS NULL OR process_instance_id != ?)
                """,
                (now_iso, current_process_instance_id),
            )
            cur_rep = conn.execute(
                """
                UPDATE deliveries
                SET eval_reply_status = 'ambiguous_reply',
                    eval_reply_attempt_id = NULL,
                    eval_reply_process_instance_id = NULL,
                    eval_reply_lease_expires_at = NULL,
                    last_error_redacted = 'orphaned_reply_after_process_restart',
                    updated_at = ?
                WHERE eval_reply_status = 'sending'
                  AND (eval_reply_process_instance_id IS NULL OR eval_reply_process_instance_id != ?)
                """,
                (now_iso, current_process_instance_id),
            )
            conn.execute("COMMIT;")
            return {
                "promoted_attempts": promoted_attempts,
                "orphaned_deliveries": cur_del.rowcount,
                "orphaned_replies": cur_rep.rowcount,
            }
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def get_health_counts(self) -> dict[str, int]:
        conn = self._connect()
        try:
            amb_del = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE status = 'ambiguous_delivery'"
            ).fetchone()[0]
            amb_rep = conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE eval_reply_status = 'ambiguous_reply'"
            ).fetchone()[0]
            amb_rad = conn.execute(
                "SELECT COUNT(*) FROM actions WHERE status = 'ambiguous_radarr'"
            ).fetchone()[0]
            active_act = conn.execute(
                "SELECT COUNT(*) FROM actions WHERE status = 'active'"
            ).fetchone()[0]
            return {
                "pending_ambiguous_deliveries": int(amb_del),
                "pending_ambiguous_replies": int(amb_rep),
                "pending_ambiguous_radarr": int(amb_rad),
                "active_actions": int(active_act),
            }
        finally:
            conn.close()

    def get_delivery(self, release_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def list_actions_for_release(self, release_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM actions WHERE release_id = ? ORDER BY created_at ASC, action_id ASC",
                (release_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_attempts_for_action(self, action_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM write_attempts WHERE action_id = ? ORDER BY started_at ASC, attempt_id ASC",
                (action_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _insert_actions_locked(
        self,
        conn: sqlite3.Connection,
        actions: list[NewActionRecord],
        *,
        now_iso: str,
    ) -> tuple[str, ...]:
        ids: list[str] = []
        for item in actions:
            conn.execute(
                """
                INSERT INTO actions (
                    action_id, token_sha256, release_id, card_revision, source_occurrence_id,
                    action_type, completion_group, bound_tmdb_id, bound_imdb_id, bound_title,
                    bound_year, bound_release_name, bound_existing_movie_id, bound_existing_file_id,
                    bound_existing_file_quality, bound_rejection_class, bound_snapshot_json,
                    status, feedback_status, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 'not_applicable', ?, ?, ?)
                """,
                (
                    item.action_id,
                    item.token_sha256,
                    item.release_id,
                    item.card_revision,
                    item.source_occurrence_id,
                    item.action_type,
                    item.completion_group,
                    item.bound_tmdb_id,
                    item.bound_imdb_id,
                    item.bound_title,
                    item.bound_year,
                    item.bound_release_name,
                    item.bound_existing_movie_id,
                    item.bound_existing_file_id,
                    item.bound_existing_file_quality,
                    item.bound_rejection_class,
                    item.bound_snapshot_json,
                    item.expires_at,
                    now_iso,
                    now_iso,
                ),
            )
            ids.append(item.action_id)
        return tuple(ids)

    def begin_card_delivery(
        self,
        *,
        release_id: str,
        card_revision: int,
        delivery_kind: str,
        target_chat_id: str,
        target_thread_id: Optional[int],
        process_instance_id: str,
        lease_expires_at: str,
        new_actions: list[NewActionRecord],
    ) -> CardDeliveryPlan:
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            self._promote_stale_attempts_locked(
                conn,
                current_process_instance_id=process_instance_id,
                now_iso=now_iso,
                release_id=release_id,
            )
            row = conn.execute(
                "SELECT * FROM deliveries WHERE release_id = ?",
                (release_id,),
            ).fetchone()

            if row is not None:
                status = str(row["status"])
                # Promote stale 'sending' lease to ambiguous_delivery (never resend!)
                if status == "sending":
                    row_proc = row["process_instance_id"]
                    row_lease = row["lease_expires_at"]
                    if (
                        not row_proc
                        or str(row_proc) != process_instance_id
                        or not row_lease
                        or str(row_lease) <= now_iso
                    ):
                        conn.execute(
                            """
                            UPDATE deliveries
                            SET status = 'ambiguous_delivery',
                                process_instance_id = NULL,
                                lease_expires_at = NULL,
                                last_error_redacted = 'stale_send_lease_promoted_to_ambiguous',
                                updated_at = ?
                            WHERE release_id = ?
                            """,
                            (now_iso, release_id),
                        )
                        conn.execute("COMMIT;")
                        return CardDeliveryPlan(mode="ambiguous", detail="stale_send_lease_ambiguous")
                    conn.execute("COMMIT;")
                    return CardDeliveryPlan(mode="in_flight", detail="delivery_already_in_flight")

                if status == "ambiguous_delivery":
                    conn.execute("COMMIT;")
                    return CardDeliveryPlan(mode="ambiguous", detail="existing_ambiguous_delivery")

                if status in ("delivered", "edit_failed"):
                    prev_rev = int(row["card_revision"])
                    prev_kind = str(row["delivery_kind"])
                    ack_chat = str(row["ack_chat_id"] or "")
                    ack_msg = row["ack_message_id"]
                    ack_thread = row["ack_thread_id"]
                    used_poster = bool(row["used_poster"])

                    if not ack_chat or ack_msg is None or int(ack_msg) <= 0:
                        conn.execute(
                            """
                            UPDATE deliveries
                            SET status = 'ambiguous_delivery',
                                last_error_redacted = 'delivered_row_missing_ack_message_id',
                                updated_at = ?
                            WHERE release_id = ?
                            """,
                            (now_iso, release_id),
                        )
                        conn.execute("COMMIT;")
                        return CardDeliveryPlan(mode="ambiguous", detail="missing_ack_message_id")

                    if status == "delivered" and card_revision <= prev_rev:
                        conn.execute("COMMIT;")
                        return CardDeliveryPlan(
                            mode="already_delivered",
                            ack_chat_id=ack_chat,
                            ack_message_id=int(ack_msg),
                            ack_thread_id=int(ack_thread) if ack_thread is not None else None,
                            used_poster=used_poster,
                            previous_card_revision=prev_rev,
                            previous_delivery_kind=prev_kind,
                            eval_reply_status=str(row["eval_reply_status"] or "pending"),
                        )

                    if status == "edit_failed" and card_revision < prev_rev:
                        conn.execute("COMMIT;")
                        return CardDeliveryPlan(
                            mode="already_delivered",
                            ack_chat_id=ack_chat,
                            ack_message_id=int(ack_msg),
                            ack_thread_id=int(ack_thread) if ack_thread is not None else None,
                            used_poster=used_poster,
                            previous_card_revision=prev_rev,
                            previous_delivery_kind=prev_kind,
                            eval_reply_status=str(row["eval_reply_status"] or "pending"),
                        )

                    # Higher card_revision edit (PR #18 Blocker 2 & Round 2 Blocker 3a):
                    # - `retired` NEVER reopens actions (0 minted buttons); always proceed with the edit,
                    #   persist card_revision/delivery_kind='retired', and supersede active tokens so any
                    #   in-flight pre-push Phase 1 (radarr_add_movie) immediately sees the retirement.
                    # - `evidence_review`, `verified_proposal`, and `alternative_grab` lock ONLY if an
                    #   irreversible write (`completed_radarr`, `completed_all`, or a push/watched/qbit
                    #   attempt in attempting/ambiguous/succeeded) has already started or completed.
                    #   An in-flight pre-push Phase 1 (`radarr_add_movie`) MUST NOT block a verified
                    #   correction: we persist the new revision immediately so Phase 1 aborts before
                    #   issuing any release push for the stale identity.
                    locked_row = None
                    if delivery_kind != "retired":
                        locked_row = conn.execute(
                            """
                            SELECT a.action_id, a.status
                            FROM actions a
                            WHERE a.release_id = ?
                              AND a.action_type != 'skip'
                              AND (
                                  a.status IN ('completed_radarr', 'completed_all')
                                  OR EXISTS (
                                      SELECT 1 FROM write_attempts w
                                      WHERE w.action_id = a.action_id
                                        AND w.phase IN ('radarr_release_push', 'radarr_watched', 'qbit_alternative_grab')
                                        AND w.status IN ('attempting', 'ambiguous', 'succeeded')
                                  )
                              )
                            LIMIT 1
                            """,
                            (release_id,),
                        ).fetchone()

                    if locked_row is not None:
                        # Never overwrite or reopen a release whose irreversible Radarr/qBit write is in-flight/completed.
                        # If a conflict downgrade arrived anyway, still supersede any unclicked active tokens.
                        if delivery_kind == "evidence_review":
                            conn.execute(
                                """
                                UPDATE actions
                                SET status = 'superseded', updated_at = ?
                                WHERE release_id = ? AND status = 'active'
                                """,
                                (now_iso, release_id),
                            )
                        conn.execute("COMMIT;")
                        return CardDeliveryPlan(
                            mode="locked_completed_radarr",
                            ack_chat_id=ack_chat,
                            ack_message_id=int(ack_msg),
                            ack_thread_id=int(ack_thread) if ack_thread is not None else None,
                            used_poster=used_poster,
                            previous_card_revision=prev_rev,
                            previous_delivery_kind=prev_kind,
                            eval_reply_status=str(row["eval_reply_status"] or "pending"),
                            detail=f"radarr_action_{locked_row['status']}",
                        )

                    # Supersede prior active tokens before editing card
                    prior_active = conn.execute(
                        "SELECT action_id FROM actions WHERE release_id = ? AND status = 'active'",
                        (release_id,),
                    ).fetchall()
                    superseded_ids = tuple(str(r["action_id"]) for r in prior_active)
                    if superseded_ids:
                        conn.execute(
                            """
                            UPDATE actions
                            SET status = 'superseded', updated_at = ?
                            WHERE release_id = ? AND status = 'active'
                            """,
                            (now_iso, release_id),
                        )

                    minted_ids = self._insert_actions_locked(conn, new_actions, now_iso=now_iso)
                    # Persist the higher card_revision & delivery_kind immediately BEFORE
                    # calling Telegram so any concurrent pre-push eligibility check sees the
                    # correction/retirement/downgrade atomically and invalidates stale pushes.
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET card_revision = ?,
                            delivery_kind = ?,
                            status = 'sending',
                            process_instance_id = ?,
                            lease_expires_at = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (card_revision, delivery_kind, process_instance_id, lease_expires_at, now_iso, release_id),
                    )
                    conn.execute("COMMIT;")
                    return CardDeliveryPlan(
                        mode="edit",
                        ack_chat_id=ack_chat,
                        ack_message_id=int(ack_msg),
                        ack_thread_id=int(ack_thread) if ack_thread is not None else None,
                        used_poster=used_poster,
                        previous_card_revision=prev_rev,
                        previous_delivery_kind=prev_kind,
                        superseded_action_ids=superseded_ids,
                        newly_minted_action_ids=minted_ids,
                    )

                # Previous initial send failed definitively -> reuse row cleanly for fresh send
                conn.execute(
                    """
                    UPDATE actions
                    SET status = 'superseded', updated_at = ?
                    WHERE release_id = ? AND status = 'active'
                    """,
                    (now_iso, release_id),
                )
                conn.execute(
                    """
                    UPDATE deliveries
                    SET card_revision = ?,
                        delivery_kind = ?,
                        status = 'sending',
                        process_instance_id = ?,
                        lease_expires_at = ?,
                        target_chat_id = ?,
                        target_thread_id = ?,
                        ack_chat_id = NULL,
                        ack_message_id = NULL,
                        ack_thread_id = NULL,
                        used_poster = 0,
                        eval_reply_status = 'pending',
                        eval_reply_card_revision = ?,
                        last_error_redacted = NULL,
                        updated_at = ?
                    WHERE release_id = ?
                    """,
                    (
                        card_revision,
                        delivery_kind,
                        process_instance_id,
                        lease_expires_at,
                        target_chat_id,
                        target_thread_id,
                        card_revision,
                        now_iso,
                        release_id,
                    ),
                )
                minted_ids = self._insert_actions_locked(conn, new_actions, now_iso=now_iso)
                conn.execute("COMMIT;")
                return CardDeliveryPlan(mode="send_new", newly_minted_action_ids=minted_ids)

            # Brand new delivery row
            conn.execute(
                """
                INSERT INTO deliveries (
                    release_id, card_revision, delivery_kind, status,
                    process_instance_id, lease_expires_at,
                    target_chat_id, target_thread_id,
                    used_poster, eval_reply_status, eval_reply_card_revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'sending', ?, ?, ?, ?, 0, 'pending', ?, ?, ?)
                """,
                (
                    release_id,
                    card_revision,
                    delivery_kind,
                    process_instance_id,
                    lease_expires_at,
                    target_chat_id,
                    target_thread_id,
                    card_revision,
                    now_iso,
                    now_iso,
                ),
            )
            minted_ids = self._insert_actions_locked(conn, new_actions, now_iso=now_iso)
            conn.execute("COMMIT;")
            return CardDeliveryPlan(mode="send_new", newly_minted_action_ids=minted_ids)
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def finish_card_delivery_success(
        self,
        release_id: str,
        *,
        card_revision: int,
        delivery_kind: str,
        ack_chat_id: str,
        ack_message_id: int,
        ack_thread_id: Optional[int],
        used_poster: bool,
        is_edit: bool,
        preserve_eval_reply: bool = False,
    ) -> bool:
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            preserved = False
            if is_edit:
                if preserve_eval_reply:
                    cur = conn.execute(
                        "SELECT eval_reply_status FROM deliveries WHERE release_id = ?",
                        (release_id,),
                    ).fetchone()
                    if cur is not None and str(cur["eval_reply_status"]) in ("sent", "ambiguous_reply"):
                        preserved = True
                if preserved:
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET card_revision = ?,
                            delivery_kind = ?,
                            status = 'delivered',
                            process_instance_id = NULL,
                            lease_expires_at = NULL,
                            ack_chat_id = ?,
                            ack_message_id = ?,
                            ack_thread_id = ?,
                            used_poster = ?,
                            eval_reply_card_revision = ?,
                            last_error_redacted = NULL,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (
                            card_revision,
                            delivery_kind,
                            ack_chat_id,
                            ack_message_id,
                            ack_thread_id,
                            1 if used_poster else 0,
                            card_revision,
                            now_iso,
                            release_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET card_revision = ?,
                            delivery_kind = ?,
                            status = 'delivered',
                            process_instance_id = NULL,
                            lease_expires_at = NULL,
                            ack_chat_id = ?,
                            ack_message_id = ?,
                            ack_thread_id = ?,
                            used_poster = ?,
                            eval_reply_status = 'pending',
                            eval_reply_card_revision = ?,
                            eval_reply_attempt_id = NULL,
                            eval_reply_process_instance_id = NULL,
                            eval_reply_lease_expires_at = NULL,
                            eval_reply_message_id = NULL,
                            eval_doc_message_id = NULL,
                            last_error_redacted = NULL,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (
                            card_revision,
                            delivery_kind,
                            ack_chat_id,
                            ack_message_id,
                            ack_thread_id,
                            1 if used_poster else 0,
                            card_revision,
                            now_iso,
                            release_id,
                        ),
                    )
            else:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET card_revision = ?,
                        delivery_kind = ?,
                        status = 'delivered',
                        process_instance_id = NULL,
                        lease_expires_at = NULL,
                        ack_chat_id = ?,
                        ack_message_id = ?,
                        ack_thread_id = ?,
                        used_poster = ?,
                        last_error_redacted = NULL,
                        updated_at = ?
                    WHERE release_id = ?
                    """,
                    (
                        card_revision,
                        delivery_kind,
                        ack_chat_id,
                        ack_message_id,
                        ack_thread_id,
                        1 if used_poster else 0,
                        now_iso,
                        release_id,
                    ),
                )
            conn.execute("COMMIT;")
            return preserved
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def finish_card_delivery_failed(
        self,
        release_id: str,
        *,
        error_redacted: str,
        is_edit: bool,
        restore_card_revision: int,
        restore_delivery_kind: str,
        superseded_action_ids: tuple[str, ...],
        newly_minted_action_ids: tuple[str, ...],
        keep_prior_superseded: bool,
    ) -> None:
        now_iso = utc_now_iso()
        clean_err = redact_secrets(error_redacted, max_chars=240)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            if newly_minted_action_ids:
                placeholders = ",".join("?" for _ in newly_minted_action_ids)
                conn.execute(
                    f"UPDATE actions SET status = 'superseded', updated_at = ? WHERE action_id IN ({placeholders})",
                    [now_iso, *newly_minted_action_ids],
                )
            if is_edit:
                if keep_prior_superseded:
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET status = 'edit_failed',
                            process_instance_id = NULL,
                            lease_expires_at = NULL,
                            last_error_redacted = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (clean_err, now_iso, release_id),
                    )
                else:
                    if superseded_action_ids:
                        placeholders = ",".join("?" for _ in superseded_action_ids)
                        conn.execute(
                            f"""
                            UPDATE actions
                            SET status = 'active', updated_at = ?
                            WHERE action_id IN ({placeholders}) AND status = 'superseded'
                            """,
                            [now_iso, *superseded_action_ids],
                        )
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET card_revision = ?,
                            delivery_kind = ?,
                            status = 'delivered',
                            process_instance_id = NULL,
                            lease_expires_at = NULL,
                            last_error_redacted = ?,
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (restore_card_revision, restore_delivery_kind, clean_err, now_iso, release_id),
                    )
            else:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'failed',
                        process_instance_id = NULL,
                        lease_expires_at = NULL,
                        ack_chat_id = NULL,
                        ack_message_id = NULL,
                        ack_thread_id = NULL,
                        last_error_redacted = ?,
                        updated_at = ?
                    WHERE release_id = ?
                    """,
                    (clean_err, now_iso, release_id),
                )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def finish_card_delivery_ambiguous(
        self,
        release_id: str,
        *,
        error_redacted: str,
        is_edit: bool,
    ) -> None:
        now_iso = utc_now_iso()
        clean_err = redact_secrets(error_redacted, max_chars=240)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            if is_edit:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'ambiguous_delivery',
                        process_instance_id = NULL,
                        lease_expires_at = NULL,
                        last_error_redacted = ?,
                        updated_at = ?
                    WHERE release_id = ?
                    """,
                    (clean_err, now_iso, release_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE deliveries
                    SET status = 'ambiguous_delivery',
                        process_instance_id = NULL,
                        lease_expires_at = NULL,
                        ack_chat_id = NULL,
                        ack_message_id = NULL,
                        ack_thread_id = NULL,
                        last_error_redacted = ?,
                        updated_at = ?
                    WHERE release_id = ?
                    """,
                    (clean_err, now_iso, release_id),
                )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def begin_eval_reply(
        self,
        *,
        release_id: str,
        card_revision: int,
        expected_ack_chat_id: Optional[str],
        expected_ack_message_id: Optional[int],
        expected_ack_thread_id: Optional[int] = None,
        process_instance_id: str,
        lease_expires_at: str,
    ) -> EvalReplyPlan:
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT;")
                return EvalReplyPlan(mode="reject_missing", detail="delivery_not_found")

            # Promote stale eval_reply_status='sending' lease
            if str(row["eval_reply_status"]) == "sending":
                r_proc = row["eval_reply_process_instance_id"]
                r_lease = row["eval_reply_lease_expires_at"]
                if (
                    not r_proc
                    or str(r_proc) != process_instance_id
                    or not r_lease
                    or str(r_lease) <= now_iso
                ):
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET eval_reply_status = 'ambiguous_reply',
                            eval_reply_attempt_id = NULL,
                            eval_reply_process_instance_id = NULL,
                            eval_reply_lease_expires_at = NULL,
                            last_error_redacted = 'stale_reply_lease_promoted_to_ambiguous',
                            updated_at = ?
                        WHERE release_id = ?
                        """,
                        (now_iso, release_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM deliveries WHERE release_id = ?",
                        (release_id,),
                    ).fetchone()

            del_status = str(row["status"])
            ack_chat = str(row["ack_chat_id"] or "")
            ack_msg = row["ack_message_id"]
            ack_thread = row["ack_thread_id"]
            target_thread = row["target_thread_id"]
            if (
                del_status == "ambiguous_delivery"
                or del_status != "delivered"
                or not ack_chat
                or ack_msg is None
                or int(ack_msg) <= 0
            ):
                conn.execute("COMMIT;")
                return EvalReplyPlan(
                    mode="reject_unanchored",
                    detail=f"cannot_reply_to_delivery_status:{del_status}",
                )

            # Issue #13 §6c: Distinguish topic chats (`target_thread_id IS NOT NULL`) from known
            # non-topic chats (`target_thread_id IS NULL`). If a topic was requested but Telegram
            # omitted `message_thread_id` in its acknowledgement, topic correlation is unknown and
            # dependent replies MUST NOT guess the destination.
            if target_thread is not None and ack_thread is None:
                conn.execute("COMMIT;")
                return EvalReplyPlan(
                    mode="reject_unanchored",
                    detail="ack_thread_id_omitted_for_topic_chat",
                )

            if expected_ack_message_id is not None and int(expected_ack_message_id) != int(ack_msg):
                conn.execute("COMMIT;")
                return EvalReplyPlan(
                    mode="reject_receipt_mismatch",
                    detail="ack_message_id_mismatch",
                )
            if expected_ack_chat_id and str(expected_ack_chat_id).strip() != ack_chat:
                conn.execute("COMMIT;")
                return EvalReplyPlan(
                    mode="reject_receipt_mismatch",
                    detail="ack_chat_id_mismatch",
                )
            if expected_ack_thread_id is not None and (
                ack_thread is None or int(expected_ack_thread_id) != int(ack_thread)
            ):
                conn.execute("COMMIT;")
                return EvalReplyPlan(
                    mode="reject_receipt_mismatch",
                    detail="ack_thread_id_mismatch",
                )

            current_card_rev = int(row["card_revision"] or 0)
            if card_revision < current_card_rev:
                conn.execute("COMMIT;")
                return EvalReplyPlan(
                    mode="reject_receipt_mismatch",
                    detail=f"stale_card_revision:{card_revision}<{current_card_rev}",
                )

            reply_status = str(row["eval_reply_status"])
            reply_rev = int(row["eval_reply_card_revision"] or 0)
            existing_reply_msg_id = (
                int(row["eval_reply_message_id"])
                if row["eval_reply_message_id"] is not None
                else None
            )

            if reply_rev == card_revision:
                if reply_status == "sent":
                    conn.execute("COMMIT;")
                    return EvalReplyPlan(
                        mode="already_sent",
                        ack_chat_id=ack_chat,
                        ack_message_id=int(ack_msg),
                        ack_thread_id=int(ack_thread) if ack_thread is not None else None,
                        existing_eval_reply_message_id=existing_reply_msg_id,
                    )
                if reply_status == "ambiguous_reply":
                    conn.execute("COMMIT;")
                    return EvalReplyPlan(mode="ambiguous", detail="existing_ambiguous_reply")
                if reply_status == "sending":
                    conn.execute("COMMIT;")
                    return EvalReplyPlan(mode="in_flight", detail="reply_already_in_flight")
            else:
                # Fresh revision: clear any stale message IDs from earlier revision
                existing_reply_msg_id = None

            reply_attempt_id = generate_uuid7()
            conn.execute(
                """
                UPDATE deliveries
                SET eval_reply_status = 'sending',
                    eval_reply_card_revision = ?,
                    eval_reply_attempt_id = ?,
                    eval_reply_process_instance_id = ?,
                    eval_reply_lease_expires_at = ?,
                    eval_reply_message_id = ?,
                    updated_at = ?
                WHERE release_id = ?
                """,
                (
                    card_revision,
                    reply_attempt_id,
                    process_instance_id,
                    lease_expires_at,
                    existing_reply_msg_id,
                    now_iso,
                    release_id,
                ),
            )
            conn.execute("COMMIT;")
            return EvalReplyPlan(
                mode="send",
                ack_chat_id=ack_chat,
                ack_message_id=int(ack_msg),
                ack_thread_id=int(ack_thread) if ack_thread is not None else None,
                existing_eval_reply_message_id=existing_reply_msg_id,
                reply_attempt_id=reply_attempt_id,
            )
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def record_eval_reply_text_sent(
        self,
        release_id: str,
        *,
        card_revision: int,
        reply_attempt_id: str,
        reply_message_id: int,
    ) -> bool:
        """Persist reply_message_id ONLY if (card_revision, reply_attempt_id) still owns the sending claim."""
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                """
                UPDATE deliveries
                SET eval_reply_message_id = ?,
                    updated_at = ?
                WHERE release_id = ?
                  AND eval_reply_card_revision = ?
                  AND eval_reply_attempt_id = ?
                  AND eval_reply_status = 'sending'
                """,
                (reply_message_id, now_iso, release_id, card_revision, reply_attempt_id),
            )
            conn.execute("COMMIT;")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def finish_eval_reply(
        self,
        release_id: str,
        *,
        card_revision: int,
        reply_attempt_id: str,
        status: str,
        reply_message_id: Optional[int] = None,
        doc_message_id: Optional[int] = None,
        error_redacted: Optional[str] = None,
    ) -> bool:
        """Finalize eval reply ONLY if (card_revision, reply_attempt_id) still owns the sending claim."""
        now_iso = utc_now_iso()
        clean_err = redact_secrets(error_redacted, max_chars=240) if error_redacted else None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                """
                UPDATE deliveries
                SET eval_reply_status = ?,
                    eval_reply_attempt_id = NULL,
                    eval_reply_process_instance_id = NULL,
                    eval_reply_lease_expires_at = NULL,
                    eval_reply_message_id = COALESCE(?, eval_reply_message_id),
                    eval_doc_message_id = COALESCE(?, eval_doc_message_id),
                    last_error_redacted = ?,
                    updated_at = ?
                WHERE release_id = ?
                  AND eval_reply_card_revision = ?
                  AND eval_reply_attempt_id = ?
                  AND eval_reply_status = 'sending'
                """,
                (
                    status,
                    reply_message_id,
                    doc_message_id,
                    clean_err,
                    now_iso,
                    release_id,
                    card_revision,
                    reply_attempt_id,
                ),
            )
            conn.execute("COMMIT;")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def get_action_group_readonly(self, token_hash: str) -> ActionGroupSnapshot:
        """Strictly read-only lookup for GET /action/{token} (zero SQLite mutations)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM actions WHERE token_sha256 = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return ActionGroupSnapshot(action=None, group_actions=(), group_attempts=())
            action = dict(row)
            group = str(action["completion_group"])
            release_id = str(action["release_id"])
            g_actions = conn.execute(
                "SELECT * FROM actions WHERE completion_group = ? ORDER BY created_at ASC, action_id ASC",
                (group,),
            ).fetchall()
            g_attempts = conn.execute(
                """
                SELECT * FROM write_attempts
                WHERE completion_group = ?
                ORDER BY started_at DESC, attempt_id DESC
                """,
                (group,),
            ).fetchall()
            del_row = conn.execute(
                "SELECT * FROM deliveries WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            rel_actions = conn.execute(
                "SELECT * FROM actions WHERE release_id = ? ORDER BY created_at ASC, action_id ASC",
                (release_id,),
            ).fetchall()
            return ActionGroupSnapshot(
                action=action,
                group_actions=tuple(dict(r) for r in g_actions),
                group_attempts=tuple(dict(r) for r in g_attempts),
                delivery=dict(del_row) if del_row is not None else None,
                release_actions=tuple(dict(r) for r in rel_actions),
            )
        finally:
            conn.close()

    def inspect_action_group_for_post(
        self,
        token_hash: str,
        *,
        current_process_instance_id: str,
        now_iso: Optional[str] = None,
    ) -> ActionGroupSnapshot:
        """Atomically promote any expired/orphaned attempts on release/group and return snapshot."""
        ts = now_iso or utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                "SELECT * FROM actions WHERE token_sha256 = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT;")
                return ActionGroupSnapshot(action=None, group_actions=(), group_attempts=())
            group = str(row["completion_group"])
            release_id = str(row["release_id"])
            promoted = self._promote_stale_attempts_locked(
                conn,
                current_process_instance_id=current_process_instance_id,
                now_iso=ts,
                release_id=release_id,
            )
            action = dict(
                conn.execute(
                    "SELECT * FROM actions WHERE token_sha256 = ?",
                    (token_hash,),
                ).fetchone()
            )
            g_actions = conn.execute(
                "SELECT * FROM actions WHERE completion_group = ? ORDER BY created_at ASC, action_id ASC",
                (group,),
            ).fetchall()
            g_attempts = conn.execute(
                """
                SELECT * FROM write_attempts
                WHERE completion_group = ?
                ORDER BY started_at DESC, attempt_id DESC
                """,
                (group,),
            ).fetchall()
            del_row = conn.execute(
                "SELECT * FROM deliveries WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            rel_actions = conn.execute(
                "SELECT * FROM actions WHERE release_id = ? ORDER BY created_at ASC, action_id ASC",
                (release_id,),
            ).fetchall()
            conn.execute("COMMIT;")
            return ActionGroupSnapshot(
                action=action,
                group_actions=tuple(dict(r) for r in g_actions),
                group_attempts=tuple(dict(r) for r in g_attempts),
                promoted_stale_attempts=promoted,
                delivery=dict(del_row) if del_row is not None else None,
                release_actions=tuple(dict(r) for r in rel_actions),
            )
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def mark_action_superseded(self, *, action_id: str, detail: Optional[str] = None) -> bool:
        """Mark an active action superseded when its card revision is stale or retired."""
        ts = utc_now_iso()
        clean_err = redact_secrets(detail, max_chars=240) if detail else None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                """
                UPDATE actions
                SET status = 'superseded',
                    last_error_redacted = COALESCE(?, last_error_redacted),
                    updated_at = ?
                WHERE action_id = ? AND status = 'active'
                """,
                (clean_err, ts, action_id),
            )
            conn.execute("COMMIT;")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def begin_write_attempt(
        self,
        *,
        action_id: str,
        completion_group: str,
        phase: str,
        process_instance_id: str,
        lease_expires_at: str,
        request_fingerprint: str,
        allowed_action_statuses: frozenset[str],
        forbid_group_completed_radarr: bool = True,
        selected_root_folder: Optional[str] = None,
        selected_quality_profile_id: Optional[int] = None,
        radarr_movie_id: Optional[int] = None,
        now_iso: Optional[str] = None,
    ) -> tuple[bool, Optional[str], Optional[str]]:
        """
        Atomically commit write-ahead `write_attempts(status='attempting')` BEFORE any external write.
        Serializes concurrent submissions across the entire `completion_group` via BEGIN IMMEDIATE.
        Returns (ok, attempt_id, conflict_code).
        """
        ts = now_iso or utc_now_iso()
        attempt_id = generate_uuid7()
        clean_fp = redact_secrets(request_fingerprint, max_chars=800)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            action_row = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if action_row is None:
                conn.execute("COMMIT;")
                return False, None, "not_found"

            release_id = str(action_row["release_id"])
            self._promote_stale_attempts_locked(
                conn,
                current_process_instance_id=process_instance_id,
                now_iso=ts,
                release_id=release_id,
            )

            action_row = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if action_row is None:
                conn.execute("COMMIT;")
                return False, None, "not_found"

            act_status = str(action_row["status"])
            if act_status not in allowed_action_statuses:
                conn.execute("COMMIT;")
                return False, None, f"status:{act_status}"

            # Issue #13 §2b & PR #18 Blocker 2:
            # 1) For fresh initiation (`active`) OR any pre-push/acquisition claim (including Phase A
            #    timeout recovery), atomically re-verify latest delivery revision/kind so a retired
            #    or superseded card can NEVER start a release push or qBittorrent add.
            is_pre_push_phase = phase in (
                "radarr_add_movie",
                "radarr_release_push",
                "qbit_alt_preflight",
                "qbit_alternative_grab",
            )
            if act_status == "active" or is_pre_push_phase:
                del_row = conn.execute(
                    "SELECT card_revision, delivery_kind, status FROM deliveries WHERE release_id = ?",
                    (release_id,),
                ).fetchone()
                if del_row is not None:
                    latest_rev = int(del_row["card_revision"] or 0)
                    latest_kind = str(del_row["delivery_kind"] or "")
                    if latest_rev > int(action_row["card_revision"] or 0):
                        conn.execute(
                            "UPDATE actions SET status = 'superseded', updated_at = ? WHERE action_id = ? AND status IN ('active', 'ambiguous_radarr')",
                            (ts, action_id),
                        )
                        conn.execute("COMMIT;")
                        return False, None, "stale_card_revision"
                    if latest_kind in ("retired", "evidence_review"):
                        conn.execute(
                            "UPDATE actions SET status = 'superseded', updated_at = ? WHERE action_id = ? AND status IN ('active', 'ambiguous_radarr')",
                            (ts, action_id),
                        )
                        conn.execute("COMMIT;")
                        return False, None, f"delivery_{latest_kind}"

            # 2) Bidirectional mutual exclusion between acquisition pushes and Watched choices:
            #    - Any acquisition phase is blocked once a sibling watched_* started/completed/ambiguous.
            #    - Any radarr_watched phase is blocked once an irreversible push/grab
            #      (radarr_release_push / qbit_alternative_grab) has entered attempting/ambiguous/succeeded
            #      or add_grab/alternative_grab completed.
            if is_pre_push_phase:
                watched_sib = conn.execute(
                    """
                    SELECT action_id, status FROM actions
                    WHERE release_id = ?
                      AND action_type IN ('watched_good', 'watched_bad', 'watched_legacy_unrated')
                      AND status IN ('attempting', 'ambiguous_radarr', 'completed_radarr', 'completed_all')
                    LIMIT 1
                    """,
                    (release_id,),
                ).fetchone()
                if watched_sib is not None:
                    conn.execute("COMMIT;")
                    return False, None, f"sibling_watched:{watched_sib['status']}"

            if phase == "radarr_watched":
                grab_push_sib = conn.execute(
                    """
                    SELECT a.action_id, a.status
                    FROM actions a
                    WHERE a.release_id = ?
                      AND a.action_type IN ('add_grab', 'alternative_grab')
                      AND (
                          a.status IN ('completed_radarr', 'completed_all')
                          OR EXISTS (
                              SELECT 1 FROM write_attempts w
                              WHERE w.action_id = a.action_id
                                AND w.phase IN ('radarr_release_push', 'qbit_alternative_grab')
                                AND w.status IN ('attempting', 'ambiguous', 'succeeded')
                          )
                      )
                    LIMIT 1
                    """,
                    (release_id,),
                ).fetchone()
                if grab_push_sib is not None:
                    conn.execute("COMMIT;")
                    return False, None, f"sibling_grab:{grab_push_sib['status']}"

            # Check if another attempt is currently active in this completion_group
            active_att = conn.execute(
                """
                SELECT attempt_id, phase FROM write_attempts
                WHERE completion_group = ? AND status = 'attempting'
                LIMIT 1
                """,
                (completion_group,),
            ).fetchone()
            if active_att is not None:
                conn.execute("COMMIT;")
                return False, None, "in_progress"

            if forbid_group_completed_radarr:
                sib_done = conn.execute(
                    """
                    SELECT action_id, status FROM actions
                    WHERE completion_group = ?
                      AND status IN ('completed_radarr', 'completed_all')
                    LIMIT 1
                    """,
                    (completion_group,),
                ).fetchone()
                if sib_done is not None:
                    conn.execute("COMMIT;")
                    return False, None, f"group_completed:{sib_done['status']}"

                sib_amb = conn.execute(
                    """
                    SELECT action_id FROM actions
                    WHERE completion_group = ?
                      AND action_id != ?
                      AND status = 'ambiguous_radarr'
                    LIMIT 1
                    """,
                    (completion_group, action_id),
                ).fetchone()
                if sib_amb is not None:
                    conn.execute("COMMIT;")
                    return False, None, "group_ambiguous_radarr"

            # Hard single-push invariant (Codex Round 1 P1 #1):
            # Once ANY radarr_release_push in this completion_group has entered attempting,
            # ambiguous, or succeeded, NEVER permit a second push or re-entering Phase 1.
            if phase in ("radarr_release_push", "radarr_add_movie"):
                prior_push = conn.execute(
                    """
                    SELECT attempt_id, status FROM write_attempts
                    WHERE completion_group = ?
                      AND phase = 'radarr_release_push'
                      AND status IN ('attempting', 'ambiguous', 'succeeded')
                    LIMIT 1
                    """,
                    (completion_group,),
                ).fetchone()
                if prior_push is not None:
                    conn.execute("COMMIT;")
                    return False, None, f"prior_release_push:{prior_push['status']}"

            if phase in ("qbit_alternative_grab", "qbit_alt_preflight"):
                prior_qbit = conn.execute(
                    """
                    SELECT attempt_id, status FROM write_attempts
                    WHERE completion_group = ?
                      AND phase = 'qbit_alternative_grab'
                      AND status IN ('attempting', 'ambiguous', 'succeeded')
                    LIMIT 1
                    """,
                    (completion_group,),
                ).fetchone()
                if prior_qbit is not None:
                    conn.execute("COMMIT;")
                    return False, None, f"prior_qbit_add:{prior_qbit['status']}"

            # Update parent action state atomically with write_attempts insert
            if phase in RADARR_PHASES:
                conn.execute(
                    """
                    UPDATE actions
                    SET status = 'attempting',
                        selected_root_folder = COALESCE(?, selected_root_folder),
                        selected_quality_profile_id = COALESCE(?, selected_quality_profile_id),
                        radarr_movie_id = COALESCE(?, radarr_movie_id),
                        updated_at = ?
                    WHERE action_id = ?
                    """,
                    (
                        selected_root_folder,
                        selected_quality_profile_id,
                        radarr_movie_id,
                        ts,
                        action_id,
                    ),
                )
            elif phase == "evaluator_feedback":
                if str(action_row["action_type"]) == "skip":
                    conn.execute(
                        """
                        UPDATE actions
                        SET status = 'attempting',
                            feedback_status = 'pending',
                            updated_at = ?
                        WHERE action_id = ?
                        """,
                        (ts, action_id),
                    )
                else:
                    # Radarr action stays permanently sealed at completed_radarr
                    conn.execute(
                        """
                        UPDATE actions
                        SET feedback_status = 'pending',
                            updated_at = ?
                        WHERE action_id = ?
                        """,
                        (ts, action_id),
                    )

            conn.execute(
                """
                INSERT INTO write_attempts (
                    attempt_id, action_id, completion_group, phase, status,
                    process_instance_id, lease_expires_at, request_fingerprint,
                    started_at
                ) VALUES (?, ?, ?, ?, 'attempting', ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    action_id,
                    completion_group,
                    phase,
                    process_instance_id,
                    lease_expires_at,
                    clean_fp,
                    ts,
                ),
            )
            conn.execute("COMMIT;")
            return True, attempt_id, None
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def finish_write_attempt(
        self,
        *,
        attempt_id: str,
        action_id: str,
        attempt_status: str,
        action_status: Optional[str] = None,
        outcome_summary: Optional[str] = None,
        radarr_movie_id: Optional[int] = None,
        feedback_status: Optional[str] = None,
        feedback_decision_saved: Optional[str] = None,
        last_reconciled_at: Optional[str] = None,
        last_error_redacted: Optional[str] = None,
    ) -> bool:
        """Finalize an active attempting write_attempt; returns False if the attempt claim was already promoted/closed."""
        now_iso = utc_now_iso()
        clean_outcome = redact_secrets(outcome_summary, max_chars=300) if outcome_summary else None
        clean_err = redact_secrets(last_error_redacted, max_chars=300) if last_error_redacted else None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                """
                UPDATE write_attempts
                SET status = ?,
                    outcome_summary = ?,
                    finished_at = ?
                WHERE attempt_id = ?
                  AND action_id = ?
                  AND status = 'attempting'
                """,
                (attempt_status, clean_outcome, now_iso, attempt_id, action_id),
            )
            if cur.rowcount != 1:
                conn.execute("COMMIT;")
                return False

            updates: list[str] = ["updated_at = ?"]
            params: list[Any] = [now_iso]
            if action_status is not None:
                updates.append("status = ?")
                params.append(action_status)
            if radarr_movie_id is not None:
                updates.append("radarr_movie_id = ?")
                params.append(radarr_movie_id)
            if feedback_status is not None:
                updates.append("feedback_status = ?")
                params.append(feedback_status)
            if feedback_decision_saved is not None:
                updates.append("feedback_decision_saved = ?")
                params.append(feedback_decision_saved)
            if last_reconciled_at is not None:
                updates.append("last_reconciled_at = ?")
                params.append(last_reconciled_at)
            if last_error_redacted is not None:
                updates.append("last_error_redacted = ?")
                params.append(clean_err)
            elif attempt_status == "succeeded":
                updates.append("last_error_redacted = NULL")
            params.append(action_id)
            conn.execute(
                f"UPDATE actions SET {', '.join(updates)} WHERE action_id = ? AND status != 'completed_all'",
                params,
            )
            conn.execute("COMMIT;")
            return True
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def seal_reconciled_attempt(
        self,
        *,
        attempt_id: Optional[str],
        action_id: str,
        radarr_movie_id: Optional[int],
        outcome_summary: str,
        needs_feedback: bool,
    ) -> bool:
        """
        Atomically transition an `ambiguous_radarr` action to `completed_radarr`/`completed_all`.
        Returns False if the action (or a completion_group sibling) is no longer `ambiguous_radarr`,
        preserving terminal completion states and preventing duplicate feedback posts.
        """
        now_iso = utc_now_iso()
        clean_outcome = redact_secrets(outcome_summary, max_chars=300)
        new_action_status = "completed_radarr" if needs_feedback else "completed_all"
        new_feedback_status = "pending" if needs_feedback else "not_applicable"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            act_row = conn.execute(
                "SELECT status, completion_group FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if act_row is None or str(act_row["status"]) != "ambiguous_radarr":
                conn.execute("COMMIT;")
                return False

            sib_done = conn.execute(
                """
                SELECT action_id FROM actions
                WHERE completion_group = ?
                  AND status IN ('completed_radarr', 'completed_all')
                LIMIT 1
                """,
                (str(act_row["completion_group"]),),
            ).fetchone()
            if sib_done is not None:
                conn.execute("COMMIT;")
                return False

            if attempt_id:
                conn.execute(
                    """
                    UPDATE write_attempts
                    SET status = 'succeeded',
                        outcome_summary = ?,
                        finished_at = COALESCE(finished_at, ?)
                    WHERE attempt_id = ? AND action_id = ? AND status = 'ambiguous'
                    """,
                    (clean_outcome, now_iso, attempt_id, action_id),
                )
            cur = conn.execute(
                """
                UPDATE actions
                SET status = ?,
                    radarr_movie_id = COALESCE(?, radarr_movie_id),
                    feedback_status = CASE
                        WHEN feedback_status = 'saved' THEN 'saved'
                        ELSE ?
                    END,
                    last_reconciled_at = ?,
                    last_error_redacted = NULL,
                    updated_at = ?
                WHERE action_id = ?
                  AND status = 'ambiguous_radarr'
                """,
                (
                    new_action_status,
                    radarr_movie_id,
                    new_feedback_status,
                    now_iso,
                    now_iso,
                    action_id,
                ),
            )
            conn.execute("COMMIT;")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    @staticmethod
    def _check_pre_push_db_authorization_locked(
        conn: sqlite3.Connection,
        action_row: sqlite3.Row,
        *,
        now_iso: str,
    ) -> Optional[str]:
        """
        Atomically verify inside BEGIN IMMEDIATE that a release is still authorized to enter
        its FIRST release push / qBittorrent add (latest revision, not retired/evidence_review,
        not expired, and no conflicting sibling watched_* action).
        Returns None when authorized, or a short disqualification reason code.
        """
        release_id = str(action_row["release_id"])
        act_rev = int(action_row["card_revision"] or 0)
        del_row = conn.execute(
            "SELECT card_revision, delivery_kind, status FROM deliveries WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if del_row is not None:
            latest_rev = int(del_row["card_revision"] or 0)
            latest_kind = str(del_row["delivery_kind"] or "")
            if latest_rev > act_rev:
                return "stale_card_revision"
            if latest_kind in ("retired", "evidence_review"):
                return f"delivery_{latest_kind}"

        exp_raw = action_row["expires_at"]
        if isinstance(exp_raw, str) and exp_raw.strip() and exp_raw <= now_iso:
            return "expired"

        watched_sib = conn.execute(
            """
            SELECT action_id, status FROM actions
            WHERE release_id = ?
              AND action_type IN ('watched_good', 'watched_bad', 'watched_legacy_unrated')
              AND status IN ('attempting', 'ambiguous_radarr', 'completed_radarr', 'completed_all')
            LIMIT 1
            """,
            (release_id,),
        ).fetchone()
        if watched_sib is not None:
            return f"sibling_watched:{watched_sib['status']}"
        return None

    def mark_phase1_add_movie_reconciled(
        self,
        *,
        attempt_id: Optional[str],
        action_id: str,
        completion_group: str,
        radarr_movie_id: int,
        outcome_summary: str,
    ) -> bool:
        """
        Mark ambiguous Phase 1 (radarr_add_movie) succeeded before entering Phase 2.
        Returns False if a prior radarr_release_push attempt already exists (attempting/ambiguous/succeeded),
        if the action is no longer ambiguous_radarr, or if the release was retired/superseded/watched.
        """
        now_iso = utc_now_iso()
        clean_outcome = redact_secrets(outcome_summary, max_chars=300)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            act_row = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if act_row is None or str(act_row["status"]) != "ambiguous_radarr":
                conn.execute("COMMIT;")
                return False
            disqual = self._check_pre_push_db_authorization_locked(conn, act_row, now_iso=now_iso)
            if disqual is not None:
                if disqual in ("stale_card_revision", "delivery_retired", "delivery_evidence_review"):
                    conn.execute(
                        "UPDATE actions SET status = 'superseded', last_error_redacted = ?, updated_at = ? WHERE action_id = ? AND status = 'ambiguous_radarr'",
                        (f"blocked_before_push:{disqual}", now_iso, action_id),
                    )
                conn.execute("COMMIT;")
                return False
            prior_push = conn.execute(
                """
                SELECT attempt_id FROM write_attempts
                WHERE completion_group = ?
                  AND phase = 'radarr_release_push'
                  AND status IN ('attempting', 'ambiguous', 'succeeded')
                LIMIT 1
                """,
                (completion_group,),
            ).fetchone()
            if prior_push is not None:
                conn.execute("COMMIT;")
                return False
            if attempt_id:
                conn.execute(
                    """
                    UPDATE write_attempts
                    SET status = 'succeeded',
                        outcome_summary = ?,
                        finished_at = COALESCE(finished_at, ?)
                    WHERE attempt_id = ? AND status IN ('ambiguous', 'succeeded')
                    """,
                    (clean_outcome, now_iso, attempt_id),
                )
            conn.execute(
                """
                UPDATE actions
                SET radarr_movie_id = ?,
                    last_reconciled_at = ?,
                    last_error_redacted = NULL,
                    updated_at = ?
                WHERE action_id = ? AND status = 'ambiguous_radarr'
                """,
                (radarr_movie_id, now_iso, now_iso, action_id),
            )
            conn.execute("COMMIT;")
            return True
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def mark_action_reconciled_still_ambiguous(
        self,
        *,
        action_id: str,
        detail: str,
    ) -> bool:
        """
        Record an absent reconciliation check ONLY while the action is still `ambiguous_radarr`.
        Never downgrades a concurrently completed (`completed_radarr`/`completed_all`) action.
        """
        now_iso = utc_now_iso()
        clean = redact_secrets(detail, max_chars=300)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                """
                UPDATE actions
                SET status = 'ambiguous_radarr',
                    last_reconciled_at = ?,
                    last_error_redacted = ?,
                    updated_at = ?
                WHERE action_id = ?
                  AND status = 'ambiguous_radarr'
                """,
                (now_iso, clean, now_iso, action_id),
            )
            conn.execute("COMMIT;")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def complete_phase1_and_begin_phase2(
        self,
        *,
        phase1_attempt_id: str,
        action_id: str,
        completion_group: str,
        radarr_movie_id: int,
        phase1_outcome: str,
        process_instance_id: str,
        phase2_lease_expires_at: str,
        phase2_request_fingerprint: str,
    ) -> Optional[str]:
        """
        Atomically mark Phase 1 (radarr_add_movie) succeeded and open Phase 2 (radarr_release_push).
        Returns None if a prior radarr_release_push attempt already exists in completion_group,
        if phase1_attempt_id no longer owns the active attempting claim, or if the release was
        retired/superseded/expired or blocked by a sibling watched action while Phase 1 ran.
        """
        now_iso = utc_now_iso()
        phase2_attempt_id = generate_uuid7()
        clean_outcome = redact_secrets(phase1_outcome, max_chars=300)
        clean_fp = redact_secrets(phase2_request_fingerprint, max_chars=800)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            p1_row = conn.execute(
                """
                SELECT status FROM write_attempts
                WHERE attempt_id = ? AND action_id = ? AND phase = 'radarr_add_movie'
                """,
                (phase1_attempt_id, action_id),
            ).fetchone()
            if p1_row is None or str(p1_row["status"]) != "attempting":
                conn.execute("COMMIT;")
                return None

            act_row = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if act_row is None or str(act_row["status"]) != "attempting":
                conn.execute("COMMIT;")
                return None

            disqual = self._check_pre_push_db_authorization_locked(conn, act_row, now_iso=now_iso)
            if disqual is not None:
                # Phase 1 add_movie completed on Radarr, but the action is NO LONGER authorized
                # to push a release (retired card, newer revision, expired, or Watched won).
                # Close Phase 1 cleanly and supersede this action so zero push can proceed.
                conn.execute(
                    """
                    UPDATE write_attempts
                    SET status = 'succeeded',
                        outcome_summary = ?,
                        finished_at = ?
                    WHERE attempt_id = ?
                    """,
                    (redact_secrets(f"{clean_outcome};blocked_before_push:{disqual}", max_chars=300), now_iso, phase1_attempt_id),
                )
                conn.execute(
                    """
                    UPDATE actions
                    SET status = 'superseded',
                        radarr_movie_id = ?,
                        last_error_redacted = ?,
                        updated_at = ?
                    WHERE action_id = ? AND status = 'attempting'
                    """,
                    (radarr_movie_id, redact_secrets(f"blocked_before_push:{disqual}", max_chars=300), now_iso, action_id),
                )
                conn.execute("COMMIT;")
                return None

            prior_push = conn.execute(
                """
                SELECT attempt_id FROM write_attempts
                WHERE completion_group = ?
                  AND phase = 'radarr_release_push'
                  AND status IN ('attempting', 'ambiguous', 'succeeded')
                LIMIT 1
                """,
                (completion_group,),
            ).fetchone()
            if prior_push is not None:
                conn.execute("COMMIT;")
                return None

            conn.execute(
                """
                UPDATE write_attempts
                SET status = 'succeeded',
                    outcome_summary = ?,
                    finished_at = ?
                WHERE attempt_id = ?
                """,
                (clean_outcome, now_iso, phase1_attempt_id),
            )
            conn.execute(
                """
                UPDATE actions
                SET status = 'attempting',
                    radarr_movie_id = ?,
                    last_error_redacted = NULL,
                    updated_at = ?
                WHERE action_id = ?
                """,
                (radarr_movie_id, now_iso, action_id),
            )
            conn.execute(
                """
                INSERT INTO write_attempts (
                    attempt_id, action_id, completion_group, phase, status,
                    process_instance_id, lease_expires_at, request_fingerprint,
                    started_at
                ) VALUES (?, ?, ?, 'radarr_release_push', 'attempting', ?, ?, ?, ?)
                """,
                (
                    phase2_attempt_id,
                    action_id,
                    completion_group,
                    process_instance_id,
                    phase2_lease_expires_at,
                    clean_fp,
                    now_iso,
                ),
            )
            conn.execute("COMMIT;")
            return phase2_attempt_id
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def complete_alt_preflight_and_begin_qbit_add(
        self,
        *,
        preflight_attempt_id: str,
        action_id: str,
        completion_group: str,
        radarr_movie_id: int,
        preflight_outcome: str,
        process_instance_id: str,
        qbit_lease_expires_at: str,
        qbit_request_fingerprint: str,
    ) -> Optional[str]:
        """
        Atomically close qbit_alt_preflight (succeeded) and open qbit_alternative_grab (attempting)
        in one BEGIN IMMEDIATE transaction, guaranteeing zero dangling lock gap and zero duplicate adds.
        """
        now_iso = utc_now_iso()
        add_attempt_id = generate_uuid7()
        clean_outcome = redact_secrets(preflight_outcome, max_chars=300)
        clean_fp = redact_secrets(qbit_request_fingerprint, max_chars=800)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            p1_row = conn.execute(
                """
                SELECT status FROM write_attempts
                WHERE attempt_id = ? AND action_id = ? AND phase = 'qbit_alt_preflight'
                """,
                (preflight_attempt_id, action_id),
            ).fetchone()
            if p1_row is None or str(p1_row["status"]) != "attempting":
                conn.execute("COMMIT;")
                return None

            act_row = conn.execute(
                "SELECT * FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if act_row is None or str(act_row["status"]) != "attempting":
                conn.execute("COMMIT;")
                return None

            disqual = self._check_pre_push_db_authorization_locked(conn, act_row, now_iso=now_iso)
            if disqual is not None:
                conn.execute(
                    """
                    UPDATE write_attempts
                    SET status = 'failed_definitive',
                        outcome_summary = ?,
                        finished_at = ?
                    WHERE attempt_id = ?
                    """,
                    (redact_secrets(f"blocked_before_qbit_add:{disqual}", max_chars=300), now_iso, preflight_attempt_id),
                )
                conn.execute(
                    """
                    UPDATE actions
                    SET status = 'superseded',
                        radarr_movie_id = ?,
                        last_error_redacted = ?,
                        updated_at = ?
                    WHERE action_id = ? AND status = 'attempting'
                    """,
                    (radarr_movie_id, redact_secrets(f"blocked_before_qbit_add:{disqual}", max_chars=300), now_iso, action_id),
                )
                conn.execute("COMMIT;")
                return None

            prior_qbit = conn.execute(
                """
                SELECT attempt_id FROM write_attempts
                WHERE completion_group = ?
                  AND phase = 'qbit_alternative_grab'
                  AND status IN ('attempting', 'ambiguous', 'succeeded')
                LIMIT 1
                """,
                (completion_group,),
            ).fetchone()
            if prior_qbit is not None:
                conn.execute("COMMIT;")
                return None

            conn.execute(
                """
                UPDATE write_attempts
                SET status = 'succeeded',
                    outcome_summary = ?,
                    finished_at = ?
                WHERE attempt_id = ?
                """,
                (clean_outcome, now_iso, preflight_attempt_id),
            )
            conn.execute(
                """
                UPDATE actions
                SET status = 'attempting',
                    radarr_movie_id = ?,
                    last_error_redacted = NULL,
                    updated_at = ?
                WHERE action_id = ?
                """,
                (radarr_movie_id, now_iso, action_id),
            )
            conn.execute(
                """
                INSERT INTO write_attempts (
                    attempt_id, action_id, completion_group, phase, status,
                    process_instance_id, lease_expires_at, request_fingerprint,
                    started_at
                ) VALUES (?, ?, ?, 'qbit_alternative_grab', 'attempting', ?, ?, ?, ?)
                """,
                (
                    add_attempt_id,
                    action_id,
                    completion_group,
                    process_instance_id,
                    qbit_lease_expires_at,
                    clean_fp,
                    now_iso,
                ),
            )
            conn.execute("COMMIT;")
            return add_attempt_id
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def reset_interrupted_alt_preflight(
        self,
        *,
        attempt_id: str,
        action_id: str,
        completion_group: str,
    ) -> bool:
        """
        Atomically reset an orphaned/ambiguous `qbit_alt_preflight` attempt back to `active`
        ONLY when zero `qbit_alternative_grab` attempts exist in `completion_group` and the
        action is still `ambiguous_radarr`.
        """
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            act_row = conn.execute(
                "SELECT status FROM actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if act_row is None or str(act_row["status"]) != "ambiguous_radarr":
                conn.execute("COMMIT;")
                return False
            prior_qbit = conn.execute(
                """
                SELECT attempt_id FROM write_attempts
                WHERE completion_group = ?
                  AND phase = 'qbit_alternative_grab'
                  AND status IN ('attempting', 'ambiguous', 'succeeded')
                LIMIT 1
                """,
                (completion_group,),
            ).fetchone()
            if prior_qbit is not None:
                conn.execute("COMMIT;")
                return False
            cur_att = conn.execute(
                """
                UPDATE write_attempts
                SET status = 'failed_definitive',
                    outcome_summary = 'preflight_interrupted_reset',
                    finished_at = COALESCE(finished_at, ?)
                WHERE attempt_id = ?
                  AND action_id = ?
                  AND phase = 'qbit_alt_preflight'
                  AND status = 'ambiguous'
                """,
                (now_iso, attempt_id, action_id),
            )
            if cur_att.rowcount != 1:
                conn.execute("COMMIT;")
                return False
            cur_act = conn.execute(
                """
                UPDATE actions
                SET status = 'active',
                    last_reconciled_at = ?,
                    last_error_redacted = 'Prior read-only preflight was interrupted before submission.',
                    updated_at = ?
                WHERE action_id = ?
                  AND status = 'ambiguous_radarr'
                """,
                (now_iso, now_iso, action_id),
            )
            conn.execute("COMMIT;")
            return cur_act.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def mark_action_expired(
        self,
        *,
        action_id: str,
        detail: str = "expired",
    ) -> bool:
        """Mark action expired ONLY if still active/ambiguous_radarr and no release push or qBit add may have landed."""
        now_iso = utc_now_iso()
        clean = redact_secrets(detail, max_chars=300)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute(
                """
                UPDATE actions
                SET status = 'expired',
                    last_reconciled_at = ?,
                    last_error_redacted = ?,
                    updated_at = ?
                WHERE action_id = ?
                  AND status IN ('active', 'ambiguous_radarr')
                  AND NOT EXISTS (
                      SELECT 1 FROM write_attempts wa
                      WHERE wa.completion_group = actions.completion_group
                        AND wa.phase IN ('radarr_release_push', 'qbit_alternative_grab')
                        AND wa.status IN ('attempting', 'ambiguous', 'succeeded')
                  )
                """,
                (now_iso, clean, now_iso, action_id),
            )
            conn.execute("COMMIT;")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()
