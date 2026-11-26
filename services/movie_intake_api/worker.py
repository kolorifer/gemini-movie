from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
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
    TriageEvalResult,
)
from .config import IntakeConfig
from .db import IntakeDB, utc_now, utc_now_iso
from .security import redact_secrets
from .tmdb_verifier import (
    IdentityGateResult,
    TmdbIndependentClient,
    TmdbMovieRecord,
    _titles_match,
    normalize_title,
    run_independent_identity_gate,
)
from .torrentleech import (
    TL_TRANSIENT_FAILURE_OUTCOMES,
    TorrentLeechResult,
    TorrentLeechScraper,
)

logger = logging.getLogger(__name__)

_CARD_DIGEST_EXCLUDED_KEYS = frozenset(
    {"card_revision", "source_occurrence_id", "preferred_topic_kind", "preserve_eval_reply"}
)


def compute_card_content_digest(card_payload: dict[str, Any]) -> str:
    """
    Hash semantic Telegram card content and action bindings (excluding revision,
    occurrence pointer, initial topic hint, and delivery control flags) so no-op
    follow-up webhooks do not needlessly edit an identical card or rotate buttons.
    """
    normalized = {k: v for k, v in card_payload.items() if k not in _CARD_DIGEST_EXCLUDED_KEYS}
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _directors_match_full(a: Optional[str], b: Optional[str]) -> bool:
    """Require full director name agreement (never match on shared first name alone)."""
    if not a or not b:
        return False

    def _split_names(raw: str) -> list[list[str]]:
        parts = re.split(r"\s*(?:,|&|\band\b|/)\s*", raw, flags=re.IGNORECASE)
        out: list[list[str]] = []
        for part in parts:
            norm = normalize_title(part)
            toks = [t for t in norm.split() if t]
            if toks and norm not in ("unknown", "n a", "na", "none"):
                out.append(toks)
        return out

    names_a = _split_names(a)
    names_b = _split_names(b)
    if not names_a or not names_b:
        return False
    for ta in names_a:
        for tb in names_b:
            if ta == tb:
                return True
            if len(ta) >= 2 and len(tb) >= 2 and ta[-1] == tb[-1] and len(ta[-1]) >= 2:
                if ta[0] == tb[0] or (
                    (len(ta[0]) == 1 or len(tb[0]) == 1) and ta[0][0] == tb[0][0]
                ):
                    return True
    return False


def _eval_contradicts_movie(eval_payload: Any, movie: TmdbMovieRecord) -> Optional[str]:
    if not isinstance(eval_payload, dict):
        return None
    cat_ident = eval_payload.get("catalogue_identification")
    if isinstance(cat_ident, dict) and cat_ident.get("compatible") is False:
        reason = str(cat_ident.get("conflict_reason") or "Evaluator catalogue verification reported incompatibility")
        return f"Evaluator catalogue conflict: {reason}"

    ident_blocks: list[tuple[str, dict[str, Any]]] = []
    if isinstance(eval_payload.get("identification"), dict):
        ident_blocks.append(("identification", eval_payload["identification"]))
    if isinstance(eval_payload.get("model_identification"), dict):
        ident_blocks.append(("model_identification", eval_payload["model_identification"]))
    if not ident_blocks:
        return None

    all_movie_titles = movie.all_titles()
    for label, ident in ident_blocks:
        ev_imdb = str(ident.get("imdb_id") or "").strip().lower() or None
        ev_tmdb = ident.get("tmdb_id")
        if ev_imdb and movie.imdb_id and ev_imdb != movie.imdb_id:
            return f"Evaluator ({label}) proposed {ev_imdb}, contradicting pre-verified {movie.imdb_id}"
        if (
            isinstance(ev_tmdb, int)
            and not isinstance(ev_tmdb, bool)
            and ev_tmdb != movie.tmdb_id
        ):
            return f"Evaluator ({label}) proposed TMDB {ev_tmdb}, contradicting pre-verified TMDB {movie.tmdb_id}"

        ev_year = ident.get("year")
        if (
            isinstance(ev_year, int)
            and not isinstance(ev_year, bool)
            and movie.year is not None
            and abs(ev_year - movie.year) > 1
        ):
            return f"Evaluator ({label}) proposed year {ev_year}, contradicting pre-verified {movie.year}"

        ev_titles = [
            str(ident.get(k) or "").strip()
            for k in ("canonical_title", "original_title")
            if str(ident.get(k) or "").strip()
            and str(ident.get(k) or "").strip().lower() not in ("unknown", "n/a")
        ]
        if ev_titles and all_movie_titles:
            if not any(_titles_match(et, all_movie_titles) for et in ev_titles):
                return (
                    f"Evaluator ({label}) proposed title '{ev_titles[0]}', "
                    f"contradicting pre-verified '{movie.title}'"
                )

        ev_dir = str(ident.get("director") or "").strip()
        if (
            ev_dir
            and ev_dir.lower() not in ("unknown", "n/a", "none")
            and movie.director
            and not _directors_match_full(ev_dir, movie.director)
        ):
            return (
                f"Evaluator ({label}) proposed director '{ev_dir}', "
                f"contradicting pre-verified director '{movie.director}'"
            )

    return None


