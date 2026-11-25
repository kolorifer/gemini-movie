from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import timedelta
from typing import Any, Optional

from .classifier import ClassifiedWebhook, classify_rejection_reasons
from .clients import (
    ActionsApiClient,
    DeliveryCardResponse,
    DeliveryEvalResponse,
    RadarrStatusResponse,
    TriageApiClient,
)
from .config import AlternativeGrabPolicy, IntakeConfig
from .db import IntakeDB, utc_now, utc_now_iso
from .security import redact_secrets
from .tmdb_verifier import IdentityGateResult, TmdbIndependentClient, TmdbMovieRecord, run_independent_identity_gate
from .torrentleech import (
    TL_TRANSIENT_FAILURE_OUTCOMES,
    TorrentLeechResult,
    TorrentLeechScraper,
)

logger = logging.getLogger(__name__)


def build_triage_evaluate_request(
    *,
    classified: ClassifiedWebhook,
    tl_result: TorrentLeechResult,
    force_refresh: bool,
    extra_conflict_note: Optional[str] = None,
) -> tuple[dict[str, Any], str]:
    """
    Build the POST /evaluate payload for movie-triage-api strictly following handover rules:
      - Send observed IDs, indexer/category, quality, actual MediaInfo file runtime/audio,
        decimal size_gb, explicit freeleech (including False), sanitized description/NFO.
      - Omit unknown fields.
      - Never include download_url, passkeys, or cookies.
    Returns (request_body, full_evidence_digest).
    """
    tinfo: dict[str, Any] = {}
    if classified.indexer_raw:
        tinfo["indexer"] = classified.indexer_raw
    if classified.category:
        tinfo["category"] = classified.category

    # Prefer verified TL listing IMDb over autobrr meta_imdb when available, else autobrr hint
    observed_imdb = tl_result.imdb_id or classified.meta_imdb
    if observed_imdb:
        tinfo["imdb_id"] = observed_imdb
    if classified.meta_tmdb is not None:
        tinfo["tmdb_id"] = classified.meta_tmdb

    # Strictly actual MediaInfo file runtime & audio languages (never catalogue runtime or subtitles)
    if tl_result.file_runtime_minutes is not None:
        tinfo["runtime_minutes"] = tl_result.file_runtime_minutes
    if tl_result.audio_languages:
        tinfo["audio_languages"] = list(tl_result.audio_languages)

    if classified.resolution:
        tinfo["resolution"] = classified.resolution
    if classified.source:
        tinfo["source"] = classified.source
    if classified.dynamic_range:
        tinfo["dynamic_range"] = list(classified.dynamic_range)
    if classified.media_processing:
        tinfo["media_processing"] = classified.media_processing
    if classified.group:
        tinfo["group"] = classified.group
    if classified.size_gb is not None:
        tinfo["size_gb"] = classified.size_gb

    # Preserve explicit False as well as True; omit only when unknown
    effective_freeleech: Optional[bool] = classified.freeleech
    if effective_freeleech is None and tl_result.freeleech_badge is not None:
        effective_freeleech = tl_result.freeleech_badge
    if effective_freeleech is not None:
        tinfo["freeleech"] = bool(effective_freeleech)

    if tl_result.sanitized_description:
        tinfo["description"] = tl_result.sanitized_description
    if tl_result.sanitized_nfo:
        tinfo["nfo_text"] = tl_result.sanitized_nfo

    body: dict[str, Any] = {
        "raw_text": classified.exact_release_name,
        "force_refresh": bool(force_refresh),
    }
    if classified.hint_title:
        body["hint_title"] = classified.hint_title
    if classified.hint_year is not None:
        body["hint_year"] = classified.hint_year
    if extra_conflict_note:
        body["extra_context"] = redact_secrets(extra_conflict_note, max_chars=400)
    if tinfo:
        body["torrent_info"] = tinfo

    digest_input = {
        "raw_text": body["raw_text"],
        "hint_title": body.get("hint_title"),
        "hint_year": body.get("hint_year"),
        "extra_context": body.get("extra_context"),
        "torrent_info": tinfo,
        "tl_outcome": tl_result.outcome,
    }
    full_digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return body, full_digest


