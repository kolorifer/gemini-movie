"""Resolve a recent, already delivered timeout review using its retained movie and receipt."""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from .classifier import ClassifiedWebhook, classify_rejection_reasons
from .radarr_retry import MAX_RADARR_ATTEMPTS, is_lookup_timeout
from .tmdb_verifier import IdentityGateResult, TmdbMovieRecord
from .torrentleech import TorrentLeechResult

if TYPE_CHECKING:
    from .worker import IntakeWorker


async def resume_legacy(worker: IntakeWorker, row: dict[str, Any], checkpoint: dict[str, Any]) -> None:
    from .worker import compute_card_content_digest

    release_id = row["release_id"]
    digest = checkpoint["incoming_digest"]
    source = checkpoint["source_occurrence_id"]
    attempts = checkpoint["attempts"]

    async def lease(state: str) -> bool:
        return await asyncio.to_thread(
            worker.db.renew_lease, release_id, worker_id=worker.worker_id,
            lease_seconds=worker.config.worker_lease_seconds, new_state=state,
            expected_incoming_digest=digest, expected_source_occurrence_id=source,
        )

    async def finish(state: str, **fields: Any) -> None:
        await asyncio.to_thread(worker.db.finalize_release, release_id, worker_id=worker.worker_id,
                                expected_incoming_digest=digest, state=state, **fields)

    if not await lease("evaluating"):
        return
    if (row.get("ack_incoming_digest") != digest or not row.get("ack_chat_id")
            or int(row.get("ack_message_id") or 0) <= 0 or row["card_revision"] < 1
            or row.get("identity_gate_status") != "verified" or row.get("identity_conflict_reason")
            or not row.get("verified_title") or not row.get("verified_tmdb_id")
            or row.get("eval_reply_status") != "sent" or attempts >= MAX_RADARR_ATTEMPTS):
        await finish("completed")
        return

    movie = TmdbMovieRecord(tmdb_id=row["verified_tmdb_id"], imdb_id=row.get("verified_imdb_id"),
                            title=row["verified_title"], year=row.get("verified_year"))
    classified = ClassifiedWebhook.from_sanitized_snapshot(json.loads(row["incoming_snapshot_json"]), digest)
    radarr = await worker.actions_client.get_radarr_status(
        tmdb_id=movie.tmdb_id, imdb_id=movie.imdb_id, release_title=movie.title,
    )
    attempts += 1
    if is_lookup_timeout(radarr) and attempts < MAX_RADARR_ATTEMPTS:
        await asyncio.to_thread(worker.db.defer_radarr_retry, release_id, worker_id=worker.worker_id,
                                expected_incoming_digest=digest, checkpoint={**checkpoint, "attempts": attempts},
                                next_attempt_at=worker._radarr_retry_at(attempts))
        return
    if radarr.status in ("lookup_failed", "ambiguous"):
        # The existing review already withholds every action. Do not send another warning/reply.
        await finish("completed", radarr_status_snapshot=radarr.to_dict())
        return

    rejection, rejection_class = classify_rejection_reasons(classified)
    alternative = rejection == "legacy_alt_grab_eligible"
    mode, reason, note = worker._decide_radarr_outcome(
        radarr=radarr, alt_grab_eligible=alternative, alt_grab_rejection_class=rejection_class,
    )
    if rejection == "quality_rejected_suppress" or (alternative and not radarr.alternative_grab_configured):
        mode, reason = "suppress", "quality_rejected"
    if mode == "suppress":
        note = f"Disqualified by Radarr state ({reason})"
    card = worker._build_card_payload(
        release_id=release_id, card_revision=row["card_revision"] + 1, source_occurrence_id=source,
        delivery_kind="retired" if mode == "suppress" else mode, classified=classified,
        tl_result=TorrentLeechResult(outcome=row.get("tl_outcome") or "not_applicable"),
        gate=IdentityGateResult(status="verified", verified_movie=movie),
        radarr=radarr, radarr_note=note, alt_rejection_class=rejection_class,
    )
    # The old snapshot retains the verified identity but not every piece of supporting evidence.
    card["accepted_evidence"] = f"Previously verified identity: {movie.title} (TMDB {movie.tmdb_id})"
    card["preserve_eval_reply"] = True
    card["expected_ack"] = {"chat_id": row["ack_chat_id"], "message_id": row["ack_message_id"],
                            "revision": row["card_revision"], "thread_id": row.get("ack_thread_id")}
    if not await lease("delivering"):
        return
    receipt = await worker.actions_client.deliver_card(card)
    if receipt.status == "failed":
        await finish("failed_terminal", radarr_status_snapshot=radarr.to_dict())
    elif (receipt.status != "delivered" or receipt.ack_message_id != row["ack_message_id"]
          or receipt.ack_chat_id != row["ack_chat_id"] or receipt.ack_thread_id != row.get("ack_thread_id")):
        await finish("ambiguous_delivery", radarr_status_snapshot=radarr.to_dict())
    else:
        await finish("suppressed" if mode == "suppress" else "completed", card_revision=card["card_revision"],
                     suppression_reason=reason, last_card_digest=compute_card_content_digest(card),
                     radarr_status_snapshot=radarr.to_dict())