def _is_ambiguous_card_ack(resp: DeliveryCardResponse) -> bool:
    """Only explicit status=='failed' proves non-delivery; usable 'delivered' requires a positive ack_message_id."""
    if resp.status == "failed":
        return False
    if resp.status == "delivered" and resp.ack_message_id is not None and resp.ack_message_id > 0:
        return False
    return True


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
        clean_idx = redact_secrets(classified.indexer_raw, max_chars=80)
        if clean_idx:
            tinfo["indexer"] = clean_idx
    if classified.category:
        clean_cat = redact_secrets(classified.category, max_chars=120)
        if clean_cat:
            tinfo["category"] = clean_cat

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
        clean_audio = [redact_secrets(a, max_chars=40) for a in tl_result.audio_languages if a]
        clean_audio = [a for a in clean_audio if a]
        if clean_audio:
            tinfo["audio_languages"] = clean_audio

    if classified.resolution:
        clean_res = redact_secrets(classified.resolution, max_chars=40)
        if clean_res:
            tinfo["resolution"] = clean_res
    if classified.source:
        clean_src = redact_secrets(classified.source, max_chars=60)
        if clean_src:
            tinfo["source"] = clean_src
    if classified.dynamic_range:
        clean_dr = [redact_secrets(d, max_chars=40) for d in classified.dynamic_range if d]
        clean_dr = [d for d in clean_dr if d]
        if clean_dr:
            tinfo["dynamic_range"] = clean_dr
    if classified.media_processing:
        clean_mp = redact_secrets(classified.media_processing, max_chars=60)
        if clean_mp:
            tinfo["media_processing"] = clean_mp
    if classified.group:
        clean_grp = redact_secrets(classified.group, max_chars=80)
        if clean_grp:
            tinfo["group"] = clean_grp
    if classified.size_gb is not None:
        tinfo["size_gb"] = classified.size_gb

    # Preserve explicit False as well as True; omit only when unknown
    effective_freeleech: Optional[bool] = classified.freeleech
    if effective_freeleech is None and tl_result.freeleech_badge is not None:
        effective_freeleech = tl_result.freeleech_badge
    if effective_freeleech is not None:
        tinfo["freeleech"] = bool(effective_freeleech)

    if tl_result.sanitized_description:
        tinfo["description"] = redact_secrets(tl_result.sanitized_description, max_chars=900)
    if tl_result.sanitized_nfo:
        tinfo["nfo_text"] = redact_secrets(tl_result.sanitized_nfo, max_chars=900)

    body: dict[str, Any] = {
        "raw_text": redact_secrets(classified.exact_release_name, max_chars=512),
        "force_refresh": bool(force_refresh),
    }
    if classified.hint_title:
        clean_hint = redact_secrets(classified.hint_title, max_chars=240)
        if clean_hint:
            body["hint_title"] = clean_hint
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
        worker_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.tl_scraper = tl_scraper
        self.tmdb_client = tmdb_client
        self.triage_client = triage_client
        self.actions_client = actions_client
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
                # Issue #13 §5a: Never pass active unsanitized exception traceback to logger.exception
                logger.error(
                    "Intake worker loop error (%s): %s",
                    exc.__class__.__name__,
                    redact_secrets(str(exc), max_chars=240),
                )

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
        Apply Pre-Delivery Suppression Matrix (§5.3 + legacy Grab Anyway rules).
        Returns (mode, suppression_reason, radarr_note) where mode is one of:
          suppress | actionable_grab | alternative_grab | evidence_review
        """
        if radarr.status == "watched_unmonitored":
            return "suppress", "watched_unmonitored", None
        if radarr.status == "owned_file":
            if (
                alt_grab_eligible
                and alt_grab_rejection_class
                and radarr.alternative_grab_configured
                and radarr.monitored is True
                and isinstance(radarr.movie_file_snapshot, dict)
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
        preferred_topic_kind: Optional[str] = None,
    ) -> dict[str, Any]:
        movie = gate.verified_movie
        # Issue #13 §6a: Build concise explanation of accepted evidence or disagreements
        accepted_evidence: Optional[str] = None
        if gate.status == "verified" and movie is not None:
            basis_bits: list[str] = []
            if tl_result.imdb_id and tl_result.imdb_id == movie.imdb_id:
                basis_bits.append("TL listing IMDb")
            elif classified.meta_imdb and classified.meta_imdb == movie.imdb_id:
                basis_bits.append("autobrr IMDb")
            if classified.meta_tmdb and classified.meta_tmdb == movie.tmdb_id:
                basis_bits.append("autobrr TMDB")
            if not basis_bits:
                basis_bits.append("evaluator high-confidence match")
            if tl_result.file_runtime_minutes is not None:
                basis_bits.append(f"MediaInfo {tl_result.file_runtime_minutes}m")
            basis_bits.append("TMDB bidirectional + title/year verified")
            accepted_evidence = (
                f"{movie.imdb_id or '?'} / TMDB {movie.tmdb_id} via {', '.join(basis_bits)}"
            )
        # Defense in depth: only supply verified IDs for actionable/alternative cards when gate passed
        payload: dict[str, Any] = {
            "release_id": release_id,
            "card_revision": card_revision,
            "source_occurrence_id": source_occurrence_id,
            "delivery_kind": delivery_kind,
            "identity_gate_status": gate.status,
            "exact_release_name": classified.exact_release_name,
            "indexer": classified.indexer_raw or classified.indexer_norm,
            "external_id": classified.external_id_norm,
            "category": classified.category,
            "size_gb": classified.size_gb,
            "freeleech": classified.freeleech if classified.freeleech is not None else tl_result.freeleech_badge,
            "tl_outcome": tl_result.outcome,
            "radarr_status": radarr.status if radarr else "unchecked",
            "radarr_note": radarr_note,
            "conflict_reason": gate.conflict_reason,
            "checks_passed": list(gate.checks_passed),
            "checks_failed": list(gate.checks_failed),
            "accepted_evidence": accepted_evidence,
            "observed_meta_imdb": classified.meta_imdb,
            "observed_meta_tmdb": classified.meta_tmdb,
            "observed_tl_imdb": tl_result.imdb_id,
            "file_runtime_minutes": tl_result.file_runtime_minutes,
            "audio_languages": list(tl_result.audio_languages),
        }
        if preferred_topic_kind:
            payload["preferred_topic_kind"] = preferred_topic_kind
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
            payload["rejection_reasons"] = list(classified.rejections)
            if isinstance(radarr.movie_file_snapshot, dict):
                payload["bound_movie_file_snapshot"] = dict(radarr.movie_file_snapshot)
        return payload

    async def _retire_acknowledged_card(
        self,
        *,
        row: dict[str, Any],
        release_id: str,
        source_occurrence_id: str,
        expected_digest: str,
        classified: ClassifiedWebhook,
        retry_count: int,
        suppression_reason: str,
        retirement_note: str,
        tl_result: Optional[TorrentLeechResult] = None,
        movie: Optional[TmdbMovieRecord] = None,
        radarr: Optional[RadarrStatusResponse] = None,
        full_digest: Optional[str] = None,
        eval_res: Optional[TriageEvalResult] = None,
        prev_last_eval_digest: Optional[str] = None,
    ) -> None:
        """
        Issue #13 §2a: When later evidence disqualifies an already-delivered card (e.g.
        late "Quality is not wanted in profile" or Radarr watched/owned transition),
        edit the acknowledged card to delivery_kind='retired' with zero buttons and
        supersede all previously active action tokens while preserving any sent evaluation reply.
        """
        prev_card_rev = int(row.get("card_revision") or 0)
        next_rev = prev_card_rev + 1
        prev_eval_status = str(row.get("eval_reply_status") or "pending")
        final_eval_status = (
            prev_eval_status if prev_eval_status in ("sent", "ambiguous_reply") else "skipped"
        )
        eff_tmdb = movie.tmdb_id if movie is not None else row.get("verified_tmdb_id")
        eff_imdb = movie.imdb_id if movie is not None else row.get("verified_imdb_id")
        eff_title = movie.title if movie is not None else row.get("verified_title")
        eff_year = movie.year if movie is not None else row.get("verified_year")
        eff_gate_status = (
            "verified"
            if (movie is not None or str(row.get("identity_gate_status") or "") == "verified")
            else str(row.get("identity_gate_status") or "evidence_review")
        )
        retired_card: dict[str, Any] = {
            "release_id": release_id,
            "card_revision": next_rev,
            "source_occurrence_id": source_occurrence_id,
            "delivery_kind": "retired",
            "identity_gate_status": eff_gate_status,
            "exact_release_name": classified.exact_release_name,
            "indexer": classified.indexer_raw or classified.indexer_norm,
            "external_id": classified.external_id_norm,
            "category": classified.category,
            "size_gb": classified.size_gb,
            "freeleech": (
                classified.freeleech
                if classified.freeleech is not None
                else (tl_result.freeleech_badge if tl_result is not None else None)
            ),
            "tl_outcome": tl_result.outcome if tl_result is not None else (row.get("tl_outcome") or "not_applicable"),
            "radarr_status": radarr.status if radarr is not None else "disqualified",
            "radarr_note": retirement_note,
            "conflict_reason": retirement_note,
            "observed_meta_imdb": classified.meta_imdb,
            "observed_meta_tmdb": classified.meta_tmdb,
            "observed_tl_imdb": tl_result.imdb_id if tl_result is not None else None,
            "file_runtime_minutes": tl_result.file_runtime_minutes if tl_result is not None else None,
            "audio_languages": list(tl_result.audio_languages) if tl_result is not None else [],
            "preserve_eval_reply": True,
        }
        if isinstance(eff_tmdb, int) and not isinstance(eff_tmdb, bool) and eff_tmdb > 0:
            retired_card["verified_tmdb_id"] = eff_tmdb
        if isinstance(eff_imdb, str) and eff_imdb.strip():
            retired_card["verified_imdb_id"] = eff_imdb.strip().lower()
        if isinstance(eff_title, str) and eff_title.strip():
            retired_card["verified_title"] = eff_title.strip()
        if isinstance(eff_year, int) and not isinstance(eff_year, bool):
            retired_card["verified_year"] = eff_year

        retired_digest = compute_card_content_digest(retired_card)
        prev_card_digest = str(row.get("last_card_digest") or "") or None
        if prev_card_digest == retired_digest and str(row.get("suppression_reason") or "") == suppression_reason:
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="suppressed",
                expected_incoming_digest=expected_digest,
                suppression_reason=suppression_reason,
                full_evidence_digest=full_digest,
                last_evaluated_digest=(
                    full_digest if (eval_res is not None and eval_res.ok and full_digest) else prev_last_eval_digest
                ),
                tl_outcome=tl_result.outcome if tl_result is not None else None,
                identity_gate_status=eff_gate_status,
                verified_tmdb_id=eff_tmdb,
                verified_imdb_id=eff_imdb,
                verified_title=eff_title,
                verified_year=eff_year,
                eval_reply_status=final_eval_status,
                pending_card_supersede=None,
                pending_eval_reply=None,
                radarr_status_snapshot=radarr.to_dict() if radarr is not None else None,
                evaluation_json=eval_res.payload if (eval_res is not None and eval_res.ok) else None,
            )
            return

        await asyncio.to_thread(
            self.db.renew_lease,
            release_id,
            worker_id=self.worker_id,
            lease_seconds=self.config.worker_lease_seconds,
            new_state="delivering",
        )
        ret_resp = await self.actions_client.deliver_card(retired_card)
        if _is_ambiguous_card_ack(ret_resp):
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="ambiguous_delivery",
                expected_incoming_digest=expected_digest,
                suppression_reason=suppression_reason,
                full_evidence_digest=full_digest,
                tl_outcome=tl_result.outcome if tl_result is not None else None,
                identity_gate_status=eff_gate_status,
                verified_tmdb_id=eff_tmdb,
                verified_imdb_id=eff_imdb,
                verified_title=eff_title,
                verified_year=eff_year,
                radarr_status_snapshot=radarr.to_dict() if radarr is not None else None,
                evaluation_json=eval_res.payload if (eval_res is not None and eval_res.ok) else None,
            )
            return
        if ret_resp.status != "delivered" or ret_resp.ack_message_id is None:
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="queued" if retry_count < 4 else "failed_terminal",
                expected_incoming_digest=expected_digest,
                suppression_reason=suppression_reason,
                full_evidence_digest=full_digest,
                tl_outcome=tl_result.outcome if tl_result is not None else None,
                identity_gate_status=eff_gate_status,
                verified_tmdb_id=eff_tmdb,
                verified_imdb_id=eff_imdb,
                verified_title=eff_title,
                verified_year=eff_year,
                radarr_status_snapshot=radarr.to_dict() if radarr is not None else None,
                evaluation_json=eval_res.payload if (eval_res is not None and eval_res.ok) else None,
                next_attempt_at=self._retry_next_attempt_iso(),
                increment_retry=True,
            )
            return

        await asyncio.to_thread(
            self.db.finalize_release,
            release_id,
            worker_id=self.worker_id,
            state="suppressed",
            expected_incoming_digest=expected_digest,
            suppression_reason=suppression_reason,
            card_revision=next_rev,
            full_evidence_digest=full_digest,
            last_evaluated_digest=(
                full_digest if (eval_res is not None and eval_res.ok and full_digest) else prev_last_eval_digest
            ),
            last_card_digest=retired_digest,
            tl_outcome=tl_result.outcome if tl_result is not None else None,
            identity_gate_status=eff_gate_status,
            verified_tmdb_id=eff_tmdb,
            verified_imdb_id=eff_imdb,
            verified_title=eff_title,
            verified_year=eff_year,
            ack_chat_id=ret_resp.ack_chat_id,
            ack_message_id=ret_resp.ack_message_id,
            ack_thread_id=ret_resp.ack_thread_id,
            eval_reply_status=final_eval_status,
            pending_card_supersede=None,
            pending_eval_reply=None,
            radarr_status_snapshot=radarr.to_dict() if radarr is not None else None,
            evaluation_json=eval_res.payload if (eval_res is not None and eval_res.ok) else None,
        )

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
        if card_resp.status != "delivered" or card_resp.ack_message_id is None or card_resp.ack_message_id <= 0:
            # Never post an unanchored reply when ack_message_id is missing / invalid / ambiguous_delivery
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
            last_card_digest=compute_card_content_digest(card_payload),
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
        full_dig = row.get("full_evidence_digest")
        last_eval_dig = row.get("last_evaluated_digest")

        def _reply_digest_for(reply_obj: Optional[dict[str, Any]]) -> Optional[str]:
            if (
                isinstance(reply_obj, dict)
                and isinstance(reply_obj.get("evaluation"), dict)
                and full_dig
                and last_eval_dig == full_dig
            ):
                return str(last_eval_dig)
            return None

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
            sup_card_digest = compute_card_content_digest(supersede_card)
            effective_rev = int(supersede_card.get("card_revision") or (int(row.get("card_revision") or 0) + 1))
            conflict_reason = supersede_card.get("conflict_reason")
            sup_resp = await self.actions_client.deliver_card(supersede_card)
            if _is_ambiguous_card_ack(sup_resp):
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
            eval_reply_dig: Any = ...
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
                        last_card_digest=sup_card_digest,
                        last_eval_reply_digest=None,
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
                eval_reply_dig = _reply_digest_for(pending_reply)
            else:
                reply_status = str(row.get("eval_reply_status") or "sent")

            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="completed",
                expected_incoming_digest=expected_digest,
                card_revision=effective_rev,
                last_card_digest=sup_card_digest,
                last_eval_reply_digest=eval_reply_dig,
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
            last_eval_reply_digest=_reply_digest_for(pending_reply),
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
        prev_last_card_digest = row.get("last_card_digest")
        prev_last_eval_reply_digest = row.get("last_eval_reply_digest")
        prev_eval_reply_status = str(row.get("eval_reply_status") or "pending")
        prev_gate_status = str(row.get("identity_gate_status") or "")
        prev_verified = prev_gate_status == "verified"
        retry_count = int(row.get("retry_count") or 0)

        prev_eval_payload: Optional[dict[str, Any]] = None
        raw_prev_eval = row.get("evaluation_json")
        if isinstance(raw_prev_eval, str) and raw_prev_eval.strip():
            try:
                parsed_eval = json.loads(raw_prev_eval)
                if isinstance(parsed_eval, dict):
                    prev_eval_payload = parsed_eval
            except ValueError:
                prev_eval_payload = None

        has_acknowledged_card = (
            prev_card_revision >= 1
            and row.get("ack_message_id") is not None
            and int(row["ack_message_id"]) > 0
        )
        has_unsent_eval_for_ack = (
            has_acknowledged_card and prev_eval_reply_status == "pending"
        )
        has_acknowledged_verified_card = prev_verified and has_acknowledged_card

        snap = json.loads(str(row["incoming_snapshot_json"]))
        classified = ClassifiedWebhook.from_sanitized_snapshot(snap, expected_digest)

        # Step 1: Routine quality rejection early check (§5.1 Step 3 + legacy Grab Anyway capability check)
        rej_mode, rej_class = classify_rejection_reasons(classified)
        if rej_mode == "quality_rejected_suppress":
            if not has_acknowledged_card:
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
            rej_detail = "; ".join(classified.rejections[:3]) if classified.rejections else (rej_class or "quality_rejected")
            await self._retire_acknowledged_card(
                row=row,
                release_id=release_id,
                source_occurrence_id=source_occurrence_id,
                expected_digest=expected_digest,
                classified=classified,
                retry_count=retry_count,
                suppression_reason="quality_rejected",
                retirement_note=f"Disqualified by later Radarr rejection: {rej_detail}",
                prev_last_eval_digest=prev_last_eval_digest,
            )
            return

        alt_grab_eligible = False
        preferred_topic_kind: Optional[str] = None
        if rej_mode == "legacy_alt_grab_eligible":
            alt_cap = await self.actions_client.get_capabilities()
            if alt_cap is False:
                if not has_acknowledged_card:
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
                rej_detail = "; ".join(classified.rejections[:3]) if classified.rejections else (rej_class or "quality_rejected")
                await self._retire_acknowledged_card(
                    row=row,
                    release_id=release_id,
                    source_occurrence_id=source_occurrence_id,
                    expected_digest=expected_digest,
                    classified=classified,
                    retry_count=retry_count,
                    suppression_reason="quality_rejected",
                    retirement_note=f"Disqualified by later Radarr rejection: {rej_detail}",
                    prev_last_eval_digest=prev_last_eval_digest,
                )
                return
            elif alt_cap is None:
                if retry_count < 4:
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="queued",
                        expected_incoming_digest=expected_digest,
                        next_attempt_at=self._retry_next_attempt_iso(),
                        increment_retry=True,
                    )
                elif not has_acknowledged_card:
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="suppressed",
                        expected_incoming_digest=expected_digest,
                        suppression_reason="quality_rejected",
                        eval_reply_status="skipped",
                    )
                else:
                    rej_detail = "; ".join(classified.rejections[:3]) if classified.rejections else (rej_class or "quality_rejected")
                    await self._retire_acknowledged_card(
                        row=row,
                        release_id=release_id,
                        source_occurrence_id=source_occurrence_id,
                        expected_digest=expected_digest,
                        classified=classified,
                        retry_count=retry_count,
                        suppression_reason="quality_rejected",
                        retirement_note=f"Disqualified by later Radarr rejection: {rej_detail}",
                        prev_last_eval_digest=prev_last_eval_digest,
                    )
                return
            else:
                alt_grab_eligible = True
                preferred_topic_kind = "alternative_grab"

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
        if has_acknowledged_verified_card:
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

        # Guard: if a verified card was already acknowledged previously and this pass had
        # only a transient TL lookup failure (never a genuine tracker conflict or metadata
        # contradiction against the verified identity), preserve that card.
        if (
            has_acknowledged_verified_card
            and pre_gate.status != "verified"
            and not pre_gate.is_genuine_conflict
            and not has_prev_verified_conflict
            and tl_result.outcome in TL_TRANSIENT_FAILURE_OUTCOMES
        ):
            if has_unsent_eval_for_ack:
                # Card ack was recorded before crashing mid-/evaluate;
                # re-queue rather than completing without sending the initial evaluation reply.
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="queued" if retry_count < 4 else "failed_terminal",
                    expected_incoming_digest=expected_digest,
                    tl_outcome=tl_result.outcome,
                    next_attempt_at=self._retry_next_attempt_iso(),
                    increment_retry=True,
                )
                return
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
                if has_acknowledged_card:
                    await self._retire_acknowledged_card(
                        row=row,
                        release_id=release_id,
                        source_occurrence_id=source_occurrence_id,
                        expected_digest=expected_digest,
                        classified=classified,
                        retry_count=retry_count,
                        suppression_reason=suppression_reason or "radarr_suppressed",
                        retirement_note=f"Disqualified by Radarr state ({suppression_reason or radarr.status})",
                        tl_result=tl_result,
                        movie=movie,
                        radarr=radarr,
                        prev_last_eval_digest=prev_last_eval_digest,
                    )
                    return
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

            eval_body_probe, full_digest = build_triage_evaluate_request(
                classified=classified,
                tl_result=tl_result,
                force_refresh=False,
            )
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
                preferred_topic_kind=preferred_topic_kind,
            )
            candidate_card_digest = compute_card_content_digest(card_payload)
            card_unchanged = (
                has_acknowledged_card
                and prev_last_card_digest is not None
                and prev_last_card_digest == candidate_card_digest
            )
            prev_eval_matches_verified = prev_verified
            if not prev_eval_matches_verified and isinstance(prev_eval_payload, dict):
                prev_ident = prev_eval_payload.get("identification")
                if isinstance(prev_ident, dict):
                    ev_conf = str(prev_ident.get("match_confidence") or "").strip().lower()
                    ev_imdb = str(prev_ident.get("imdb_id") or "").strip().lower() or None
                    ev_tmdb = prev_ident.get("tmdb_id")
                    prev_eval_matches_verified = ev_conf == "high" and (
                        (movie.imdb_id is not None and ev_imdb == movie.imdb_id)
                        or (
                            isinstance(ev_tmdb, int)
                            and not isinstance(ev_tmdb, bool)
                            and ev_tmdb == movie.tmdb_id
                        )
                    )
            eval_reusable = (
                has_acknowledged_card
                and prev_eval_reply_status in ("sent", "ambiguous_reply")
                and prev_last_eval_reply_digest is not None
                and prev_last_eval_reply_digest == full_digest
                and isinstance(prev_eval_payload, dict)
                and prev_eval_matches_verified
                and _eval_contradicts_movie(prev_eval_payload, movie) is None
            )

            # Case 5a: Both rendered card content and movie evaluation evidence are unchanged
            # (e.g., release.new followed by Radarr action.rejected "Unknown Movie").
            if card_unchanged and eval_reusable:
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="completed",
                    expected_incoming_digest=expected_digest,
                    card_revision=prev_card_revision,
                    full_evidence_digest=full_digest,
                    tl_outcome=tl_result.outcome,
                    identity_gate_status="verified",
                    identity_conflict_reason=None,
                    verified_tmdb_id=movie.tmdb_id,
                    verified_imdb_id=movie.imdb_id,
                    verified_title=movie.title,
                    verified_year=movie.year,
                    radarr_status_snapshot=radarr.to_dict(),
                )
                return

            # Case 5b: Card changed (e.g. Radarr action transition) while evaluation evidence
            # is unchanged -> edit card with preserve_eval_reply=True and reuse sent reply.
            if eval_reusable:
                card_payload["preserve_eval_reply"] = True
                await asyncio.to_thread(
                    self.db.renew_lease,
                    release_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.config.worker_lease_seconds,
                    new_state="delivering",
                )
                card_resp = await self.actions_client.deliver_card(card_payload)
                if _is_ambiguous_card_ack(card_resp):
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
                if card_resp.eval_reply_preserved:
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="completed",
                        expected_incoming_digest=expected_digest,
                        card_revision=next_card_rev,
                        full_evidence_digest=full_digest,
                        last_card_digest=candidate_card_digest,
                        last_eval_reply_digest=prev_last_eval_reply_digest,
                        tl_outcome=tl_result.outcome,
                        identity_gate_status="verified",
                        identity_conflict_reason=None,
                        verified_tmdb_id=movie.tmdb_id,
                        verified_imdb_id=movie.imdb_id,
                        verified_title=movie.title,
                        verified_year=movie.year,
                        ack_chat_id=card_resp.ack_chat_id,
                        ack_message_id=card_resp.ack_message_id,
                        ack_thread_id=card_resp.ack_thread_id,
                        eval_reply_status=prev_eval_reply_status,
                        pending_card_supersede=None,
                        pending_eval_reply=None,
                        radarr_status_snapshot=radarr.to_dict(),
                    )
                    return
                # Safety fallback if Actions did not have an existing sent eval reply to preserve
                await asyncio.to_thread(
                    self.db.record_card_ack,
                    release_id,
                    worker_id=self.worker_id,
                    card_revision=next_card_rev,
                    ack_chat_id=card_resp.ack_chat_id,
                    ack_message_id=card_resp.ack_message_id,
                    ack_thread_id=card_resp.ack_thread_id,
                    ack_incoming_digest=expected_digest,
                    last_card_digest=candidate_card_digest,
                    tl_outcome=tl_result.outcome,
                    identity_gate_status="verified",
                    identity_conflict_reason=None,
                    verified_tmdb_id=movie.tmdb_id,
                    verified_imdb_id=movie.imdb_id,
                    verified_title=movie.title,
                    verified_year=movie.year,
                    radarr_status_snapshot=radarr.to_dict(),
                )
            else:
                # If the worker crashed after persisting card ack and before completing /evaluate
                # (or a follow-up arrived with identical card content while eval was still pending),
                # reuse the persisted receipt instead of re-sending or bumping card_revision.
                reuse_acked_card = has_unsent_eval_for_ack and (
                    str(row.get("ack_incoming_digest") or "") == expected_digest
                    or card_unchanged
                )
                if reuse_acked_card:
                    next_card_rev = prev_card_revision
                    card_resp = DeliveryCardResponse(
                        status="delivered",
                        ack_chat_id=row.get("ack_chat_id"),
                        ack_message_id=int(row["ack_message_id"]),
                        ack_thread_id=row.get("ack_thread_id"),
                    )
                else:
                    # Deliver prompt card immediately BEFORE waiting on Gemini evaluation
                    await asyncio.to_thread(
                        self.db.renew_lease,
                        release_id,
                        worker_id=self.worker_id,
                        lease_seconds=self.config.worker_lease_seconds,
                        new_state="delivering",
                    )
                    card_resp = await self.actions_client.deliver_card(card_payload)
                    if _is_ambiguous_card_ack(card_resp):
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

                    # Persist card ack receipt + verified identity atomically so a crash during
                    # /evaluate preserves the delivered verified card across recovery & transient outages.
                    await asyncio.to_thread(
                        self.db.record_card_ack,
                        release_id,
                        worker_id=self.worker_id,
                        card_revision=next_card_rev,
                        ack_chat_id=card_resp.ack_chat_id,
                        ack_message_id=card_resp.ack_message_id,
                        ack_thread_id=card_resp.ack_thread_id,
                        ack_incoming_digest=expected_digest,
                        last_card_digest=candidate_card_digest,
                        tl_outcome=tl_result.outcome,
                        identity_gate_status="verified",
                        identity_conflict_reason=None,
                        verified_tmdb_id=movie.tmdb_id,
                        verified_imdb_id=movie.imdb_id,
                        verified_title=movie.title,
                        verified_year=movie.year,
                        radarr_status_snapshot=radarr.to_dict(),
                    )

            # Now call movie-triage-api /evaluate and post bound reply
            await asyncio.to_thread(
                self.db.renew_lease,
                release_id,
                worker_id=self.worker_id,
                lease_seconds=self.config.worker_lease_seconds,
                new_state="evaluating",
            )
            if prev_last_eval_digest is not None and full_digest != prev_last_eval_digest:
                eval_body_probe["force_refresh"] = True

            eval_res = await self.triage_client.evaluate(eval_body_probe)

            # If evaluator contradicts our pre-evaluation verified movie with a different
            # canonical IMDb/TMDB ID, supersede the bound card to evidence_review!
            eval_conflict_reason = (
                _eval_contradicts_movie(eval_res.payload, movie) if eval_res.ok else None
            )

            effective_rev = next_card_rev
            effective_gate_status = "verified"
            effective_conflict: Optional[str] = None
            effective_card_digest = candidate_card_digest
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
                    preferred_topic_kind=preferred_topic_kind,
                )
                supersede_card_digest = compute_card_content_digest(supersede_card)
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
                if _is_ambiguous_card_ack(sup_resp):
                    await asyncio.to_thread(
                        self.db.finalize_release,
                        release_id,
                        worker_id=self.worker_id,
                        state="ambiguous_delivery",
                        expected_incoming_digest=expected_digest,
                        card_revision=next_card_rev,
                        full_evidence_digest=full_digest,
                        last_evaluated_digest=full_digest if eval_res.ok else prev_last_eval_digest,
                        last_card_digest=candidate_card_digest,
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
                        last_card_digest=candidate_card_digest,
                        last_eval_reply_digest=None,
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
                effective_card_digest = supersede_card_digest

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
                    last_card_digest=effective_card_digest,
                    last_eval_reply_digest=None,
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
                last_card_digest=effective_card_digest,
                last_eval_reply_digest=full_digest if eval_res.ok else None,
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
        downgrading_verified = has_acknowledged_verified_card and (
            pre_gate.is_genuine_conflict
            or has_prev_verified_conflict
            or tl_result.outcome in ("listing_id_conflict", "listing_id_malformed")
        )
        can_reuse_prev_eval = (
            has_acknowledged_card
            and not downgrading_verified
            and prev_eval_reply_status in ("sent", "ambiguous_reply")
            and prev_last_eval_reply_digest is not None
            and prev_last_eval_reply_digest == full_digest
            and isinstance(prev_eval_payload, dict)
        )

        if can_reuse_prev_eval:
            eval_res = TriageEvalResult(ok=True, payload=prev_eval_payload)
        else:
            await asyncio.to_thread(
                self.db.renew_lease,
                release_id,
                worker_id=self.worker_id,
                lease_seconds=self.config.worker_lease_seconds,
                new_state="evaluating",
            )
            if (
                pre_gate.is_genuine_conflict
                or has_prev_verified_conflict
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

        # If reusing a prior evaluation uncovered a brand-new genuine conflict on an
        # already-verified card, force a fresh conflict-aware evaluation before superseding.
        if can_reuse_prev_eval and has_acknowledged_verified_card and effective_genuine_conflict:
            can_reuse_prev_eval = False
            await asyncio.to_thread(
                self.db.renew_lease,
                release_id,
                worker_id=self.worker_id,
                lease_seconds=self.config.worker_lease_seconds,
                new_state="evaluating",
            )
            eval_body, full_digest = build_triage_evaluate_request(
                classified=classified,
                tl_result=tl_result,
                force_refresh=True,
                extra_conflict_note=final_gate.conflict_reason or conflict_context,
            )
            eval_res = await self.triage_client.evaluate(eval_body)

        # Never downgrade an already-acknowledged verified card on a temporary lookup failure (non-conflict)
        if has_acknowledged_verified_card and final_gate.status != "verified" and not effective_genuine_conflict:
            if has_unsent_eval_for_ack:
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="queued" if retry_count < 4 else "failed_terminal",
                    expected_incoming_digest=expected_digest,
                    full_evidence_digest=full_digest,
                    tl_outcome=tl_result.outcome,
                    next_attempt_at=self._retry_next_attempt_iso(),
                    increment_retry=True,
                )
                return
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
                if has_acknowledged_card:
                    await self._retire_acknowledged_card(
                        row=row,
                        release_id=release_id,
                        source_occurrence_id=source_occurrence_id,
                        expected_digest=expected_digest,
                        classified=classified,
                        retry_count=retry_count,
                        suppression_reason=suppression_reason or "radarr_suppressed",
                        retirement_note=f"Disqualified by Radarr state ({suppression_reason or radarr.status})",
                        tl_result=tl_result,
                        movie=movie,
                        radarr=radarr,
                        full_digest=full_digest,
                        eval_res=eval_res,
                        prev_last_eval_digest=prev_last_eval_digest,
                    )
                    return
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
            preferred_topic_kind=preferred_topic_kind,
        )
        candidate_card_digest = compute_card_content_digest(card_payload)
        verified_movie: Optional[TmdbMovieRecord] = (
            final_gate.verified_movie if final_gate.status == "verified" else None
        )
        card_unchanged = (
            has_acknowledged_card
            and prev_last_card_digest is not None
            and prev_last_card_digest == candidate_card_digest
        )

        # Case 5a (Unresolved path): Both rendered card and evaluation evidence are unchanged
        if card_unchanged and can_reuse_prev_eval:
            await asyncio.to_thread(
                self.db.finalize_release,
                release_id,
                worker_id=self.worker_id,
                state="completed",
                expected_incoming_digest=expected_digest,
                card_revision=prev_card_revision,
                full_evidence_digest=full_digest,
                tl_outcome=tl_result.outcome,
                identity_gate_status=final_gate.status,
                identity_conflict_reason=final_gate.conflict_reason,
                verified_tmdb_id=verified_movie.tmdb_id if verified_movie else None,
                verified_imdb_id=verified_movie.imdb_id if verified_movie else None,
                verified_title=verified_movie.title if verified_movie else None,
                verified_year=verified_movie.year if verified_movie else None,
                radarr_status_snapshot=radarr.to_dict() if radarr else None,
                allow_downgrade_on_conflict=effective_genuine_conflict,
            )
            return

        # Case 5b (Unresolved path): Card changed while evaluation evidence is unchanged
        if can_reuse_prev_eval:
            card_payload["preserve_eval_reply"] = True
            await asyncio.to_thread(
                self.db.renew_lease,
                release_id,
                worker_id=self.worker_id,
                lease_seconds=self.config.worker_lease_seconds,
                new_state="delivering",
            )
            card_resp = await self.actions_client.deliver_card(card_payload)
            if _is_ambiguous_card_ack(card_resp):
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="ambiguous_delivery",
                    expected_incoming_digest=expected_digest,
                    full_evidence_digest=full_digest,
                    tl_outcome=tl_result.outcome,
                    identity_gate_status=final_gate.status,
                    identity_conflict_reason=final_gate.conflict_reason,
                    verified_tmdb_id=verified_movie.tmdb_id if verified_movie else None,
                    verified_imdb_id=verified_movie.imdb_id if verified_movie else None,
                    verified_title=verified_movie.title if verified_movie else None,
                    verified_year=verified_movie.year if verified_movie else None,
                    radarr_status_snapshot=radarr.to_dict() if radarr else None,
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
                    tl_outcome=tl_result.outcome,
                    identity_gate_status=final_gate.status,
                    identity_conflict_reason=final_gate.conflict_reason,
                    verified_tmdb_id=verified_movie.tmdb_id if verified_movie else None,
                    verified_imdb_id=verified_movie.imdb_id if verified_movie else None,
                    verified_title=verified_movie.title if verified_movie else None,
                    verified_year=verified_movie.year if verified_movie else None,
                    radarr_status_snapshot=radarr.to_dict() if radarr else None,
                    next_attempt_at=self._retry_next_attempt_iso(),
                    increment_retry=True,
                    allow_downgrade_on_conflict=effective_genuine_conflict,
                )
                return
            if card_resp.eval_reply_preserved:
                await asyncio.to_thread(
                    self.db.finalize_release,
                    release_id,
                    worker_id=self.worker_id,
                    state="completed",
                    expected_incoming_digest=expected_digest,
                    card_revision=next_card_rev,
                    full_evidence_digest=full_digest,
                    last_card_digest=candidate_card_digest,
                    last_eval_reply_digest=prev_last_eval_reply_digest,
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
                    eval_reply_status=prev_eval_reply_status,
                    pending_card_supersede=None,
                    pending_eval_reply=None,
                    radarr_status_snapshot=radarr.to_dict() if radarr else None,
                    allow_downgrade_on_conflict=effective_genuine_conflict,
                )
                return
            # Safety fallback if Actions did not have a sent eval reply to preserve
            reply_body = {
                "release_id": release_id,
                "card_revision": next_card_rev,
                "ack_chat_id": card_resp.ack_chat_id,
                "ack_message_id": card_resp.ack_message_id,
                "ack_thread_id": card_resp.ack_thread_id,
                "evaluation": eval_res.payload if eval_res.ok else None,
                "error_redacted": None if eval_res.ok else eval_res.error_redacted,
            }
            await asyncio.to_thread(
                self.db.record_card_ack,
                release_id,
                worker_id=self.worker_id,
                card_revision=next_card_rev,
                ack_chat_id=card_resp.ack_chat_id,
                ack_message_id=card_resp.ack_message_id,
                ack_thread_id=card_resp.ack_thread_id,
                ack_incoming_digest=expected_digest,
                last_card_digest=candidate_card_digest,
                pending_eval_reply=reply_body,
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
            )
            reply_resp = await self.actions_client.deliver_evaluation(reply_body)
        elif has_unsent_eval_for_ack and card_unchanged:
            next_card_rev = prev_card_revision
            card_resp = DeliveryCardResponse(
                status="delivered",
                ack_chat_id=row.get("ack_chat_id"),
                ack_message_id=int(row["ack_message_id"]),
                ack_thread_id=row.get("ack_thread_id"),
            )
            reply_body = {
                "release_id": release_id,
                "card_revision": next_card_rev,
                "ack_chat_id": card_resp.ack_chat_id,
                "ack_message_id": card_resp.ack_message_id,
                "ack_thread_id": card_resp.ack_thread_id,
                "evaluation": eval_res.payload if eval_res.ok else None,
                "error_redacted": None if eval_res.ok else eval_res.error_redacted,
            }
            await asyncio.to_thread(
                self.db.renew_lease,
                release_id,
                worker_id=self.worker_id,
                lease_seconds=self.config.worker_lease_seconds,
                new_state="delivering",
            )
            await asyncio.to_thread(
                self.db.record_card_ack,
                release_id,
                worker_id=self.worker_id,
                card_revision=next_card_rev,
                ack_chat_id=card_resp.ack_chat_id,
                ack_message_id=card_resp.ack_message_id,
                ack_thread_id=card_resp.ack_thread_id,
                ack_incoming_digest=expected_digest,
                last_card_digest=candidate_card_digest,
                pending_eval_reply=reply_body,
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
            )
            reply_resp = await self.actions_client.deliver_evaluation(reply_body)
        else:
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
        if _is_ambiguous_card_ack(card_resp):
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
                last_card_digest=candidate_card_digest,
                last_eval_reply_digest=None,
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
            last_card_digest=candidate_card_digest,
            last_eval_reply_digest=full_digest if eval_res.ok else None,
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