class IntakeWorker:
    """
    Restart-safe durable worker loop driving queued releases through:
      rejection check -> TL scrape -> independent identity gate ->
      Radarr pre-delivery suppression -> prompt card delivery + bound evaluation reply.
    """

    def __init__(
        self,
        *,
        config: IntakeConfig,
        db: IntakeDB,
        tl_scraper: TorrentLeechScraper,
        tmdb_client: TmdbIndependentClient,
        triage_client: TriageApiClient,
        actions_client: ActionsApiClient,
        alt_grab_policy: Optional[AlternativeGrabPolicy] = None,
        worker_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.tl_scraper = tl_scraper
        self.tmdb_client = tmdb_client
        self.triage_client = triage_client
        self.actions_client = actions_client
        self.alt_grab_policy = alt_grab_policy or AlternativeGrabPolicy()
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"

        self._wakeup = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    def notify(self) -> None:
        self._wakeup.set()

    async def start(self) -> None:
        await asyncio.to_thread(
            self.db.recover_orphaned_leases,
            active_worker_id=self.worker_id,
            recover_dead_owners=True,
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"intake-worker-{self.worker_id[:8]}")

    async def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(
                    self.db.recover_orphaned_leases,
                    active_worker_id=self.worker_id,
                    recover_dead_owners=False,
                )
                processed = await self.process_next_queued()
                if processed:
                    continue
            except Exception as exc:
                logger.exception("Intake worker loop error: %s", redact_secrets(str(exc), max_chars=240))

            self._wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(),
                    timeout=self.config.worker_poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def process_next_queued(self) -> bool:
        row = await asyncio.to_thread(
            self.db.claim_next_queued_release,
            worker_id=self.worker_id,
            lease_seconds=self.config.worker_lease_seconds,
        )
        if row is None:
            return False
        await self._process_claimed_release(row)
        return True

    def _decide_radarr_outcome(
        self,
        *,
        radarr: RadarrStatusResponse,
        alt_grab_eligible: bool,
        alt_grab_rejection_class: Optional[str],
    ) -> tuple[str, Optional[str], Optional[str]]:
        """
        Apply Pre-Delivery Suppression Matrix (§5.3).
        Returns (mode, suppression_reason, radarr_note) where mode is one of:
          suppress | actionable_grab | alternative_grab | evidence_review
        """
        if radarr.status == "watched_unmonitored":
            return "suppress", "watched_unmonitored", None
        if radarr.status == "owned_file":
            if (
                alt_grab_eligible
                and alt_grab_rejection_class
                and radarr.movie_id is not None
                and radarr.file_id is not None
            ):
                return "alternative_grab", None, f"Alternative Grab ({alt_grab_rejection_class})"
            return "suppress", "already_owned", None
        if radarr.status == "monitored_no_file":
            return "actionable_grab", None, "Monitored in Radarr (no file yet)"
        if radarr.status == "absent":
            return "actionable_grab", None, None
        # lookup_failed or ambiguous: NEVER establish absence
        return (
            "evidence_review",
            None,
            f"Radarr status unverified ({radarr.status})",
        )

    def _build_card_payload(
        self,
        *,
        release_id: str,
        card_revision: int,
        source_occurrence_id: str,
        delivery_kind: str,
        classified: ClassifiedWebhook,
        tl_result: TorrentLeechResult,
        gate: IdentityGateResult,
        radarr: Optional[RadarrStatusResponse],
        radarr_note: Optional[str],
        alt_rejection_class: Optional[str],
    ) -> dict[str, Any]:
        movie = gate.verified_movie
        # Defense in depth: only supply verified IDs for actionable/alternative cards when gate passed
        payload: dict[str, Any] = {
            "release_id": release_id,
            "card_revision": card_revision,
            "source_occurrence_id": source_occurrence_id,
            "delivery_kind": delivery_kind,
            "identity_gate_status": gate.status,
            "exact_release_name": classified.exact_release_name,
            "indexer": classified.indexer_raw or classified.indexer_norm,
            "category": classified.category,
            "size_gb": classified.size_gb,
            "freeleech": classified.freeleech if classified.freeleech is not None else tl_result.freeleech_badge,
            "tl_outcome": tl_result.outcome,
            "radarr_status": radarr.status if radarr else "unchecked",
            "radarr_note": radarr_note,
            "conflict_reason": gate.conflict_reason,
            "observed_meta_imdb": classified.meta_imdb,
            "observed_meta_tmdb": classified.meta_tmdb,
            "observed_tl_imdb": tl_result.imdb_id,
            "file_runtime_minutes": tl_result.file_runtime_minutes,
            "audio_languages": list(tl_result.audio_languages),
        }
        if gate.status == "verified" and movie is not None:
            payload["verified_tmdb_id"] = movie.tmdb_id
            payload["verified_imdb_id"] = movie.imdb_id
            payload["verified_title"] = movie.title
            payload["verified_year"] = movie.year
            payload["poster_path"] = movie.poster_path
        if delivery_kind == "alternative_grab" and radarr is not None:
            payload["bound_existing_movie_id"] = radarr.movie_id
            payload["bound_existing_file_id"] = radarr.file_id
            payload["bound_rejection_class"] = alt_rejection_class
        return payload

    async def _deliver_card_and_eval_reply(
        self,
        *,
        release_id: str,
        card_revision: int,
        incoming_digest: str,
        card_payload: dict[str, Any],
        eval_payload: Optional[dict[str, Any]],
        eval_error: Optional[str],
        ack_metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[DeliveryCardResponse, Optional[DeliveryEvalResponse], Optional[dict[str, Any]]]:
        await asyncio.to_thread(
            self.db.renew_lease,
            release_id,
            worker_id=self.worker_id,
            lease_seconds=self.config.worker_lease_seconds,
            new_state="delivering",
        )
        card_resp = await self.actions_client.deliver_card(card_payload)
        if card_resp.status != "delivered" or card_resp.ack_message_id is None:
            # Never post an unanchored reply when ack_message_id is missing / ambiguous_delivery
            return card_resp, None, None

        reply_body: dict[str, Any] = {
            "release_id": release_id,
            "card_revision": card_revision,
            "ack_chat_id": card_resp.ack_chat_id,
            "ack_message_id": card_resp.ack_message_id,
            "ack_thread_id": card_resp.ack_thread_id,
            "evaluation": eval_payload,
            "error_redacted": eval_error,
        }
        # Persist acknowledged card receipt + pending reply + snapshot metadata BEFORE posting reply
        # so even a hard process kill mid-reply recovers directly into Path B with intact state.
        await asyncio.to_thread(
            self.db.record_card_ack,
            release_id,
            worker_id=self.worker_id,
            card_revision=card_revision,
            ack_chat_id=card_resp.ack_chat_id,
            ack_message_id=card_resp.ack_message_id,
            ack_thread_id=card_resp.ack_thread_id,
            ack_incoming_digest=incoming_digest,
            pending_eval_reply=reply_body,
            **(ack_metadata or {}),
        )
        reply_resp = await self.actions_client.deliver_evaluation(reply_body)
        return card_resp, reply_resp, reply_body

    def _retry_next_attempt_iso(self) -> str:
        return utc_now_iso(utc_now() + timedelta(seconds=self.config.retry_backoff_seconds))

    async def _process_pending_followups(self, row: dict[str, Any]) -> bool:
        """
        Handle durably persisted post-card follow-ups independently WITHOUT resending
        the original card, re-scraping TorrentLeech, or repeating /evaluate:
          - Path A: retry failed conflict-card supersession (evidence_review edit)
          - Path B: retry failed evaluation / redacted-error reply bound to ack_message_id
        Returns True if this row was handled by a pending follow-up path.
        """
        raw_sup = row.get("pending_card_supersede_json")
        raw_reply = row.get("pending_eval_reply_json")
        if not raw_sup and not raw_reply:
            return False

        release_id = str(row["release_id"])
        expected_digest = str(row["incoming_evidence_digest"])
        retry_count = int(row.get("retry_count") or 0)
        pending_reply: Optional[dict[str, Any]] = json.loads(str(raw_reply)) if raw_reply else None

        await asyncio.to_thread(
            self.db.renew_lease,
            release_id,
            worker_id=self.worker_id,
            lease_seconds=self.config.worker_lease_seconds,
            new_state="delivering",
        )

        # Path A: Retry conflict-card supersession before allowing completion
        if raw_sup:
            supersede_card = json.loads(str(raw_sup))
            effective_rev = int(supersede_card.get("card_revision") or (int(row.get("card_revision") or 0) + 1))
            conflict_reason = supersede_card.get("conflict_reason")
            sup_resp = await self.actions_client.deliver_card(supersede_card)
            if sup_resp.status == "ambiguous_delivery":
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="ambiguous_delivery",
                    expected_incoming_digest=expected_digest,
                    identity_gate_status="evidence_review",
                    identity_conflict_reason=conflict_reason,
                    verified_tmdb_id=None,
                    verified_imdb_id=None,
                    verified_title=None,
                    verified_year=None,
                    pending_card_supersede=None,
                    pending_eval_reply=None,
                    allow_downgrade_on_conflict=True,
                )
                return True
            if sup_resp.status != "delivered":
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="queued" if retry_count < 4 else "failed_terminal",
                    expected_incoming_digest=expected_digest,
                    identity_gate_status="evidence_review",
                    identity_conflict_reason=conflict_reason,
                    verified_tmdb_id=None,
                    verified_imdb_id=None,
                    verified_title=None,
                    verified_year=None,
                    pending_card_supersede=supersede_card,
                    pending_eval_reply=pending_reply,
                    next_attempt_at=self._retry_next_attempt_iso(),
                    increment_retry=True,
                    allow_downgrade_on_conflict=True,
                )
                return True

            # Conflict supersession succeeded! Now deliver pending bound evaluation reply if any.
            if pending_reply is not None:
                pending_reply["card_revision"] = effective_rev
                reply_resp = await self.actions_client.deliver_evaluation(pending_reply)
                if reply_resp.status == "failed":
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="queued",
                        expected_incoming_digest=expected_digest,
                        card_revision=effective_rev,
                        identity_gate_status="evidence_review",
                        identity_conflict_reason=conflict_reason,
                        verified_tmdb_id=None,
                        verified_imdb_id=None,
                        verified_title=None,
                        verified_year=None,
                        eval_reply_status="pending",
                        pending_card_supersede=None,
                        pending_eval_reply=pending_reply,
                        next_attempt_at=self._retry_next_attempt_iso(),
                        reset_retry=True,
                        increment_retry=True,
                        allow_downgrade_on_conflict=True,
                    )
                    return True
                reply_status = "sent" if reply_resp.status == "sent" else "ambiguous_reply"
            else:
                reply_status = str(row.get("eval_reply_status") or "sent")

            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="completed",
                expected_incoming_digest=expected_digest,
                card_revision=effective_rev,
                identity_gate_status="evidence_review",
                identity_conflict_reason=conflict_reason,
                verified_tmdb_id=None,
                verified_imdb_id=None,
                verified_title=None,
                verified_year=None,
                eval_reply_status=reply_status,
                pending_card_supersede=None,
                pending_eval_reply=None,
                allow_downgrade_on_conflict=True,
            )
            return True

        # Path B: Retry pending bound evaluation reply ONLY (never resend card or re-evaluate)
        assert pending_reply is not None
        reply_resp = await self.actions_client.deliver_evaluation(pending_reply)
        if reply_resp.status == "failed":
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="queued" if retry_count < 4 else "failed_terminal",
                expected_incoming_digest=expected_digest,
                eval_reply_status="pending",
                pending_eval_reply=pending_reply,
                next_attempt_at=self._retry_next_attempt_iso(),
                increment_retry=True,
            )
            return True

        reply_status = "sent" if reply_resp.status == "sent" else "ambiguous_reply"
        await asyncio.to_thread(
            self.db.finalize_release,
            release_id,
            worker_id=self.worker_id,
            state="completed",
            expected_incoming_digest=expected_digest,
            eval_reply_status=reply_status,
            pending_eval_reply=None,
        )
        return True

    async def _process_claimed_release(self, row: dict[str, Any]) -> None:
        # First drain any persisted post-card follow-ups (conflict supersession or eval reply)
        if await self._process_pending_followups(row):
            return

        release_id = str(row["release_id"])
        source_occurrence_id = str(row["source_occurrence_id"])
        expected_digest = str(row["incoming_evidence_digest"])
        prev_card_revision = int(row["card_revision"] or 0)
        prev_last_eval_digest = row.get("last_evaluated_digest")
        prev_verified = str(row.get("identity_gate_status") or "") == "verified"
        retry_count = int(row.get("retry_count") or 0)

        snap = json.loads(str(row["incoming_snapshot_json"]))
        classified = ClassifiedWebhook.from_sanitized_snapshot(snap, expected_digest)

        # Step 1: Routine quality rejection early check (§5.1 Step 3)
        rej_mode, rej_class = classify_rejection_reasons(
            classified.action_rejected,
            classified.rejections,
            self.alt_grab_policy,
        )
        if rej_mode == "quality_rejected_suppress":
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="suppressed",
                expected_incoming_digest=expected_digest,
                suppression_reason="quality_rejected",
                eval_reply_status="skipped",
            )
            return

        alt_grab_eligible = rej_mode == "alternative_grab_candidate"

        # Step 2: TorrentLeech listing enrichment (serialized single worker)
        # Keep TL verification mandatory whenever TL metadata is enabled for a TorrentLeech
        # webhook — a missing or malformed external_id MUST fail verification rather than
        # disabling the source-integrity check.
        tl_applicable = (
            self.config.tl_metadata_enabled
            and classified.indexer_norm == "torrentleech"
        )
        tl_result = await self.tl_scraper.fetch_listing(
            indexer_norm=classified.indexer_norm,
            external_id=classified.external_id_norm,
            exact_release_name=classified.exact_release_name,
        )

        # Step 3: Check whether pre-evaluation source IDs independently pass all 6 gate checks
        pre_imdb = tl_result.imdb_id or classified.meta_imdb
        pre_tmdb = classified.meta_tmdb
        pre_gate = await run_independent_identity_gate(
            tmdb_client=self.tmdb_client,
            exact_release_name=classified.exact_release_name,
            proposed_imdb_id=pre_imdb,
            proposed_tmdb_id=pre_tmdb,
            proposal_confidence=None,
            from_evaluator=False,
            tl_applicable=tl_applicable,
            tl_outcome=tl_result.outcome,
            observed_meta_imdb=classified.meta_imdb,
            observed_meta_tmdb=classified.meta_tmdb,
            observed_tl_imdb=tl_result.imdb_id,
            file_runtime_minutes=tl_result.file_runtime_minutes,
        )

        prev_verified_conflict_reasons: list[str] = []
        if prev_verified:
            prev_imdb = str(row.get("verified_imdb_id") or "").strip().lower() or None
            prev_tmdb = row.get("verified_tmdb_id")
            if prev_imdb and classified.meta_imdb and classified.meta_imdb.lower() != prev_imdb:
                prev_verified_conflict_reasons.append(
                    f"correction meta_imdb {classified.meta_imdb.lower()} != previously verified {prev_imdb}"
                )
            if (
                isinstance(prev_tmdb, int)
                and not isinstance(prev_tmdb, bool)
                and classified.meta_tmdb is not None
                and classified.meta_tmdb != prev_tmdb
            ):
                prev_verified_conflict_reasons.append(
                    f"correction meta_tmdb {classified.meta_tmdb} != previously verified {prev_tmdb}"
                )
            if prev_imdb and tl_result.imdb_id and tl_result.imdb_id.lower() != prev_imdb:
                prev_verified_conflict_reasons.append(
                    f"TL listing IMDb {tl_result.imdb_id.lower()} != previously verified {prev_imdb}"
                )
        has_prev_verified_conflict = bool(prev_verified_conflict_reasons)

        next_card_rev = prev_card_revision + 1

        # Guard: if already verified previously and this correction pass had only a
        # transient TL lookup failure (never a genuine tracker conflict or metadata
        # contradiction against the verified identity), preserve the verified release.
        if (
            prev_verified
            and pre_gate.status != "verified"
            and not pre_gate.is_genuine_conflict
            and not has_prev_verified_conflict
            and tl_result.outcome in TL_TRANSIENT_FAILURE_OUTCOMES
        ):
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="completed",
                expected_incoming_digest=expected_digest,
                tl_outcome=tl_result.outcome,
            )
            return

        # =====================================================================
        # FAST PATH: Pre-evaluation identity independently verified (< 2s)
        # =====================================================================
        if pre_gate.status == "verified" and pre_gate.verified_movie is not None:
            movie = pre_gate.verified_movie
            radarr = await self.actions_client.get_radarr_status(
                tmdb_id=movie.tmdb_id,
                imdb_id=movie.imdb_id,
                release_title=movie.title,
            )
            delivery_mode, suppression_reason, radarr_note = self._decide_radarr_outcome(
                radarr=radarr,
                alt_grab_eligible=alt_grab_eligible,
                alt_grab_rejection_class=rej_class,
            )
            if delivery_mode == "suppress":
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="suppressed",
                    expected_incoming_digest=expected_digest,
                    suppression_reason=suppression_reason,
                    tl_outcome=tl_result.outcome,
                    identity_gate_status="verified",
                    verified_tmdb_id=movie.tmdb_id,
                    verified_imdb_id=movie.imdb_id,
                    verified_title=movie.title,
                    verified_year=movie.year,
                    eval_reply_status="skipped",
                    radarr_status_snapshot=radarr.to_dict(),
                )
                return

            # If the worker crashed after persisting card ack for this exact incoming digest
            # and before completing /evaluate, reuse the persisted receipt instead of
            # re-sending or bumping card_revision unnecessarily.
            resuming_mid_eval = (
                prev_card_revision >= 1
                and row.get("ack_message_id") is not None
                and str(row.get("ack_incoming_digest") or "") == expected_digest
                and str(row.get("eval_reply_status") or "") == "pending"
            )
            if resuming_mid_eval:
                next_card_rev = prev_card_revision
                card_resp = DeliveryCardResponse(
                    status="delivered",
                    ack_chat_id=row.get("ack_chat_id"),
                    ack_message_id=int(row["ack_message_id"]),
                    ack_thread_id=row.get("ack_thread_id"),
                )
            else:
                # Deliver prompt card immediately BEFORE waiting on Gemini evaluation
                card_payload = self._build_card_payload(
                    release_id=release_id,
                    card_revision=next_card_rev,
                    source_occurrence_id=source_occurrence_id,
                    delivery_kind=delivery_mode,
                    classified=classified,
                    tl_result=tl_result,
                    gate=pre_gate,
                    radarr=radarr,
                    radarr_note=radarr_note,
                    alt_rejection_class=rej_class,
                )
                await asyncio.to_thread(
                    self.db.renew_lease,
                    release_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.config.worker_lease_seconds,
                    new_state="delivering",
                )
                card_resp = await self.actions_client.deliver_card(card_payload)
                if card_resp.status == "ambiguous_delivery":
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="ambiguous_delivery",
                        expected_incoming_digest=expected_digest,
                        tl_outcome=tl_result.outcome,
                        identity_gate_status="verified",
                        verified_tmdb_id=movie.tmdb_id,
                        verified_imdb_id=movie.imdb_id,
                        verified_title=movie.title,
                        verified_year=movie.year,
                        radarr_status_snapshot=radarr.to_dict(),
                    )
                    return
                if card_resp.status != "delivered" or card_resp.ack_message_id is None:
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="queued" if retry_count < 4 else "failed_terminal",
                        expected_incoming_digest=expected_digest,
                        tl_outcome=tl_result.outcome,
                        identity_gate_status="verified",
                        verified_tmdb_id=movie.tmdb_id,
                        verified_imdb_id=movie.imdb_id,
                        verified_title=movie.title,
                        verified_year=movie.year,
                        radarr_status_snapshot=radarr.to_dict(),
                        next_attempt_at=self._retry_next_attempt_iso(),
                        increment_retry=True,
                    )
                    return

                # Persist card ack receipt immediately so crash during /evaluate never loses receipt
                await asyncio.to_thread(
                    self.db.record_card_ack,
                    release_id,
                    worker_id=self.worker_id,
                    card_revision=next_card_rev,
                    ack_chat_id=card_resp.ack_chat_id,
                    ack_message_id=card_resp.ack_message_id,
                    ack_thread_id=card_resp.ack_thread_id,
                    ack_incoming_digest=expected_digest,
                )

            # Now call movie-triage-api /evaluate and post bound reply
            await asyncio.to_thread(
                self.db.renew_lease,
                release_id,
                worker_id=self.worker_id,
                lease_seconds=self.config.worker_lease_seconds,
                new_state="evaluating",
            )
            eval_body_probe, full_digest = build_triage_evaluate_request(
                classified=classified,
                tl_result=tl_result,
                force_refresh=False,
            )
            must_force = (
                prev_last_eval_digest is not None and full_digest != prev_last_eval_digest
            ) or (prev_card_revision > 0 and not resuming_mid_eval)
            if must_force:
                eval_body_probe["force_refresh"] = True

            eval_res = await self.triage_client.evaluate(eval_body_probe)

            # If evaluator contradicts our pre-evaluation verified movie with a different
            # canonical IMDb/TMDB ID, supersede the bound card to evidence_review!
            eval_conflict_reason: Optional[str] = None
            if eval_res.ok and isinstance(eval_res.payload, dict):
                ident = eval_res.payload.get("identification")
                if isinstance(ident, dict):
                    ev_imdb = str(ident.get("imdb_id") or "").strip().lower() or None
                    ev_tmdb = ident.get("tmdb_id")
                    if ev_imdb and movie.imdb_id and ev_imdb != movie.imdb_id:
                        eval_conflict_reason = (
                            f"Evaluator proposed {ev_imdb}, contradicting pre-verified {movie.imdb_id}"
                        )
                    elif (
                        isinstance(ev_tmdb, int)
                        and not isinstance(ev_tmdb, bool)
                        and ev_tmdb != movie.tmdb_id
                    ):
                        eval_conflict_reason = (
                            f"Evaluator proposed TMDB {ev_tmdb}, contradicting pre-verified TMDB {movie.tmdb_id}"
                        )

            effective_rev = next_card_rev
            effective_gate_status = "verified"
            effective_conflict: Optional[str] = None
            if eval_conflict_reason is not None:
                supersede_rev = next_card_rev + 1
                conflict_gate = IdentityGateResult(
                    status="evidence_review",
                    verified_movie=None,
                    conflict_reason=eval_conflict_reason,
                    is_genuine_conflict=True,
                )
                supersede_card = self._build_card_payload(
                    release_id=release_id,
                    card_revision=supersede_rev,
                    source_occurrence_id=source_occurrence_id,
                    delivery_kind="evidence_review",
                    classified=classified,
                    tl_result=tl_result,
                    gate=conflict_gate,
                    radarr=radarr,
                    radarr_note="Superseded to evidence review due to evaluator identity conflict",
                    alt_rejection_class=None,
                )
                pending_reply_for_sup: dict[str, Any] = {
                    "release_id": release_id,
                    "card_revision": supersede_rev,
                    "ack_chat_id": card_resp.ack_chat_id,
                    "ack_message_id": card_resp.ack_message_id,
                    "ack_thread_id": card_resp.ack_thread_id,
                    "evaluation": eval_res.payload if eval_res.ok else None,
                    "error_redacted": None if eval_res.ok else eval_res.error_redacted,
                }
                sup_resp = await self.actions_client.deliver_card(supersede_card)
                if sup_resp.status == "ambiguous_delivery":
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="ambiguous_delivery",
                        expected_incoming_digest=expected_digest,
                        card_revision=next_card_rev,
                        full_evidence_digest=full_digest,
                        last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                        tl_outcome=tl_result.outcome,
                        identity_gate_status="evidence_review",
                        identity_conflict_reason=eval_conflict_reason,
                        verified_tmdb_id=None,
                        verified_imdb_id=None,
                        verified_title=None,
                        verified_year=None,
                        ack_chat_id=card_resp.ack_chat_id,
                        ack_message_id=card_resp.ack_message_id,
                        ack_thread_id=card_resp.ack_thread_id,
                        pending_card_supersede=None,
                        pending_eval_reply=None,
                        radarr_status_snapshot=radarr.to_dict(),
                        evaluation_json=eval_res.payload if eval_res.ok else None,
                        allow_downgrade_on_conflict=True,
                    )
                    return
                if sup_resp.status != "delivered":
                    # Supersession failed definitively: DO NOT mark completed or advance revision;
                    # record genuine conflict and persist pending supersession + reply for independent retry.
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="queued",
                        expected_incoming_digest=expected_digest,
                        card_revision=next_card_rev,
                        full_evidence_digest=full_digest,
                        last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                        tl_outcome=tl_result.outcome,
                        identity_gate_status="evidence_review",
                        identity_conflict_reason=eval_conflict_reason,
                        verified_tmdb_id=None,
                        verified_imdb_id=None,
                        verified_title=None,
                        verified_year=None,
                        ack_chat_id=card_resp.ack_chat_id,
                        ack_message_id=card_resp.ack_message_id,
                        ack_thread_id=card_resp.ack_thread_id,
                        eval_reply_status="pending",
                        pending_card_supersede=supersede_card,
                        pending_eval_reply=pending_reply_for_sup,
                        radarr_status_snapshot=radarr.to_dict(),
                        evaluation_json=eval_res.payload if eval_res.ok else None,
                        next_attempt_at=self._retry_next_attempt_iso(),
                        reset_retry=True,
                        increment_retry=True,
                        allow_downgrade_on_conflict=True,
                    )
                    return

                effective_rev = supersede_rev
                effective_gate_status = "evidence_review"
                effective_conflict = eval_conflict_reason

            reply_body: dict[str, Any] = {
                "release_id": release_id,
                "card_revision": effective_rev,
                "ack_chat_id": card_resp.ack_chat_id,
                "ack_message_id": card_resp.ack_message_id,
                "ack_thread_id": card_resp.ack_thread_id,
                "evaluation": eval_res.payload if eval_res.ok else None,
                "error_redacted": None if eval_res.ok else eval_res.error_redacted,
            }
            reply_resp = await self.actions_client.deliver_evaluation(reply_body)
            if reply_resp.status == "failed":
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="queued",
                    expected_incoming_digest=expected_digest,
                    card_revision=effective_rev,
                    full_evidence_digest=full_digest,
                    last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                    tl_outcome=tl_result.outcome,
                    identity_gate_status=effective_gate_status,
                    identity_conflict_reason=effective_conflict,
                    verified_tmdb_id=movie.tmdb_id if effective_gate_status == "verified" else None,
                    verified_imdb_id=movie.imdb_id if effective_gate_status == "verified" else None,
                    verified_title=movie.title if effective_gate_status == "verified" else None,
                    verified_year=movie.year if effective_gate_status == "verified" else None,
                    ack_chat_id=card_resp.ack_chat_id,
                    ack_message_id=card_resp.ack_message_id,
                    ack_thread_id=card_resp.ack_thread_id,
                    eval_reply_status="pending",
                    pending_card_supersede=None,
                    pending_eval_reply=reply_body,
                    radarr_status_snapshot=radarr.to_dict(),
                    evaluation_json=eval_res.payload if eval_res.ok else None,
                    next_attempt_at=self._retry_next_attempt_iso(),
                    reset_retry=True,
                    increment_retry=True,
                    allow_downgrade_on_conflict=effective_conflict is not None,
                )
                return

            reply_status = "sent" if reply_resp.status == "sent" else "ambiguous_reply"
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="completed",
                expected_incoming_digest=expected_digest,
                card_revision=effective_rev,
                full_evidence_digest=full_digest,
                last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                tl_outcome=tl_result.outcome,
                identity_gate_status=effective_gate_status,
                identity_conflict_reason=effective_conflict,
                verified_tmdb_id=movie.tmdb_id if effective_gate_status == "verified" else None,
                verified_imdb_id=movie.imdb_id if effective_gate_status == "verified" else None,
                verified_title=movie.title if effective_gate_status == "verified" else None,
                verified_year=movie.year if effective_gate_status == "verified" else None,
                ack_chat_id=card_resp.ack_chat_id,
                ack_message_id=card_resp.ack_message_id,
                ack_thread_id=card_resp.ack_thread_id,
                eval_reply_status=reply_status,
                pending_card_supersede=None,
                pending_eval_reply=None,
                radarr_status_snapshot=radarr.to_dict(),
                evaluation_json=eval_res.payload if eval_res.ok else None,
                allow_downgrade_on_conflict=effective_conflict is not None,
            )
            return

        # =====================================================================
        # UNRESOLVED / CONFLICT PATH: Evaluate first, then run independent gate
        # =====================================================================
        await asyncio.to_thread(
            self.db.renew_lease,
            release_id,
            worker_id=self.worker_id,
            lease_seconds=self.config.worker_lease_seconds,
            new_state="evaluating",
        )
        conflict_notes: list[str] = []
        if pre_gate.is_genuine_conflict and pre_gate.conflict_reason:
            conflict_notes.append(pre_gate.conflict_reason)
        conflict_notes.extend(prev_verified_conflict_reasons)
        conflict_context = "; ".join(conflict_notes) if conflict_notes else None
        eval_body, full_digest = build_triage_evaluate_request(
            classified=classified,
            tl_result=tl_result,
            force_refresh=False,
            extra_conflict_note=conflict_context,
        )
        if (
            pre_gate.is_genuine_conflict
            or has_prev_verified_conflict
            or prev_card_revision > 0
            or (prev_last_eval_digest is not None and prev_last_eval_digest != full_digest)
            or tl_result.outcome in ("listing_id_conflict", "listing_id_malformed")
        ):
            eval_body["force_refresh"] = True

        eval_res = await self.triage_client.evaluate(eval_body)

        final_gate = pre_gate
        if eval_res.ok and isinstance(eval_res.payload, dict):
            ident = eval_res.payload.get("identification")
            ident_dict = ident if isinstance(ident, dict) else {}
            # Run independent verification gate on evaluator's proposal
            final_gate = await run_independent_identity_gate(
                tmdb_client=self.tmdb_client,
                exact_release_name=classified.exact_release_name,
                proposed_imdb_id=ident_dict.get("imdb_id"),
                proposed_tmdb_id=ident_dict.get("tmdb_id"),
                proposal_confidence=ident_dict.get("match_confidence"),
                from_evaluator=True,
                tl_applicable=tl_applicable,
                tl_outcome=tl_result.outcome,
                observed_meta_imdb=classified.meta_imdb,
                observed_meta_tmdb=classified.meta_tmdb,
                observed_tl_imdb=tl_result.imdb_id,
                file_runtime_minutes=tl_result.file_runtime_minutes,
            )

        effective_genuine_conflict = (
            final_gate.is_genuine_conflict
            or pre_gate.is_genuine_conflict
            or has_prev_verified_conflict
        )
        if effective_genuine_conflict and (
            final_gate.status == "verified" or not final_gate.is_genuine_conflict
        ):
            reason_parts: list[str] = []
            if pre_gate.is_genuine_conflict and pre_gate.conflict_reason:
                reason_parts.append(pre_gate.conflict_reason)
            if final_gate.conflict_reason and final_gate.conflict_reason not in reason_parts:
                reason_parts.append(final_gate.conflict_reason)
            for reason in prev_verified_conflict_reasons:
                if reason not in reason_parts:
                    reason_parts.append(reason)
            merged_failed = tuple(
                dict.fromkeys(
                    (pre_gate.checks_failed if pre_gate.is_genuine_conflict else ())
                    + final_gate.checks_failed
                    + (("prev_verified_metadata_conflict",) if has_prev_verified_conflict else ())
                )
            )
            final_gate = IdentityGateResult(
                status="evidence_review",
                verified_movie=None,
                conflict_reason="; ".join(reason_parts) or "Unresolved identity conflict",
                is_genuine_conflict=True,
                checks_passed=final_gate.checks_passed,
                checks_failed=merged_failed,
            )

        # Never downgrade an already-verified card on a temporary lookup failure (non-conflict)
        if prev_verified and final_gate.status != "verified" and not effective_genuine_conflict:
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="completed",
                expected_incoming_digest=expected_digest,
                full_evidence_digest=full_digest,
                tl_outcome=tl_result.outcome,
            )
            return

        radarr: Optional[RadarrStatusResponse] = None
        delivery_kind = "evidence_review"
        radarr_note: Optional[str] = None

        if final_gate.status == "verified" and final_gate.verified_movie is not None:
            movie = final_gate.verified_movie
            radarr = await self.actions_client.get_radarr_status(
                tmdb_id=movie.tmdb_id,
                imdb_id=movie.imdb_id,
                release_title=movie.title,
            )
            delivery_mode, suppression_reason, radarr_note = self._decide_radarr_outcome(
                radarr=radarr,
                alt_grab_eligible=alt_grab_eligible,
                alt_grab_rejection_class=rej_class,
            )
            if delivery_mode == "suppress":
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="suppressed",
                    expected_incoming_digest=expected_digest,
                    suppression_reason=suppression_reason,
                    full_evidence_digest=full_digest,
                    last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                    tl_outcome=tl_result.outcome,
                    identity_gate_status="verified",
                    verified_tmdb_id=movie.tmdb_id,
                    verified_imdb_id=movie.imdb_id,
                    verified_title=movie.title,
                    verified_year=movie.year,
                    eval_reply_status="skipped",
                    radarr_status_snapshot=radarr.to_dict(),
                    evaluation_json=eval_res.payload if eval_res.ok else None,
                )
                return
            delivery_kind = delivery_mode
        else:
            # Hard invariant: never convert an identity conflict / failed gate into Alternative Grab
            delivery_kind = "evidence_review"

        card_payload = self._build_card_payload(
            release_id=release_id,
            card_revision=next_card_rev,
            source_occurrence_id=source_occurrence_id,
            delivery_kind=delivery_kind,
            classified=classified,
            tl_result=tl_result,
            gate=final_gate,
            radarr=radarr,
            radarr_note=radarr_note,
            alt_rejection_class=rej_class if delivery_kind == "alternative_grab" else None,
        )
        verified_movie: Optional[TmdbMovieRecord] = (
            final_gate.verified_movie if final_gate.status == "verified" else None
        )
        card_resp, reply_resp, reply_body = await self._deliver_card_and_eval_reply(
            release_id=release_id,
            card_revision=next_card_rev,
            incoming_digest=expected_digest,
            card_payload=card_payload,
            eval_payload=eval_res.payload if eval_res.ok else None,
            eval_error=None if eval_res.ok else eval_res.error_redacted,
            ack_metadata={
                "full_evidence_digest": full_digest,
                "last_evaluated_digest": full_digest if eval_res.ok else prev_last_eval_digest,
                "tl_outcome": tl_result.outcome,
                "identity_gate_status": final_gate.status,
                "identity_conflict_reason": final_gate.conflict_reason,
                "verified_tmdb_id": verified_movie.tmdb_id if verified_movie else None,
                "verified_imdb_id": verified_movie.imdb_id if verified_movie else None,
                "verified_title": verified_movie.title if verified_movie else None,
                "verified_year": verified_movie.year if verified_movie else None,
                "radarr_status_snapshot": radarr.to_dict() if radarr else None,
                "evaluation_json": eval_res.payload if eval_res.ok else None,
            },
        )
        if card_resp.status == "ambiguous_delivery":
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="ambiguous_delivery",
                expected_incoming_digest=expected_digest,
                full_evidence_digest=full_digest,
                last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                tl_outcome=tl_result.outcome,
                identity_gate_status=final_gate.status,
                identity_conflict_reason=final_gate.conflict_reason,
                verified_tmdb_id=verified_movie.tmdb_id if verified_movie else None,
                verified_imdb_id=verified_movie.imdb_id if verified_movie else None,
                verified_title=verified_movie.title if verified_movie else None,
                verified_year=verified_movie.year if verified_movie else None,
                radarr_status_snapshot=radarr.to_dict() if radarr else None,
                evaluation_json=eval_res.payload if eval_res.ok else None,
                allow_downgrade_on_conflict=effective_genuine_conflict,
            )
            return

        if card_resp.status != "delivered" or card_resp.ack_message_id is None:
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="queued" if retry_count < 4 else "failed_terminal",
                expected_incoming_digest=expected_digest,
                full_evidence_digest=full_digest,
                last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                tl_outcome=tl_result.outcome,
                identity_gate_status=final_gate.status,
                identity_conflict_reason=final_gate.conflict_reason,
                verified_tmdb_id=verified_movie.tmdb_id if verified_movie else None,
                verified_imdb_id=verified_movie.imdb_id if verified_movie else None,
                verified_title=verified_movie.title if verified_movie else None,
                verified_year=verified_movie.year if verified_movie else None,
                radarr_status_snapshot=radarr.to_dict() if radarr else None,
                evaluation_json=eval_res.payload if eval_res.ok else None,
                next_attempt_at=self._retry_next_attempt_iso(),
                increment_retry=True,
                allow_downgrade_on_conflict=effective_genuine_conflict,
            )
            return

        # Card was delivered & ack persisted! Check evaluation reply status before completing.
        if reply_resp is not None and reply_resp.status == "failed" and reply_body is not None:
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="queued",
                expected_incoming_digest=expected_digest,
                card_revision=next_card_rev,
                full_evidence_digest=full_digest,
                last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                tl_outcome=tl_result.outcome,
                identity_gate_status=final_gate.status,
                identity_conflict_reason=final_gate.conflict_reason,
                verified_tmdb_id=verified_movie.tmdb_id if verified_movie else None,
                verified_imdb_id=verified_movie.imdb_id if verified_movie else None,
                verified_title=verified_movie.title if verified_movie else None,
                verified_year=verified_movie.year if verified_movie else None,
                ack_chat_id=card_resp.ack_chat_id,
                ack_message_id=card_resp.ack_message_id,
                ack_thread_id=card_resp.ack_thread_id,
                eval_reply_status="pending",
                pending_eval_reply=reply_body,
                radarr_status_snapshot=radarr.to_dict() if radarr else None,
                evaluation_json=eval_res.payload if eval_res.ok else None,
                next_attempt_at=self._retry_next_attempt_iso(),
                reset_retry=True,
                increment_retry=True,
                allow_downgrade_on_conflict=effective_genuine_conflict,
            )
            return

        reply_status = "sent" if (reply_resp and reply_resp.status == "sent") else "ambiguous_reply"
        await asyncio.to_thread(
            self.db.finalize_release,
            release_id,
            worker_id=self.worker_id,
            state="completed",
            expected_incoming_digest=expected_digest,
            card_revision=next_card_rev,
            full_evidence_digest=full_digest,
            last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
            tl_outcome=tl_result.outcome,
            identity_gate_status=final_gate.status,
            identity_conflict_reason=final_gate.conflict_reason,
            verified_tmdb_id=verified_movie.tmdb_id if verified_movie else None,
            verified_imdb_id=verified_movie.imdb_id if verified_movie else None,
            verified_title=verified_movie.title if verified_movie else None,
            verified_year=verified_movie.year if verified_movie else None,
            ack_chat_id=card_resp.ack_chat_id,
            ack_message_id=card_resp.ack_message_id,
            ack_thread_id=card_resp.ack_thread_id,
            eval_reply_status=reply_status,
            pending_card_supersede=None,
            pending_eval_reply=None,
            radarr_status_snapshot=radarr.to_dict() if radarr else None,
            evaluation_json=eval_res.payload if eval_res.ok else None,
            allow_downgrade_on_conflict=effective_genuine_conflict,
        )
