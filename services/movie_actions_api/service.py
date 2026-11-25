from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from .clients import (
    IntakePushClient,
    RadarrClient,
    TelegramClient,
    TriageFeedbackClient,
)
from .config import QUALITY_ONLY_REJECTION_CLASSES, ActionsConfig, AlternativeGrabPolicy
from .db import (
    ActionGroupSnapshot,
    ActionsDB,
    NewActionRecord,
    generate_uuid7,
    parse_iso_utc,
    utc_now,
    utc_now_iso,
)
from .html_views import render_confirmation_page, render_simple_status_page
from .security import hash_token, mint_opaque_token, redact_secrets
from .telegram_format import (
    TELEGRAM_CAPTION_UTF16_LIMIT,
    TELEGRAM_TEXT_UTF16_LIMIT,
    build_card_html,
    build_inline_keyboard,
    format_evaluation_reply,
    safe_poster_url,
)

logger = logging.getLogger(__name__)

_NORM_SPACE_RE = re.compile(r"[\s._-]+")


class DeliveryValidationError(ValueError):
    """Raised on invalid / defense-in-depth-rejected card delivery payloads (HTTP 400)."""


class DeliveryPreconditionError(RuntimeError):
    """Raised when evaluation reply cannot be anchored to a verified delivery receipt (HTTP 409)."""


@dataclass(frozen=True)
class HtmlResponseData:
    status_code: int
    html: str


def _normalize_release_match_key(name: Optional[str]) -> str:
    if not name:
        return ""
    cleaned = _NORM_SPACE_RE.sub(" ", str(name).strip().lower()).strip()
    return cleaned


def _extract_existing_quality(movie: dict[str, Any]) -> Optional[str]:
    mf = movie.get("movieFile")
    if not isinstance(mf, dict):
        return None
    q = mf.get("quality")
    if isinstance(q, dict):
        inner = q.get("quality")
        if isinstance(inner, dict) and isinstance(inner.get("name"), str) and inner["name"].strip():
            return inner["name"].strip()
        if isinstance(q.get("name"), str) and q["name"].strip():
            return q["name"].strip()
    return None


def _extract_file_id(movie: dict[str, Any]) -> Optional[int]:
    mf = movie.get("movieFile")
    if isinstance(mf, dict):
        fid = mf.get("id")
        if isinstance(fid, int) and not isinstance(fid, bool) and fid > 0:
            return fid
    fid2 = movie.get("movieFileId")
    if isinstance(fid2, int) and not isinstance(fid2, bool) and fid2 > 0:
        return fid2
    return None


def _find_watched_tag_id(tags: list[dict[str, Any]]) -> Optional[int]:
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        label = str(tag.get("label") or "").strip().lower()
        tid = tag.get("id")
        if label == "watched" and isinstance(tid, int) and not isinstance(tid, bool):
            return tid
    return None


def _feedback_spec_for_action(action_type: str) -> Optional[tuple[str, str]]:
    """Return (decision, note) for action types that post to movie-triage-api /feedback."""
    if action_type in ("add_grab", "alternative_grab"):
        return ("watch", "Grabbed via Telegram")
    if action_type == "watched_good":
        return ("watch", "watched")
    if action_type == "watched_bad":
        return ("skip", "watched")
    if action_type == "skip":
        return ("skip", "Skipped via Telegram")
    # watched_legacy_unrated sends NO feedback
    return None


class ActionsService:
    """Authoritative coordinator for Radarr reads/writes, Telegram deliveries, actions, and feedback."""

    def __init__(
        self,
        *,
        config: ActionsConfig,
        db: ActionsDB,
        process_instance_id: str,
        radarr_client: RadarrClient,
        telegram_client: TelegramClient,
        intake_push_client: IntakePushClient,
        triage_feedback_client: TriageFeedbackClient,
        alt_grab_policy: AlternativeGrabPolicy,
    ) -> None:
        self.config = config
        self.db = db
        self.process_instance_id = process_instance_id
        self.radarr = radarr_client
        self.telegram = telegram_client
        self.intake_push = intake_push_client
        self.triage_feedback = triage_feedback_client
        self.alt_grab_policy = alt_grab_policy

    def _lease_iso(self, upstream_timeout: float) -> str:
        seconds = upstream_timeout + self.config.write_lease_grace_seconds
        return utc_now_iso(utc_now() + timedelta(seconds=seconds))

    # =========================================================================
    # 1. Read-only Radarr Pre-Delivery Status Check (GET /internal/radarr/status)
    # =========================================================================

    async def get_radarr_status(
        self,
        *,
        tmdb_id: Optional[int],
        imdb_id: Optional[str],
        release_title: Optional[str] = None,
    ) -> dict[str, Any]:
        clean_imdb = imdb_id.strip().lower() if imdb_id and imdb_id.strip() else None
        if tmdb_id is None and not clean_imdb:
            return {"status": "ambiguous", "detail": "missing_movie_identifiers"}

        tags_res = await self.radarr.get_tags()
        if not tags_res.ok:
            return {"status": "lookup_failed", "detail": tags_res.error_redacted or "radarr_tags_failed"}
        watched_tag_id = _find_watched_tag_id(tags_res.data)

        matched_movie: Optional[dict[str, Any]] = None
        if tmdb_id is not None:
            m_res = await self.radarr.get_movies_by_tmdb(tmdb_id)
            if not m_res.ok:
                return {"status": "lookup_failed", "detail": m_res.error_redacted or "radarr_tmdb_lookup_failed"}
            movies = [m for m in m_res.data if isinstance(m, dict)]
            if len(movies) > 1:
                return {"status": "ambiguous", "detail": "multiple_radarr_movies_for_tmdb_id"}
            if len(movies) == 1:
                candidate = movies[0]
                cand_imdb = str(candidate.get("imdbId") or "").strip().lower()
                if clean_imdb and cand_imdb and cand_imdb != clean_imdb:
                    return {"status": "ambiguous", "detail": "radarr_imdb_id_mismatch"}
                matched_movie = candidate
            elif clean_imdb:
                # Cross-check imdbId before declaring absence so conflicting entries are caught
                i_res = await self.radarr.get_movies_by_imdb(clean_imdb)
                if not i_res.ok:
                    return {"status": "lookup_failed", "detail": i_res.error_redacted or "radarr_imdb_lookup_failed"}
                i_movies = [m for m in i_res.data if isinstance(m, dict)]
                if len(i_movies) > 1:
                    return {"status": "ambiguous", "detail": "multiple_radarr_movies_for_imdb_id"}
                if len(i_movies) == 1:
                    cand = i_movies[0]
                    cand_tmdb = cand.get("tmdbId")
                    if isinstance(cand_tmdb, int) and not isinstance(cand_tmdb, bool) and cand_tmdb != tmdb_id:
                        return {"status": "ambiguous", "detail": "radarr_tmdb_id_mismatch_on_imdb_match"}
                    matched_movie = cand
        elif clean_imdb:
            i_res = await self.radarr.get_movies_by_imdb(clean_imdb)
            if not i_res.ok:
                return {"status": "lookup_failed", "detail": i_res.error_redacted or "radarr_imdb_lookup_failed"}
            i_movies = [m for m in i_res.data if isinstance(m, dict)]
            if len(i_movies) > 1:
                return {"status": "ambiguous", "detail": "multiple_radarr_movies_for_imdb_id"}
            if len(i_movies) == 1:
                matched_movie = i_movies[0]

        if matched_movie is None:
            return {"status": "absent", "movie_id": None, "file_id": None, "existing_quality": None, "detail": None}

        movie_id = matched_movie.get("id")
        if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id <= 0:
            return {"status": "ambiguous", "detail": "radarr_movie_missing_valid_id"}

        raw_tags = matched_movie.get("tags")
        movie_tags = [t for t in raw_tags if isinstance(t, int) and not isinstance(t, bool)] if isinstance(raw_tags, list) else []
        has_watched = watched_tag_id is not None and watched_tag_id in movie_tags
        monitored = bool(matched_movie.get("monitored", False))
        has_file = bool(matched_movie.get("hasFile", False))
        file_id = _extract_file_id(matched_movie)
        existing_quality = _extract_existing_quality(matched_movie)

        if has_watched and not monitored:
            return {
                "status": "watched_unmonitored",
                "movie_id": movie_id,
                "file_id": file_id,
                "existing_quality": existing_quality,
                "detail": "watched_tag_unmonitored",
            }
        if has_file:
            return {
                "status": "owned_file",
                "movie_id": movie_id,
                "file_id": file_id,
                "existing_quality": existing_quality,
                "detail": "has_file",
            }
        if monitored:
            return {
                "status": "monitored_no_file",
                "movie_id": movie_id,
                "file_id": None,
                "existing_quality": None,
                "detail": "monitored_without_file",
            }
        return {
            "status": "absent",
            "movie_id": movie_id,
            "file_id": None,
            "existing_quality": None,
            "detail": "existing_unmonitored_no_file_unwatched",
        }

    # =========================================================================
    # 2. Telegram Card & Bound Evaluation Reply Delivery
    # =========================================================================

    def _validate_and_mint_actions(
        self,
        card_payload: dict[str, Any],
    ) -> tuple[list[NewActionRecord], dict[str, str]]:
        delivery_kind = str(card_payload.get("delivery_kind") or "").strip()
        if delivery_kind not in ("actionable_grab", "alternative_grab", "evidence_review"):
            raise DeliveryValidationError(f"Unsupported delivery_kind: {delivery_kind!r}")

        release_id = str(card_payload.get("release_id") or "").strip()
        source_occurrence_id = str(card_payload.get("source_occurrence_id") or "").strip()
        exact_release_name = str(card_payload.get("exact_release_name") or "").strip()
        card_revision = card_payload.get("card_revision")
        if (
            not release_id
            or not source_occurrence_id
            or not exact_release_name
            or not isinstance(card_revision, int)
            or isinstance(card_revision, bool)
            or card_revision < 1
        ):
            raise DeliveryValidationError("Missing or invalid release_id / source_occurrence_id / card_revision")

        if delivery_kind == "evidence_review":
            return [], {}

        # Defense-in-depth gate: NEVER mint ordinary or alternative buttons without verified identity
        gate_status = str(card_payload.get("identity_gate_status") or "").strip().lower()
        verified_tmdb = card_payload.get("verified_tmdb_id")
        verified_imdb = card_payload.get("verified_imdb_id")
        verified_title = str(card_payload.get("verified_title") or "").strip()
        verified_year = card_payload.get("verified_year")
        conflict_reason = str(card_payload.get("conflict_reason") or "").strip()

        if gate_status != "verified":
            raise DeliveryValidationError("Refused: actionable links require identity_gate_status == 'verified'")
        if not isinstance(verified_tmdb, int) or isinstance(verified_tmdb, bool) or verified_tmdb <= 0:
            raise DeliveryValidationError("Refused: actionable links require verified positive integer verified_tmdb_id")
        if not verified_title:
            raise DeliveryValidationError("Refused: actionable links require verified_title")
        if conflict_reason:
            raise DeliveryValidationError("Refused: cannot mint actionable links when conflict_reason is present")
        if not self.config.public_base_url:
            raise DeliveryValidationError("MOVIE_INTEL_PUBLIC_BASE_URL must be configured to mint action links")

        bound_existing_movie_id: Optional[int] = None
        bound_existing_file_id: Optional[int] = None
        bound_existing_file_quality: Optional[str] = None
        bound_rejection_class: Optional[str] = None

        if delivery_kind == "alternative_grab":
            bound_existing_movie_id = card_payload.get("bound_existing_movie_id")
            bound_existing_file_id = card_payload.get("bound_existing_file_id")
            bound_rejection_class = str(card_payload.get("bound_rejection_class") or "").strip().lower()
            raw_q = card_payload.get("bound_existing_file_quality")
            bound_existing_file_quality = str(raw_q).strip() if isinstance(raw_q, str) and raw_q.strip() else None

            if not self.alt_grab_policy.enabled:
                raise DeliveryValidationError("Refused: Alternative Grab policy is disabled")
            if (
                bound_rejection_class not in QUALITY_ONLY_REJECTION_CLASSES
                or bound_rejection_class not in self.alt_grab_policy.allowed_rejection_classes
            ):
                raise DeliveryValidationError("Refused: bound_rejection_class is not allowed by Alternative Grab policy")
            if (
                not isinstance(bound_existing_movie_id, int)
                or isinstance(bound_existing_movie_id, bool)
                or bound_existing_movie_id <= 0
                or not isinstance(bound_existing_file_id, int)
                or isinstance(bound_existing_file_id, bool)
                or bound_existing_file_id <= 0
            ):
                raise DeliveryValidationError(
                    "Refused: alternative_grab requires positive bound_existing_movie_id and bound_existing_file_id"
                )

        expires_at = utc_now_iso(utc_now() + timedelta(seconds=self.config.action_ttl_seconds))
        action_types_to_mint: list[str] = []
        if delivery_kind == "actionable_grab":
            action_types_to_mint.append("add_grab")
        else:
            action_types_to_mint.append("alternative_grab")
        action_types_to_mint.extend(["watched_good", "watched_bad"])
        if self.config.standalone_skip_enabled:
            action_types_to_mint.append("skip")

        records: list[NewActionRecord] = []
        raw_tokens: dict[str, str] = {}
        clean_imdb = (
            str(verified_imdb).strip().lower()
            if isinstance(verified_imdb, str) and verified_imdb.strip()
            else None
        )
        clean_year = (
            verified_year
            if isinstance(verified_year, int) and not isinstance(verified_year, bool)
            else None
        )

        for act_type in action_types_to_mint:
            raw_tok = mint_opaque_token()
            raw_tokens[act_type] = raw_tok
            if act_type in ("watched_good", "watched_bad", "watched_legacy_unrated"):
                comp_group = f"watched:{release_id}:{verified_tmdb}"
            elif act_type == "add_grab":
                comp_group = f"grab:{release_id}:{verified_tmdb}"
            elif act_type == "alternative_grab":
                comp_group = f"alt_grab:{release_id}:{verified_tmdb}"
            else:
                comp_group = f"skip:{release_id}:{verified_tmdb}"

            records.append(
                NewActionRecord(
                    action_id=generate_uuid7(),
                    token_sha256=hash_token(raw_tok),
                    release_id=release_id,
                    card_revision=card_revision,
                    source_occurrence_id=source_occurrence_id,
                    action_type=act_type,
                    completion_group=comp_group,
                    bound_tmdb_id=verified_tmdb,
                    bound_imdb_id=clean_imdb,
                    bound_title=verified_title,
                    bound_year=clean_year,
                    bound_release_name=exact_release_name,
                    bound_existing_movie_id=bound_existing_movie_id if act_type == "alternative_grab" else None,
                    bound_existing_file_id=bound_existing_file_id if act_type == "alternative_grab" else None,
                    bound_existing_file_quality=bound_existing_file_quality if act_type == "alternative_grab" else None,
                    bound_rejection_class=bound_rejection_class if act_type == "alternative_grab" else None,
                    expires_at=expires_at,
                )
            )
        return records, raw_tokens

    async def deliver_card(self, card_payload: dict[str, Any]) -> dict[str, Any]:
        new_actions, raw_tokens = self._validate_and_mint_actions(card_payload)

        release_id = str(card_payload["release_id"]).strip()
        card_revision = int(card_payload["card_revision"])
        delivery_kind = str(card_payload["delivery_kind"]).strip()
        target_chat_id = self.config.telegram_chat_id
        if not target_chat_id:
            return {"status": "failed", "detail": "telegram_chat_id_not_configured"}
        target_thread_id = self.config.topic_for_delivery_kind(delivery_kind)

        plan = await asyncio.to_thread(
            self.db.begin_card_delivery,
            release_id=release_id,
            card_revision=card_revision,
            delivery_kind=delivery_kind,
            target_chat_id=target_chat_id,
            target_thread_id=target_thread_id,
            process_instance_id=self.process_instance_id,
            lease_expires_at=self._lease_iso(self.config.telegram_timeout_seconds),
            new_actions=new_actions,
        )

        if plan.mode in ("already_delivered", "locked_completed_radarr"):
            return {
                "status": "delivered",
                "ack_chat_id": plan.ack_chat_id,
                "ack_message_id": plan.ack_message_id,
                "ack_thread_id": plan.ack_thread_id,
                "detail": plan.detail or plan.mode,
            }
        if plan.mode in ("ambiguous", "in_flight"):
            return {
                "status": "ambiguous_delivery",
                "detail": plan.detail or plan.mode,
            }

        reply_markup = build_inline_keyboard(
            delivery_kind=delivery_kind,
            public_base_url=self.config.public_base_url,
            action_tokens=raw_tokens,
        )

        # Path 1: Edit an existing acknowledged card on a higher verified revision / conflict downgrade
        if plan.mode == "edit":
            assert plan.ack_chat_id is not None and plan.ack_message_id is not None
            if plan.used_poster:
                caption_html = build_card_html(card_payload, max_utf16_units=TELEGRAM_CAPTION_UTF16_LIMIT)
                res = await self.telegram.edit_message_caption(
                    chat_id=plan.ack_chat_id,
                    message_id=plan.ack_message_id,
                    caption_html=caption_html,
                    reply_markup=reply_markup,
                )
            else:
                text_html = build_card_html(card_payload, max_utf16_units=TELEGRAM_TEXT_UTF16_LIMIT)
                res = await self.telegram.edit_message_text(
                    chat_id=plan.ack_chat_id,
                    message_id=plan.ack_message_id,
                    text_html=text_html,
                    reply_markup=reply_markup,
                )

            if res.outcome in ("ok", "not_modified"):
                await asyncio.to_thread(
                    self.db.finish_card_delivery_success,
                    release_id,
                    card_revision=card_revision,
                    delivery_kind=delivery_kind,
                    ack_chat_id=plan.ack_chat_id,
                    ack_message_id=plan.ack_message_id,
                    ack_thread_id=plan.ack_thread_id,
                    used_poster=plan.used_poster,
                    is_edit=True,
                )
                return {
                    "status": "delivered",
                    "ack_chat_id": plan.ack_chat_id,
                    "ack_message_id": plan.ack_message_id,
                    "ack_thread_id": plan.ack_thread_id,
                }
            if res.outcome == "failed_definitive":
                await asyncio.to_thread(
                    self.db.finish_card_delivery_failed,
                    release_id,
                    error_redacted=res.error_redacted or "telegram_edit_failed",
                    is_edit=True,
                    restore_card_revision=plan.previous_card_revision,
                    restore_delivery_kind=plan.previous_delivery_kind,
                    superseded_action_ids=plan.superseded_action_ids,
                    newly_minted_action_ids=plan.newly_minted_action_ids,
                    keep_prior_superseded=(delivery_kind == "evidence_review"),
                )
                return {"status": "failed", "detail": res.error_redacted or "telegram_edit_failed"}

            await asyncio.to_thread(
                self.db.finish_card_delivery_ambiguous,
                release_id,
                error_redacted=res.error_redacted or "telegram_edit_ambiguous",
                is_edit=True,
            )
            return {"status": "ambiguous_delivery", "detail": res.error_redacted or "telegram_edit_ambiguous"}

        # Path 2: Fresh initial send (poster with automatic fallback to text preserving exact buttons)
        poster_url = safe_poster_url(card_payload.get("poster_path"))
        if poster_url:
            caption_html = build_card_html(card_payload, max_utf16_units=TELEGRAM_CAPTION_UTF16_LIMIT)
            photo_res = await self.telegram.send_photo(
                chat_id=target_chat_id,
                photo_url=poster_url,
                caption_html=caption_html,
                reply_markup=reply_markup,
                thread_id=target_thread_id,
            )
            if photo_res.outcome == "ok" and photo_res.ack_chat_id and photo_res.ack_message_id:
                final_thread = photo_res.ack_thread_id if photo_res.ack_thread_id is not None else target_thread_id
                await asyncio.to_thread(
                    self.db.finish_card_delivery_success,
                    release_id,
                    card_revision=card_revision,
                    delivery_kind=delivery_kind,
                    ack_chat_id=photo_res.ack_chat_id,
                    ack_message_id=photo_res.ack_message_id,
                    ack_thread_id=final_thread,
                    used_poster=True,
                    is_edit=False,
                )
                return {
                    "status": "delivered",
                    "ack_chat_id": photo_res.ack_chat_id,
                    "ack_message_id": photo_res.ack_message_id,
                    "ack_thread_id": final_thread,
                }
            if photo_res.outcome == "ambiguous":
                # NEVER fall back to sendMessage when sendPhoto timed out / lost ack!
                await asyncio.to_thread(
                    self.db.finish_card_delivery_ambiguous,
                    release_id,
                    error_redacted=photo_res.error_redacted or "telegram_send_photo_ambiguous",
                    is_edit=False,
                )
                return {
                    "status": "ambiguous_delivery",
                    "detail": photo_res.error_redacted or "telegram_send_photo_ambiguous",
                }
            # Definitive sendPhoto failure (e.g. HTTP 400 invalid image/caption) -> fall back to sendMessage
            logger.info("Poster sendPhoto failed definitively for release=%s; falling back to text card", release_id)

        text_html = build_card_html(card_payload, max_utf16_units=TELEGRAM_TEXT_UTF16_LIMIT)
        msg_res = await self.telegram.send_message(
            chat_id=target_chat_id,
            text_html=text_html,
            reply_markup=reply_markup,
            thread_id=target_thread_id,
        )
        if msg_res.outcome == "ok" and msg_res.ack_chat_id and msg_res.ack_message_id:
            final_thread = msg_res.ack_thread_id if msg_res.ack_thread_id is not None else target_thread_id
            await asyncio.to_thread(
                self.db.finish_card_delivery_success,
                release_id,
                card_revision=card_revision,
                delivery_kind=delivery_kind,
                ack_chat_id=msg_res.ack_chat_id,
                ack_message_id=msg_res.ack_message_id,
                ack_thread_id=final_thread,
                used_poster=False,
                is_edit=False,
            )
            return {
                "status": "delivered",
                "ack_chat_id": msg_res.ack_chat_id,
                "ack_message_id": msg_res.ack_message_id,
                "ack_thread_id": final_thread,
            }
        if msg_res.outcome == "failed_definitive":
            await asyncio.to_thread(
                self.db.finish_card_delivery_failed,
                release_id,
                error_redacted=msg_res.error_redacted or "telegram_send_message_failed",
                is_edit=False,
                restore_card_revision=0,
                restore_delivery_kind="",
                superseded_action_ids=(),
                newly_minted_action_ids=plan.newly_minted_action_ids,
                keep_prior_superseded=True,
            )
            return {"status": "failed", "detail": msg_res.error_redacted or "telegram_send_message_failed"}

        await asyncio.to_thread(
            self.db.finish_card_delivery_ambiguous,
            release_id,
            error_redacted=msg_res.error_redacted or "telegram_send_message_ambiguous",
            is_edit=False,
        )
        return {"status": "ambiguous_delivery", "detail": msg_res.error_redacted or "telegram_send_message_ambiguous"}

    async def deliver_evaluation(self, eval_payload: dict[str, Any]) -> dict[str, Any]:
        release_id = str(eval_payload.get("release_id") or "").strip()
        card_revision = eval_payload.get("card_revision")
        if (
            not release_id
            or not isinstance(card_revision, int)
            or isinstance(card_revision, bool)
            or card_revision < 1
        ):
            raise DeliveryValidationError("Missing or invalid release_id / card_revision")

        expected_chat = eval_payload.get("ack_chat_id")
        expected_msg = eval_payload.get("ack_message_id")
        plan = await asyncio.to_thread(
            self.db.begin_eval_reply,
            release_id=release_id,
            card_revision=card_revision,
            expected_ack_chat_id=str(expected_chat) if expected_chat is not None else None,
            expected_ack_message_id=int(expected_msg) if isinstance(expected_msg, int) and not isinstance(expected_msg, bool) else None,
            process_instance_id=self.process_instance_id,
            lease_expires_at=self._lease_iso(self.config.telegram_timeout_seconds),
        )
        if plan.mode in ("reject_missing", "reject_unanchored", "reject_receipt_mismatch"):
            raise DeliveryPreconditionError(plan.detail or plan.mode)
        if plan.mode == "already_sent":
            return {"status": "sent", "detail": "already_sent"}
        if plan.mode in ("ambiguous", "in_flight"):
            return {"status": "ambiguous_reply", "detail": plan.detail or plan.mode}

        assert plan.ack_chat_id is not None and plan.ack_message_id is not None
        formatted = format_evaluation_reply(
            release_id=release_id,
            evaluation=eval_payload.get("evaluation") if isinstance(eval_payload.get("evaluation"), dict) else None,
            error_redacted=str(eval_payload.get("error_redacted") or "") or None,
        )

        reply_msg_id = plan.existing_eval_reply_message_id
        if reply_msg_id is None:
            msg_res = await self.telegram.send_message(
                chat_id=plan.ack_chat_id,
                text_html=formatted.html_text,
                thread_id=plan.ack_thread_id,
                reply_to_message_id=plan.ack_message_id,
            )
            if msg_res.outcome == "failed_definitive":
                await asyncio.to_thread(
                    self.db.finish_eval_reply,
                    release_id,
                    status="failed",
                    error_redacted=msg_res.error_redacted or "telegram_reply_failed",
                )
                return {"status": "failed", "detail": msg_res.error_redacted}
            if msg_res.outcome != "ok" or not msg_res.ack_message_id:
                await asyncio.to_thread(
                    self.db.finish_eval_reply,
                    release_id,
                    status="ambiguous_reply",
                    error_redacted=msg_res.error_redacted or "telegram_reply_ambiguous",
                )
                return {"status": "ambiguous_reply", "detail": msg_res.error_redacted}
            reply_msg_id = msg_res.ack_message_id
            if formatted.attach_txt:
                await asyncio.to_thread(
                    self.db.record_eval_reply_text_sent,
                    release_id,
                    reply_message_id=reply_msg_id,
                )

        doc_msg_id: Optional[int] = None
        if formatted.attach_txt and formatted.txt_content and formatted.txt_filename:
            doc_res = await self.telegram.send_document(
                chat_id=plan.ack_chat_id,
                filename=formatted.txt_filename,
                content_bytes=formatted.txt_content.encode("utf-8"),
                caption="Full evaluation report",
                thread_id=plan.ack_thread_id,
                reply_to_message_id=plan.ack_message_id,
            )
            if doc_res.outcome == "failed_definitive":
                await asyncio.to_thread(
                    self.db.finish_eval_reply,
                    release_id,
                    status="failed",
                    reply_message_id=reply_msg_id,
                    error_redacted=doc_res.error_redacted or "telegram_doc_failed",
                )
                return {"status": "failed", "detail": doc_res.error_redacted}
            if doc_res.outcome != "ok" or not doc_res.ack_message_id:
                await asyncio.to_thread(
                    self.db.finish_eval_reply,
                    release_id,
                    status="ambiguous_reply",
                    reply_message_id=reply_msg_id,
                    error_redacted=doc_res.error_redacted or "telegram_doc_ambiguous",
                )
                return {"status": "ambiguous_reply", "detail": doc_res.error_redacted}
            doc_msg_id = doc_res.ack_message_id

        await asyncio.to_thread(
            self.db.finish_eval_reply,
            release_id,
            status="sent",
            reply_message_id=reply_msg_id,
            doc_message_id=doc_msg_id,
        )
        return {"status": "sent", "reply_message_id": reply_msg_id, "doc_message_id": doc_msg_id}

    # =========================================================================
    # 3. Private Action Confirmation (GET /action/{token} — strictly read-only)
    # =========================================================================

    def _is_expired(self, action: dict[str, Any]) -> bool:
        exp = parse_iso_utc(action.get("expires_at"))
        if exp is None:
            return True
        return utc_now() > exp

    def _classify_group_state_readonly(
        self,
        snap: ActionGroupSnapshot,
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], bool]:
        """Return (completed_action, ambiguous_action, has_live_attempting) without mutating DB."""
        now_iso = utc_now_iso()
        completed_act = next(
            (a for a in snap.group_actions if a["status"] in ("completed_all", "completed_radarr")),
            None,
        )
        ambiguous_act = next(
            (a for a in snap.group_actions if a["status"] == "ambiguous_radarr"),
            None,
        )
        has_live_attempting = False
        for att in snap.group_attempts:
            if att["status"] == "attempting":
                is_live = (
                    att["process_instance_id"] == self.process_instance_id
                    and str(att["lease_expires_at"]) > now_iso
                )
                if is_live:
                    has_live_attempting = True
                elif att["phase"] != "evaluator_feedback" and ambiguous_act is None:
                    # Orphaned / stale-lease attempt will be promoted on POST into reconciliation
                    ambiguous_act = next(
                        (a for a in snap.group_actions if a["action_id"] == att["action_id"]),
                        snap.action,
                    )
        return completed_act, ambiguous_act, has_live_attempting

    def _render_completion_html(self, done_act: dict[str, Any], clicked_act: dict[str, Any]) -> HtmlResponseData:
        act_type = str(done_act["action_type"])
        fb_status = str(done_act.get("feedback_status") or "not_applicable")
        fb_saved = done_act.get("feedback_decision_saved")
        same_action = done_act["action_id"] == clicked_act["action_id"]

        details = [
            ("Movie", f"{done_act['bound_title']} ({done_act['bound_year']})" if done_act.get("bound_year") else str(done_act["bound_title"])),
            ("Completed Action", act_type),
            ("Status", str(done_act["status"])),
        ]
        if done_act.get("radarr_movie_id"):
            details.append(("Radarr Movie ID", str(done_act["radarr_movie_id"])))

        if act_type == "skip":
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Skip Recorded",
                    badge="completed_all",
                    message=f"Evaluator feedback saved ({fb_saved or 'SKIP'}). No Radarr changes were made.",
                    tone="ok",
                    details=details,
                ),
            )

        if act_type == "watched_legacy_unrated":
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Marked as Watched in Radarr",
                    badge="completed_all",
                    message="Radarr movie is tagged 'watched' and unmonitored. Legacy unrated link sends no evaluator rating.",
                    tone="ok",
                    details=details,
                ),
            )

        if fb_status == "saved" and fb_saved:
            msg = f"Radarr action completed and evaluator rating saved ({fb_saved})."
            if not same_action:
                msg = f"This watched group was already completed via '{act_type}' and evaluator rating saved ({fb_saved}). No duplicate Radarr write was made."
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Action Completed",
                    badge="completed_all",
                    message=msg,
                    tone="ok",
                    details=details,
                ),
            )

        # Radarr succeeded & permanently sealed, but evaluator feedback did not save
        warn_msg = (
            "Radarr action succeeded and is permanently sealed (will not be repeated or undone), "
            "but evaluator rating was NOT saved."
        )
        if done_act.get("last_error_redacted"):
            details.append(("Feedback Error", str(done_act["last_error_redacted"])))
        return HtmlResponseData(
            status_code=200,
            html=render_simple_status_page(
                title="Radarr Completed (Feedback Pending)",
                badge="completed_radarr",
                message=warn_msg,
                tone="warn",
                details=details,
                post_button_label="Retry Evaluator Feedback Only",
                hidden_fields={"confirm": "1", "retry_feedback_only": "1"},
            ),
        )

    async def _validate_alternative_grab_live(
        self,
        action: dict[str, Any],
    ) -> tuple[bool, Optional[dict[str, Any]], int, str]:
        """Revalidate exact movie/file/policy for alternative_grab. Returns (ok, movie, http_code, message)."""
        rej_class = str(action.get("bound_rejection_class") or "").strip().lower()
        if (
            not self.alt_grab_policy.enabled
            or rej_class not in QUALITY_ONLY_REJECTION_CLASSES
            or rej_class not in self.alt_grab_policy.allowed_rejection_classes
        ):
            return False, None, 409, "Alternative Grab policy no longer permits this rejection class."

        tmdb_id = action.get("bound_tmdb_id")
        exp_movie_id = action.get("bound_existing_movie_id")
        exp_file_id = action.get("bound_existing_file_id")
        if not (
            isinstance(tmdb_id, int)
            and isinstance(exp_movie_id, int)
            and isinstance(exp_file_id, int)
            and tmdb_id > 0
            and exp_movie_id > 0
            and exp_file_id > 0
        ):
            return False, None, 409, "Alternative Grab action lacks valid movie/file bindings."

        tags_res, movies_res = await asyncio.gather(
            self.radarr.get_tags(),
            self.radarr.get_movies_by_tmdb(tmdb_id),
        )
        if not tags_res.ok or not movies_res.ok:
            return False, None, 503, "Radarr is temporarily unreachable; cannot revalidate Alternative Grab."

        movies = [m for m in movies_res.data if isinstance(m, dict)]
        if len(movies) != 1:
            return False, None, 409, "Existing Radarr movie no longer matches uniquely; Alternative Grab refused."
        movie = movies[0]
        if movie.get("id") != exp_movie_id:
            return False, None, 409, "Bound Radarr movie ID changed; Alternative Grab refused."

        watched_tag_id = _find_watched_tag_id(tags_res.data)
        m_tags = movie.get("tags") if isinstance(movie.get("tags"), list) else []
        if watched_tag_id is not None and watched_tag_id in m_tags and not bool(movie.get("monitored", False)):
            return False, None, 409, "Movie is now marked watched and unmonitored; Alternative Grab refused."

        cur_file_id = _extract_file_id(movie)
        if not bool(movie.get("hasFile", False)) or cur_file_id != exp_file_id:
            return (
                False,
                None,
                409,
                "Existing movie file changed or was removed since card creation; Alternative Grab refused.",
            )
        return True, movie, 200, "Eligible for Alternative Grab"

    async def render_action_get(self, raw_token: str) -> HtmlResponseData:
        """Strictly read-only confirmation / status view for GET /action/{token}."""
        snap = await asyncio.to_thread(self.db.get_action_group_readonly, hash_token(raw_token))
        if snap.action is None:
            return HtmlResponseData(
                status_code=404,
                html=render_simple_status_page(
                    title="Action Not Found",
                    badge="404",
                    message="This action link is invalid or unknown.",
                    tone="err",
                ),
            )

        action = snap.action
        status = str(action["status"])
        if status == "superseded":
            return HtmlResponseData(
                status_code=410,
                html=render_simple_status_page(
                    title="Action Superseded",
                    badge="superseded",
                    message="This card was superseded by a newer revision. Use the updated card in Telegram.",
                    tone="warn",
                ),
            )
        if status == "expired":
            return HtmlResponseData(
                status_code=410,
                html=render_simple_status_page(
                    title="Action Expired",
                    badge="expired",
                    message="This action link has expired.",
                    tone="warn",
                ),
            )

        done_act, amb_act, has_live_attempting = self._classify_group_state_readonly(snap)
        if done_act is not None:
            return self._render_completion_html(done_act, action)

        if amb_act is not None:
            details = [
                ("Movie", str(amb_act["bound_title"])),
                ("Release", str(amb_act["bound_release_name"])),
                ("Action", str(amb_act["action_type"])),
            ]
            if amb_act.get("last_reconciled_at"):
                details.append(("Last Checked", str(amb_act["last_reconciled_at"])))
            if amb_act.get("last_error_redacted"):
                details.append(("Detail", str(amb_act["last_error_redacted"])))
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Radarr Outcome Unconfirmed (Reconciliation Required)",
                    badge="ambiguous_radarr",
                    message=(
                        "A previous Radarr request did not return a clear confirmation. "
                        "Click below to re-check live Radarr state safely without issuing duplicate release pushes."
                    ),
                    tone="warn",
                    details=details,
                    post_button_label="Re-check Radarr Status & Reconcile",
                    hidden_fields={"confirm": "1", "reconcile": "1"},
                ),
            )

        if has_live_attempting:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Action Already In Progress",
                    badge="attempting",
                    message="This action (or a sibling in the same group) is currently executing. Refresh in a few seconds.",
                    tone="warn",
                ),
            )

        if status == "rejected_push":
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Release Push Rejected by Radarr",
                    badge="rejected_push",
                    message=str(action.get("last_error_redacted") or "Radarr rejected this release push."),
                    tone="err",
                    details=[("Release", str(action["bound_release_name"]))],
                ),
            )

        # Check expiry for active links (except already-initiated skip with failed feedback)
        if self._is_expired(action):
            if action["action_type"] == "skip" and action.get("feedback_status") == "failed":
                return HtmlResponseData(
                    status_code=200,
                    html=render_simple_status_page(
                        title="Skip Feedback Pending Retry",
                        badge="skip",
                        message="Your prior Skip decision failed to reach the evaluator. Retry feedback only below.",
                        tone="warn",
                        post_button_label="Retry Evaluator Feedback Only",
                        hidden_fields={"confirm": "1", "retry_feedback_only": "1"},
                    ),
                )
            return HtmlResponseData(
                status_code=410,
                html=render_simple_status_page(
                    title="Action Expired",
                    badge="expired",
                    message="This action link has expired and can no longer initiate a new change.",
                    tone="warn",
                ),
            )

        action_type = str(action["action_type"])
        if action_type == "skip":
            return HtmlResponseData(
                status_code=200,
                html=render_confirmation_page(
                    action=action,
                    radarr_summary="No Radarr changes will be performed (evaluator feedback only).",
                    root_folders=[],
                    quality_profiles=[],
                    needs_add_options=False,
                ),
            )

        if action_type == "alternative_grab":
            ok, movie, code, msg = await self._validate_alternative_grab_live(action)
            if not ok or movie is None:
                return HtmlResponseData(
                    status_code=code,
                    html=render_simple_status_page(
                        title="Alternative Grab Unavailable",
                        badge="alternative_grab",
                        message=msg,
                        tone="err" if code == 409 else "warn",
                    ),
                )
            quality_label = _extract_existing_quality(movie) or "existing file"
            return HtmlResponseData(
                status_code=200,
                html=render_confirmation_page(
                    action=action,
                    radarr_summary=f"Existing movie ID {movie['id']} with file ID {action['bound_existing_file_id']} ({quality_label})",
                    root_folders=[],
                    quality_profiles=[],
                    needs_add_options=False,
                ),
            )

        # add_grab or watched_*
        tmdb_id = action.get("bound_tmdb_id")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Unverified Movie Binding",
                    badge="error",
                    message="This action is missing a verified TMDB ID.",
                    tone="err",
                ),
            )

        movies_res, roots_res, profiles_res, tags_res = await asyncio.gather(
            self.radarr.get_movies_by_tmdb(tmdb_id),
            self.radarr.get_root_folders(),
            self.radarr.get_quality_profiles(),
            self.radarr.get_tags(),
        )
        if not movies_res.ok or not roots_res.ok or not profiles_res.ok or not tags_res.ok:
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Radarr Unavailable",
                    badge="503",
                    message="Could not read current Radarr state. Please retry shortly.",
                    tone="warn",
                ),
            )

        movies = [m for m in movies_res.data if isinstance(m, dict)]
        if len(movies) > 1:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Ambiguous Radarr Match",
                    badge="ambiguous",
                    message="Multiple movies matched this TMDB ID in Radarr; refusing automatic mutation.",
                    tone="err",
                ),
            )

        existing = movies[0] if movies else None
        warning_note: Optional[str] = None
        if existing is not None:
            watched_tag_id = _find_watched_tag_id(tags_res.data)
            m_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
            has_watched = watched_tag_id is not None and watched_tag_id in m_tags
            radarr_summary = (
                f"Exists in Radarr (ID {existing.get('id')}, monitored={bool(existing.get('monitored'))}, "
                f"hasFile={bool(existing.get('hasFile'))}, watched={has_watched})"
            )
            if action_type == "add_grab" and has_watched and not bool(existing.get("monitored")):
                warning_note = "Note: This movie is currently tagged 'watched' and unmonitored in Radarr."
            needs_add_options = False
        else:
            radarr_summary = "Not currently in Radarr (will be added with searchForMovie=false)."
            needs_add_options = True

        return HtmlResponseData(
            status_code=200,
            html=render_confirmation_page(
                action=action,
                radarr_summary=radarr_summary,
                root_folders=roots_res.data if needs_add_options else [],
                quality_profiles=profiles_res.data if needs_add_options else [],
                needs_add_options=needs_add_options,
                warning_note=warning_note,
            ),
        )

    # =========================================================================
    # 4. Mutating Action Execution & Reconciliation (POST /action/{token})
    # =========================================================================

    async def handle_action_post(
        self,
        raw_token: str,
        form_data: dict[str, str],
    ) -> HtmlResponseData:
        if form_data.get("confirm") != "1":
            return HtmlResponseData(
                status_code=400,
                html=render_simple_status_page(
                    title="Confirmation Required",
                    badge="400",
                    message="Explicit confirmation form submission is required.",
                    tone="err",
                ),
            )

        token_hash = hash_token(raw_token)
        snap = await asyncio.to_thread(
            self.db.inspect_action_group_for_post,
            token_hash,
            current_process_instance_id=self.process_instance_id,
        )
        if snap.action is None:
            return HtmlResponseData(
                status_code=404,
                html=render_simple_status_page(
                    title="Action Not Found",
                    badge="404",
                    message="This action link is invalid or unknown.",
                    tone="err",
                ),
            )

        action = snap.action
        status = str(action["status"])
        if status == "superseded":
            return HtmlResponseData(
                status_code=410,
                html=render_simple_status_page(
                    title="Action Superseded",
                    badge="superseded",
                    message="This card was superseded by a newer revision.",
                    tone="warn",
                ),
            )
        if status == "expired":
            return HtmlResponseData(
                status_code=410,
                html=render_simple_status_page(
                    title="Action Expired",
                    badge="expired",
                    message="This action link has expired.",
                    tone="warn",
                ),
            )

        # 1. Already completed in completion_group -> ZERO Radarr writes!
        done_act = next(
            (a for a in snap.group_actions if a["status"] in ("completed_all", "completed_radarr")),
            None,
        )
        if done_act is not None:
            if (
                done_act["status"] == "completed_radarr"
                and done_act.get("feedback_status") in ("failed", "pending")
                and (form_data.get("retry_feedback_only") == "1" or done_act["action_id"] == action["action_id"])
            ):
                updated_done = await self._run_isolated_feedback(done_act)
                return self._render_completion_html(updated_done, action)
            return self._render_completion_html(done_act, action)

        # 2. Prior ambiguous_radarr in completion_group -> Reconciliation (§6.3)
        amb_act = next(
            (a for a in snap.group_actions if a["status"] == "ambiguous_radarr"),
            None,
        )
        if amb_act is not None:
            return await self._reconcile_ambiguous_action(amb_act, snap.group_attempts, form_data)

        # 3. Live in-flight attempt in completion_group -> 409 Conflict
        if any(att["status"] == "attempting" for att in snap.group_attempts):
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Action Already In Progress",
                    badge="409",
                    message="Another submission for this action group is currently in progress.",
                    tone="warn",
                ),
            )

        # 4. Previously rejected push -> idempotent rejection status (no blind re-push)
        if status == "rejected_push":
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Release Push Rejected by Radarr",
                    badge="rejected_push",
                    message=str(action.get("last_error_redacted") or "Radarr rejected this release push."),
                    tone="err",
                ),
            )

        # 5. Expiry check for fresh initiation (active status)
        if self._is_expired(action):
            if action["action_type"] == "skip" and action.get("feedback_status") == "failed":
                return await self._execute_skip(action)
            await asyncio.to_thread(self.db.mark_action_expired, action_id=str(action["action_id"]))
            return HtmlResponseData(
                status_code=410,
                html=render_simple_status_page(
                    title="Action Expired",
                    badge="expired",
                    message="This action link has expired and cannot initiate a new mutation.",
                    tone="warn",
                ),
            )

        action_type = str(action["action_type"])
        if action_type == "skip":
            return await self._execute_skip(action)
        if action_type in ("watched_good", "watched_bad", "watched_legacy_unrated"):
            return await self._execute_watched(action, form_data)
        if action_type == "add_grab":
            return await self._execute_add_grab(action, form_data)
        if action_type == "alternative_grab":
            return await self._execute_alternative_grab(action, form_data)

        return HtmlResponseData(
            status_code=400,
            html=render_simple_status_page(
                title="Unsupported Action",
                badge="400",
                message=f"Unknown action type: {action_type}",
                tone="err",
            ),
        )

    async def _run_isolated_feedback(self, action: dict[str, Any]) -> dict[str, Any]:
        """
        Post evaluator feedback strictly AFTER Radarr is permanently sealed at `completed_radarr`.
        A failure NEVER undoes or repeats Radarr.
        """
        spec = _feedback_spec_for_action(str(action["action_type"]))
        if spec is None:
            return action
        decision, note = spec
        fp = json.dumps(
            {
                "tmdb_id": action.get("bound_tmdb_id"),
                "imdb_id": action.get("bound_imdb_id"),
                "decision": decision,
                "note": note,
            },
            sort_keys=True,
        )
        ok, attempt_id, conflict = await asyncio.to_thread(
            self.db.begin_write_attempt,
            action_id=str(action["action_id"]),
            completion_group=str(action["completion_group"]),
            phase="evaluator_feedback",
            process_instance_id=self.process_instance_id,
            lease_expires_at=self._lease_iso(self.config.triage_timeout_seconds),
            request_fingerprint=fp,
            allowed_action_statuses=frozenset({"completed_radarr"}),
            forbid_group_completed_radarr=False,
        )
        if not ok or not attempt_id:
            return action

        fb_res = await self.triage_feedback.post_feedback(
            title=str(action["bound_title"]),
            imdb_id=action.get("bound_imdb_id"),
            tmdb_id=action.get("bound_tmdb_id"),
            decision=decision,
            note=note,
        )
        if fb_res.saved and fb_res.decision_saved:
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="succeeded",
                action_status="completed_all",
                outcome_summary=f"feedback_saved:{fb_res.decision_saved}",
                feedback_status="saved",
                feedback_decision_saved=fb_res.decision_saved,
            )
        else:
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="failed_definitive",
                action_status="completed_radarr",
                outcome_summary=fb_res.error_redacted or "feedback_failed",
                feedback_status="failed",
                last_error_redacted=fb_res.error_redacted or "Evaluator feedback failed",
            )
        refreshed = await asyncio.to_thread(self.db.get_action_group_readonly, str(action["token_sha256"]))
        return refreshed.action or action

    async def _execute_skip(self, action: dict[str, Any]) -> HtmlResponseData:
        """Standalone Skip: zero Radarr mutations, explicit user POST -> decision='skip'."""
        fp = json.dumps(
            {
                "tmdb_id": action.get("bound_tmdb_id"),
                "imdb_id": action.get("bound_imdb_id"),
                "decision": "skip",
                "note": "Skipped via Telegram",
            },
            sort_keys=True,
        )
        ok, attempt_id, conflict = await asyncio.to_thread(
            self.db.begin_write_attempt,
            action_id=str(action["action_id"]),
            completion_group=str(action["completion_group"]),
            phase="evaluator_feedback",
            process_instance_id=self.process_instance_id,
            lease_expires_at=self._lease_iso(self.config.triage_timeout_seconds),
            request_fingerprint=fp,
            allowed_action_statuses=frozenset({"active"}),
            forbid_group_completed_radarr=True,
        )
        if not ok or not attempt_id:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Skip Conflict",
                    badge="409",
                    message=f"Could not start Skip action ({conflict}).",
                    tone="warn",
                ),
            )

        fb_res = await self.triage_feedback.post_feedback(
            title=str(action["bound_title"]),
            imdb_id=action.get("bound_imdb_id"),
            tmdb_id=action.get("bound_tmdb_id"),
            decision="skip",
            note="Skipped via Telegram",
        )
        if fb_res.saved and fb_res.decision_saved == "SKIP":
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="succeeded",
                action_status="completed_all",
                outcome_summary="feedback_saved:SKIP",
                feedback_status="saved",
                feedback_decision_saved="SKIP",
            )
            refreshed = await asyncio.to_thread(self.db.get_action_group_readonly, str(action["token_sha256"]))
            return self._render_completion_html(refreshed.action or action, action)

        await asyncio.to_thread(
            self.db.finish_write_attempt,
            attempt_id=attempt_id,
            action_id=str(action["action_id"]),
            attempt_status="failed_definitive",
            action_status="active",
            outcome_summary=fb_res.error_redacted or "skip_feedback_failed",
            feedback_status="failed",
            last_error_redacted=fb_res.error_redacted or "Evaluator feedback did not save",
        )
        return HtmlResponseData(
            status_code=502,
            html=render_simple_status_page(
                title="Skip Feedback Not Saved",
                badge="feedback_failed",
                message="Evaluator did not confirm saving your Skip decision. No Radarr changes were made.",
                tone="warn",
                details=[("Detail", str(fb_res.error_redacted or "unknown_error"))],
                post_button_label="Retry Skip Feedback",
                hidden_fields={"confirm": "1"},
            ),
        )

    async def _validate_live_root_and_profile(
        self,
        *,
        requested_root: Optional[str],
        requested_profile_raw: Optional[str],
        allow_default_first: bool,
    ) -> tuple[bool, Optional[str], Optional[int], int, str]:
        roots_res, profiles_res = await asyncio.gather(
            self.radarr.get_root_folders(),
            self.radarr.get_quality_profiles(),
        )
        if not roots_res.ok or not profiles_res.ok:
            return False, None, None, 503, "Radarr is unreachable; cannot validate root folder and quality profile."

        valid_roots = [
            str(rf["path"]).strip()
            for rf in roots_res.data
            if isinstance(rf, dict) and isinstance(rf.get("path"), str) and rf["path"].strip()
        ]
        valid_profiles = {
            int(qp["id"]): str(qp.get("name") or "")
            for qp in profiles_res.data
            if isinstance(qp, dict) and isinstance(qp.get("id"), int) and not isinstance(qp.get("id"), bool)
        }
        if not valid_roots or not valid_profiles:
            return False, None, None, 503, "Radarr has no configured root folders or quality profiles."

        chosen_root = (requested_root or "").strip()
        if not chosen_root:
            if allow_default_first:
                chosen_root = valid_roots[0]
            else:
                return False, None, None, 400, "Root folder selection is required."
        if chosen_root not in valid_roots:
            return False, None, None, 400, "Selected root folder is not a valid live Radarr root folder."

        chosen_profile: Optional[int] = None
        if requested_profile_raw is not None and str(requested_profile_raw).strip():
            try:
                chosen_profile = int(str(requested_profile_raw).strip())
            except ValueError:
                return False, None, None, 400, "Invalid quality profile ID."
        elif allow_default_first:
            chosen_profile = next(iter(valid_profiles.keys()))
        else:
            return False, None, None, 400, "Quality profile selection is required."

        if chosen_profile not in valid_profiles:
            return False, None, None, 400, "Selected quality profile is not a valid live Radarr profile."
        return True, chosen_root, chosen_profile, 200, "ok"

    async def _execute_watched(
        self,
        action: dict[str, Any],
        form_data: dict[str, str],
        *,
        is_reconciliation: bool = False,
        existing_ambiguous_attempt_id: Optional[str] = None,
    ) -> HtmlResponseData:
        """
        Execute or reconcile `watched_good` / `watched_bad` / `watched_legacy_unrated`:
        - Dynamic `watched` tag lookup by label
        - Add unmonitored (`searchForMovie=false`) OR update preserving existing tags & files
        - Mandatory read-back `GET /api/v3/movie/{id}` asserting `watched_tag_id in tags` AND `monitored is False`
        - Isolated evaluator feedback (Good->watch, Bad->skip, legacy->none)
        """
        tmdb_id = action.get("bound_tmdb_id")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Invalid Action", badge="409", message="Missing verified TMDB ID.", tone="err"
                ),
            )

        tags_res, movies_res = await asyncio.gather(
            self.radarr.get_tags(),
            self.radarr.get_movies_by_tmdb(tmdb_id),
        )
        if not tags_res.ok or not movies_res.ok:
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Radarr Unavailable",
                    badge="503",
                    message="Radarr could not be reached to check movie/tag status. No write was attempted.",
                    tone="warn",
                ),
            )

        watched_tag_id = _find_watched_tag_id(tags_res.data)
        if watched_tag_id is None:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Radarr 'watched' Tag Missing",
                    badge="config_error",
                    message="Radarr does not have a tag labeled 'watched'. Create the tag in Radarr and retry.",
                    tone="err",
                ),
            )

        movies = [m for m in movies_res.data if isinstance(m, dict)]
        if len(movies) > 1:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Ambiguous Radarr Match",
                    badge="ambiguous",
                    message="Multiple Radarr movies match this TMDB ID; refusing watched mutation.",
                    tone="err",
                ),
            )

        existing = movies[0] if movies else None
        needs_feedback = _feedback_spec_for_action(str(action["action_type"])) is not None

        # On reconciliation: if desired state ALREADY satisfied (watched tag + unmonitored), seal cleanly!
        if is_reconciliation and existing is not None:
            m_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
            if watched_tag_id in m_tags and existing.get("monitored") is False:
                await asyncio.to_thread(
                    self.db.seal_reconciled_attempt,
                    attempt_id=existing_ambiguous_attempt_id,
                    action_id=str(action["action_id"]),
                    radarr_movie_id=int(existing["id"]),
                    outcome_summary="reconciled_watched_state_verified",
                    needs_feedback=needs_feedback,
                )
                refreshed = await asyncio.to_thread(
                    self.db.get_action_group_readonly, str(action["token_sha256"])
                )
                target = refreshed.action or action
                if needs_feedback:
                    target = await self._run_isolated_feedback(target)
                return self._render_completion_html(target, action)

        selected_root: Optional[str] = None
        selected_profile: Optional[int] = None
        if existing is None:
            if is_reconciliation and self._is_expired(action):
                await asyncio.to_thread(
                    self.db.mark_action_expired,
                    action_id=str(action["action_id"]),
                    detail="expired_with_watched_movie_absent",
                )
                return HtmlResponseData(
                    status_code=410,
                    html=render_simple_status_page(
                        title="Action Expired",
                        badge="expired",
                        message="Movie is absent from Radarr and this link has expired; no new watched entry was created.",
                        tone="warn",
                    ),
                )
            ok_rp, selected_root, selected_profile, code_rp, msg_rp = await self._validate_live_root_and_profile(
                requested_root=form_data.get("root_folder_path") or action.get("selected_root_folder"),
                requested_profile_raw=(
                    form_data.get("quality_profile_id")
                    or (
                        str(action["selected_quality_profile_id"])
                        if action.get("selected_quality_profile_id") is not None
                        else None
                    )
                ),
                allow_default_first=True,
            )
            if not ok_rp or not selected_root or selected_profile is None:
                return HtmlResponseData(
                    status_code=code_rp,
                    html=render_simple_status_page(
                        title="Cannot Add Watched Movie", badge=str(code_rp), message=msg_rp, tone="err"
                    ),
                )

        fp = json.dumps(
            {
                "tmdb_id": tmdb_id,
                "existing_movie_id": existing.get("id") if existing else None,
                "watched_tag_id": watched_tag_id,
                "root_folder": selected_root,
                "quality_profile_id": selected_profile,
                "action_type": action["action_type"],
            },
            sort_keys=True,
        )
        allowed_statuses = frozenset({"ambiguous_radarr"}) if is_reconciliation else frozenset({"active"})
        ok_lock, attempt_id, conflict = await asyncio.to_thread(
            self.db.begin_write_attempt,
            action_id=str(action["action_id"]),
            completion_group=str(action["completion_group"]),
            phase="radarr_watched",
            process_instance_id=self.process_instance_id,
            lease_expires_at=self._lease_iso(self.config.radarr_timeout_seconds),
            request_fingerprint=fp,
            allowed_action_statuses=allowed_statuses,
            forbid_group_completed_radarr=True,
            selected_root_folder=selected_root,
            selected_quality_profile_id=selected_profile,
            radarr_movie_id=int(existing["id"]) if existing and isinstance(existing.get("id"), int) else None,
        )
        if not ok_lock or not attempt_id:
            # Another concurrent request in completion_group won the lock!
            fresh_snap = await asyncio.to_thread(self.db.get_action_group_readonly, str(action["token_sha256"]))
            done_sib = next(
                (a for a in fresh_snap.group_actions if a["status"] in ("completed_all", "completed_radarr")),
                None,
            )
            if done_sib is not None:
                return self._render_completion_html(done_sib, action)
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Watched Action Already In Progress",
                    badge="409",
                    message=f"Another watched request in this group is already active ({conflict}).",
                    tone="warn",
                ),
            )

        movie_id: Optional[int] = None
        if existing is None:
            add_payload: dict[str, Any] = {
                "tmdbId": tmdb_id,
                "title": str(action["bound_title"]),
                "qualityProfileId": selected_profile,
                "rootFolderPath": selected_root,
                "monitored": False,
                "tags": [watched_tag_id],
                "addOptions": {"searchForMovie": False},
            }
            if action.get("bound_year") is not None:
                add_payload["year"] = int(action["bound_year"])
            if action.get("bound_imdb_id"):
                add_payload["imdbId"] = str(action["bound_imdb_id"])

            w_res = await self.radarr.add_movie(add_payload)
            if w_res.outcome == "ok" and isinstance(w_res.data, dict):
                movie_id = int(w_res.data["id"])
            elif w_res.outcome == "already_exists":
                # Delayed original POST won the race -> re-read movie and apply idempotent PUT if needed
                reread = await self.radarr.get_movies_by_tmdb(tmdb_id)
                if not reread.ok or len(reread.data) != 1 or not isinstance(reread.data[0], dict):
                    await asyncio.to_thread(
                        self.db.finish_write_attempt,
                        attempt_id=attempt_id,
                        action_id=str(action["action_id"]),
                        attempt_status="ambiguous",
                        action_status="ambiguous_radarr",
                        outcome_summary="watched_add_400_reread_failed",
                        last_error_redacted="Movie already existed in Radarr but could not be re-read",
                    )
                    return HtmlResponseData(
                        status_code=503,
                        html=render_simple_status_page(
                            title="Reconciliation Needed",
                            badge="ambiguous_radarr",
                            message="Movie already exists in Radarr; re-check status to reconcile.",
                            tone="warn",
                            post_button_label="Re-check Radarr Status & Reconcile",
                            hidden_fields={"confirm": "1", "reconcile": "1"},
                        ),
                    )
                existing = reread.data[0]
                movie_id = int(existing["id"])
                existing_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
                if watched_tag_id not in existing_tags or existing.get("monitored") is not False:
                    updated_payload = dict(existing)
                    updated_payload["monitored"] = False
                    updated_payload["tags"] = list(dict.fromkeys([*existing_tags, watched_tag_id]))
                    put_res = await self.radarr.update_movie(movie_id, updated_payload)
                    if put_res.outcome != "ok":
                        next_act_status = (
                            ("ambiguous_radarr" if is_reconciliation else "active")
                            if put_res.outcome in ("failed_not_transmitted", "failed_client")
                            else "ambiguous_radarr"
                        )
                        att_st = (
                            "failed_definitive"
                            if put_res.outcome in ("failed_not_transmitted", "failed_client")
                            else "ambiguous"
                        )
                        await asyncio.to_thread(
                            self.db.finish_write_attempt,
                            attempt_id=attempt_id,
                            action_id=str(action["action_id"]),
                            attempt_status=att_st,
                            action_status=next_act_status,
                            outcome_summary=put_res.error_redacted or "watched_put_after_race_failed",
                            radarr_movie_id=movie_id,
                            last_error_redacted=put_res.error_redacted,
                        )
                        return HtmlResponseData(
                            status_code=502,
                            html=render_simple_status_page(
                                title="Watched Update Unconfirmed",
                                badge=next_act_status,
                                message="Radarr update after existing-movie race did not confirm.",
                                tone="warn",
                                post_button_label="Re-check Radarr Status & Reconcile",
                                hidden_fields={"confirm": "1", "reconcile": "1"},
                            ),
                        )
            elif w_res.outcome in ("failed_not_transmitted", "failed_client"):
                restore_status = "ambiguous_radarr" if is_reconciliation else "active"
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=attempt_id,
                    action_id=str(action["action_id"]),
                    attempt_status="failed_definitive",
                    action_status=restore_status,
                    outcome_summary="not_transmitted" if w_res.outcome == "failed_not_transmitted" else w_res.error_redacted,
                    last_error_redacted=w_res.error_redacted,
                )
                return HtmlResponseData(
                    status_code=502 if w_res.outcome == "failed_not_transmitted" else 400,
                    html=render_simple_status_page(
                        title="Radarr Watched Add Failed",
                        badge="failed",
                        message=str(w_res.error_redacted or "Radarr rejected adding movie as watched."),
                        tone="err",
                    ),
                )
            else:
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=attempt_id,
                    action_id=str(action["action_id"]),
                    attempt_status="ambiguous",
                    action_status="ambiguous_radarr",
                    outcome_summary=w_res.error_redacted or "watched_add_ambiguous",
                    last_error_redacted=w_res.error_redacted,
                )
                return HtmlResponseData(
                    status_code=503,
                    html=render_simple_status_page(
                        title="Radarr Response Lost (Reconciliation Required)",
                        badge="ambiguous_radarr",
                        message="Radarr did not confirm the watched update in time. Re-check status to reconcile safely.",
                        tone="warn",
                        post_button_label="Re-check Radarr Status & Reconcile",
                        hidden_fields={"confirm": "1", "reconcile": "1"},
                    ),
                )
        else:
            movie_id = int(existing["id"])
            existing_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
            # Preserve ALL existing fields, existing files, and existing tags
            updated_payload = dict(existing)
            updated_payload["monitored"] = False
            updated_payload["tags"] = list(dict.fromkeys([*existing_tags, watched_tag_id]))
            put_res = await self.radarr.update_movie(movie_id, updated_payload)
            if put_res.outcome in ("failed_not_transmitted", "failed_client"):
                restore_status = "ambiguous_radarr" if is_reconciliation else "active"
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=attempt_id,
                    action_id=str(action["action_id"]),
                    attempt_status="failed_definitive",
                    action_status=restore_status,
                    outcome_summary="not_transmitted" if put_res.outcome == "failed_not_transmitted" else put_res.error_redacted,
                    radarr_movie_id=movie_id,
                    last_error_redacted=put_res.error_redacted,
                )
                return HtmlResponseData(
                    status_code=502 if put_res.outcome == "failed_not_transmitted" else 400,
                    html=render_simple_status_page(
                        title="Radarr Watched Update Failed",
                        badge="failed",
                        message=str(put_res.error_redacted or "Radarr rejected updating movie."),
                        tone="err",
                    ),
                )
            if put_res.outcome != "ok":
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=attempt_id,
                    action_id=str(action["action_id"]),
                    attempt_status="ambiguous",
                    action_status="ambiguous_radarr",
                    outcome_summary=put_res.error_redacted or "watched_put_ambiguous",
                    radarr_movie_id=movie_id,
                    last_error_redacted=put_res.error_redacted,
                )
                return HtmlResponseData(
                    status_code=503,
                    html=render_simple_status_page(
                        title="Radarr Response Lost (Reconciliation Required)",
                        badge="ambiguous_radarr",
                        message="Radarr did not confirm the watched update in time. Re-check status to reconcile safely.",
                        tone="warn",
                        post_button_label="Re-check Radarr Status & Reconcile",
                        hidden_fields={"confirm": "1", "reconcile": "1"},
                    ),
                )

        # MANDATORY READ-BACK VERIFICATION (§6.2): GET /api/v3/movie/{id}
        assert movie_id is not None
        verify_res = await self.radarr.get_movie_by_id(movie_id)
        if not verify_res.ok or not isinstance(verify_res.data, dict):
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="ambiguous",
                action_status="ambiguous_radarr",
                outcome_summary="watched_readback_failed",
                radarr_movie_id=movie_id,
                last_error_redacted="Read-back verification failed after watched update",
            )
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Watched Read-Back Unconfirmed",
                    badge="ambiguous_radarr",
                    message="Could not read back Radarr state to confirm watched tag and unmonitored flag. Re-check to reconcile.",
                    tone="warn",
                    post_button_label="Re-check Radarr Status & Reconcile",
                    hidden_fields={"confirm": "1", "reconcile": "1"},
                ),
            )

        rb_tags = verify_res.data.get("tags") if isinstance(verify_res.data.get("tags"), list) else []
        rb_monitored = verify_res.data.get("monitored")
        if watched_tag_id not in rb_tags or rb_monitored is not False:
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="ambiguous",
                action_status="ambiguous_radarr",
                outcome_summary=f"watched_readback_mismatch:monitored={rb_monitored}",
                radarr_movie_id=movie_id,
                last_error_redacted="Radarr read-back did not confirm both watched tag and monitored=false",
            )
            return HtmlResponseData(
                status_code=502,
                html=render_simple_status_page(
                    title="Watched Verification Mismatch",
                    badge="ambiguous_radarr",
                    message="Radarr read-back did not confirm both the 'watched' tag and monitored=false. Re-check to reconcile.",
                    tone="warn",
                    post_button_label="Re-check Radarr Status & Reconcile",
                    hidden_fields={"confirm": "1", "reconcile": "1"},
                ),
            )

        # Read-back verified! Seal Radarr permanently BEFORE posting isolated evaluator feedback!
        await asyncio.to_thread(
            self.db.finish_write_attempt,
            attempt_id=attempt_id,
            action_id=str(action["action_id"]),
            attempt_status="succeeded",
            action_status="completed_radarr" if needs_feedback else "completed_all",
            outcome_summary="watched_verified_readback",
            radarr_movie_id=movie_id,
            feedback_status="pending" if needs_feedback else "not_applicable",
            last_reconciled_at=utc_now_iso() if is_reconciliation else None,
        )
        refreshed = await asyncio.to_thread(self.db.get_action_group_readonly, str(action["token_sha256"]))
        target = refreshed.action or action
        if needs_feedback:
            target = await self._run_isolated_feedback(target)
        return self._render_completion_html(target, action)

    async def _execute_release_push_phase(
        self,
        *,
        action: dict[str, Any],
        radarr_movie_id: int,
        push_payload: dict[str, Any],
        allowed_action_statuses: frozenset[str],
        preopened_attempt_id: Optional[str] = None,
    ) -> HtmlResponseData:
        """
        Phase 2 (`radarr_release_push`): write-ahead journal (NO downloadUrl stored!),
        POST /api/v3/release/push with ONLY announced release, strict single-decision approval check,
        and isolated evaluator feedback.
        """
        attempt_id: Optional[str] = preopened_attempt_id
        if attempt_id is None:
            fp = json.dumps(
                {
                    "tmdb_id": action.get("bound_tmdb_id"),
                    "radarr_movie_id": radarr_movie_id,
                    "release_name": action.get("bound_release_name"),
                    "indexer": push_payload.get("indexer"),
                    "action_type": action["action_type"],
                },
                sort_keys=True,
            )
            ok_lock, attempt_id, conflict = await asyncio.to_thread(
                self.db.begin_write_attempt,
                action_id=str(action["action_id"]),
                completion_group=str(action["completion_group"]),
                phase="radarr_release_push",
                process_instance_id=self.process_instance_id,
                lease_expires_at=self._lease_iso(self.config.radarr_timeout_seconds),
                request_fingerprint=fp,
                allowed_action_statuses=allowed_action_statuses,
                forbid_group_completed_radarr=True,
                radarr_movie_id=radarr_movie_id,
            )
            if not ok_lock or not attempt_id:
                return HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Push Conflict",
                        badge="409",
                        message=f"Could not start release push ({conflict}).",
                        tone="warn",
                    ),
                )

        push_res = await self.radarr.push_release(push_payload)
        if push_res.outcome == "ok":
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="succeeded",
                action_status="completed_radarr",
                outcome_summary="release_push_approved",
                radarr_movie_id=radarr_movie_id,
                feedback_status="pending",
            )
            refreshed = await asyncio.to_thread(self.db.get_action_group_readonly, str(action["token_sha256"]))
            updated = await self._run_isolated_feedback(refreshed.action or action)
            return self._render_completion_html(updated, action)

        if push_res.outcome == "rejected":
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="failed_definitive",
                action_status="rejected_push",
                outcome_summary=push_res.error_redacted or "release_push_rejected",
                radarr_movie_id=radarr_movie_id,
                last_error_redacted=push_res.error_redacted or "Radarr rejected the release push.",
            )
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Release Push Rejected by Radarr",
                    badge="rejected_push",
                    message=str(push_res.error_redacted or "Radarr did not approve this release push."),
                    tone="err",
                    details=[("Release", str(action["bound_release_name"]))],
                ),
            )

        if push_res.outcome in ("failed_not_transmitted", "failed_client"):
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=attempt_id,
                action_id=str(action["action_id"]),
                attempt_status="failed_definitive",
                action_status="active",
                outcome_summary="not_transmitted" if push_res.outcome == "failed_not_transmitted" else push_res.error_redacted,
                radarr_movie_id=radarr_movie_id,
                last_error_redacted=push_res.error_redacted,
            )
            return HtmlResponseData(
                status_code=502 if push_res.outcome == "failed_not_transmitted" else 400,
                html=render_simple_status_page(
                    title="Release Push Failed Before Acceptance",
                    badge="failed",
                    message=str(push_res.error_redacted or "Could not reach Radarr to push release."),
                    tone="err",
                ),
            )

        # Ambiguous post-transmit failure -> NEVER blindly re-push!
        await asyncio.to_thread(
            self.db.finish_write_attempt,
            attempt_id=attempt_id,
            action_id=str(action["action_id"]),
            attempt_status="ambiguous",
            action_status="ambiguous_radarr",
            outcome_summary=push_res.error_redacted or "release_push_ambiguous",
            radarr_movie_id=radarr_movie_id,
            last_error_redacted=push_res.error_redacted or "Lost Radarr acknowledgment during release push",
        )
        return HtmlResponseData(
            status_code=503,
            html=render_simple_status_page(
                title="Radarr Push Acknowledgment Lost",
                badge="ambiguous_radarr",
                message=(
                    "Radarr did not return a clear confirmation for /api/v3/release/push. "
                    "No second push will be sent automatically. Click below to re-check Radarr queue and history."
                ),
                tone="warn",
                details=[("Release", str(action["bound_release_name"]))],
                post_button_label="Re-check Radarr Status",
                hidden_fields={"confirm": "1", "reconcile": "1"},
            ),
        )

    async def _execute_add_grab(
        self,
        action: dict[str, Any],
        form_data: dict[str, str],
        *,
        is_phase1_retry: bool = False,
    ) -> HtmlResponseData:
        """Two-phase Add & Grab: pre-write checks -> Phase 1 add movie (if absent) -> Phase 2 release push."""
        tmdb_id = action.get("bound_tmdb_id")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Invalid Action", badge="409", message="Missing verified TMDB ID.", tone="err"
                ),
            )

        # Pre-write check 1: lookup live movie in Radarr
        movies_res = await self.radarr.get_movies_by_tmdb(tmdb_id)
        if not movies_res.ok:
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Radarr Unavailable",
                    badge="503",
                    message="Radarr could not be reached; aborting before any write.",
                    tone="warn",
                ),
            )
        movies = [m for m in movies_res.data if isinstance(m, dict)]
        if len(movies) > 1:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Ambiguous Radarr Match",
                    badge="ambiguous",
                    message="Multiple Radarr movies match this TMDB ID; refusing Add & Grab.",
                    tone="err",
                ),
            )
        existing = movies[0] if movies else None

        # Pre-write check 2: validate live root folder & quality profile if movie absent
        selected_root: Optional[str] = None
        selected_profile: Optional[int] = None
        if existing is None:
            ok_rp, selected_root, selected_profile, code_rp, msg_rp = await self._validate_live_root_and_profile(
                requested_root=form_data.get("root_folder_path") or action.get("selected_root_folder"),
                requested_profile_raw=(
                    form_data.get("quality_profile_id")
                    or (
                        str(action["selected_quality_profile_id"])
                        if action.get("selected_quality_profile_id") is not None
                        else None
                    )
                ),
                allow_default_first=False,
            )
            if not ok_rp or not selected_root or selected_profile is None:
                return HtmlResponseData(
                    status_code=code_rp,
                    html=render_simple_status_page(
                        title="Invalid Selection", badge=str(code_rp), message=msg_rp, tone="err"
                    ),
                )

        # Pre-write check 3: fetch ephemeral push payload from movie-intake-api IN MEMORY ONLY
        payload_res = await self.intake_push.fetch_push_payload(
            release_id=str(action["release_id"]),
            source_occurrence_id=str(action["source_occurrence_id"]),
        )
        if not payload_res.ok or not payload_res.push_payload:
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Release Payload Unavailable",
                    badge="503",
                    message="Could not resolve verified release push payload from intake archive. No Radarr write was attempted.",
                    tone="err",
                    details=[("Detail", str(payload_res.error_redacted or "unavailable"))],
                ),
            )
        push_payload = payload_res.push_payload

        radarr_movie_id: Optional[int] = (
            int(existing["id"]) if existing and isinstance(existing.get("id"), int) else None
        )
        phase2_allowed = frozenset({"ambiguous_radarr"}) if is_phase1_retry else frozenset({"active"})
        preopened_phase2_attempt_id: Optional[str] = None

        # Phase 1: radarr_add_movie (ONLY when movie is absent)
        if radarr_movie_id is None:
            fp1 = json.dumps(
                {
                    "tmdb_id": tmdb_id,
                    "root_folder": selected_root,
                    "quality_profile_id": selected_profile,
                },
                sort_keys=True,
            )
            ok1, att1_id, conflict1 = await asyncio.to_thread(
                self.db.begin_write_attempt,
                action_id=str(action["action_id"]),
                completion_group=str(action["completion_group"]),
                phase="radarr_add_movie",
                process_instance_id=self.process_instance_id,
                lease_expires_at=self._lease_iso(self.config.radarr_timeout_seconds),
                request_fingerprint=fp1,
                allowed_action_statuses=phase2_allowed,
                forbid_group_completed_radarr=True,
                selected_root_folder=selected_root,
                selected_quality_profile_id=selected_profile,
            )
            if not ok1 or not att1_id:
                return HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Add & Grab Conflict",
                        badge="409",
                        message=f"Could not start Add Movie phase ({conflict1}).",
                        tone="warn",
                    ),
                )

            add_body: dict[str, Any] = {
                "tmdbId": tmdb_id,
                "title": str(action["bound_title"]),
                "qualityProfileId": selected_profile,
                "rootFolderPath": selected_root,
                "monitored": True,
                "tags": [],
                "addOptions": {"searchForMovie": False},
            }
            if action.get("bound_year") is not None:
                add_body["year"] = int(action["bound_year"])
            if action.get("bound_imdb_id"):
                add_body["imdbId"] = str(action["bound_imdb_id"])

            add_res = await self.radarr.add_movie(add_body)
            phase1_outcome = ""
            if add_res.outcome == "ok" and isinstance(add_res.data, dict):
                radarr_movie_id = int(add_res.data["id"])
                phase1_outcome = f"movie_added:{radarr_movie_id}"
            elif add_res.outcome == "already_exists":
                reread = await self.radarr.get_movies_by_tmdb(tmdb_id)
                if reread.ok and len(reread.data) == 1 and isinstance(reread.data[0], dict):
                    radarr_movie_id = int(reread.data[0]["id"])
                    phase1_outcome = f"movie_reused_after_400:{radarr_movie_id}"
                else:
                    await asyncio.to_thread(
                        self.db.finish_write_attempt,
                        attempt_id=att1_id,
                        action_id=str(action["action_id"]),
                        attempt_status="ambiguous",
                        action_status="ambiguous_radarr",
                        outcome_summary="add_400_reread_failed",
                        last_error_redacted="Movie already exists in Radarr but could not be re-read",
                    )
                    return HtmlResponseData(
                        status_code=503,
                        html=render_simple_status_page(
                            title="Reconciliation Required",
                            badge="ambiguous_radarr",
                            message="Radarr reported the movie exists, but follow-up lookup failed. Click to reconcile.",
                            tone="warn",
                            post_button_label="Re-check Radarr Status & Reconcile",
                            hidden_fields={"confirm": "1", "reconcile": "1"},
                        ),
                    )
            elif add_res.outcome in ("failed_not_transmitted", "failed_client"):
                restore_st = "ambiguous_radarr" if is_phase1_retry else "active"
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=att1_id,
                    action_id=str(action["action_id"]),
                    attempt_status="failed_definitive",
                    action_status=restore_st,
                    outcome_summary="not_transmitted" if add_res.outcome == "failed_not_transmitted" else add_res.error_redacted,
                    last_error_redacted=add_res.error_redacted,
                )
                return HtmlResponseData(
                    status_code=502 if add_res.outcome == "failed_not_transmitted" else 400,
                    html=render_simple_status_page(
                        title="Add Movie Failed",
                        badge="failed",
                        message=str(add_res.error_redacted or "Radarr rejected adding movie."),
                        tone="err",
                    ),
                )
            else:
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=att1_id,
                    action_id=str(action["action_id"]),
                    attempt_status="ambiguous",
                    action_status="ambiguous_radarr",
                    outcome_summary=add_res.error_redacted or "add_movie_ambiguous",
                    last_error_redacted=add_res.error_redacted,
                )
                return HtmlResponseData(
                    status_code=503,
                    html=render_simple_status_page(
                        title="Add Movie Outcome Unconfirmed",
                        badge="ambiguous_radarr",
                        message="Radarr did not confirm adding the movie. Release push was NOT started. Re-check to reconcile.",
                        tone="warn",
                        post_button_label="Re-check Radarr Status & Reconcile",
                        hidden_fields={"confirm": "1", "reconcile": "1"},
                    ),
                )

            # Atomically close Phase 1 and open Phase 2 in ONE transaction (zero dangling gap!)
            assert radarr_movie_id is not None
            fp2 = json.dumps(
                {
                    "tmdb_id": tmdb_id,
                    "radarr_movie_id": radarr_movie_id,
                    "release_name": action.get("bound_release_name"),
                    "indexer": push_payload.get("indexer"),
                    "action_type": action["action_type"],
                },
                sort_keys=True,
            )
            preopened_phase2_attempt_id = await asyncio.to_thread(
                self.db.complete_phase1_and_begin_phase2,
                phase1_attempt_id=att1_id,
                action_id=str(action["action_id"]),
                completion_group=str(action["completion_group"]),
                radarr_movie_id=radarr_movie_id,
                phase1_outcome=phase1_outcome,
                process_instance_id=self.process_instance_id,
                phase2_lease_expires_at=self._lease_iso(self.config.radarr_timeout_seconds),
                phase2_request_fingerprint=fp2,
            )

        # Phase 2: radarr_release_push (ONLY announced release)
        assert radarr_movie_id is not None
        return await self._execute_release_push_phase(
            action=action,
            radarr_movie_id=radarr_movie_id,
            push_payload=push_payload,
            allowed_action_statuses=phase2_allowed,
            preopened_attempt_id=preopened_phase2_attempt_id,
        )

    async def _execute_alternative_grab(
        self,
        action: dict[str, Any],
        form_data: dict[str, str],
    ) -> HtmlResponseData:
        ok, movie, code, msg = await self._validate_alternative_grab_live(action)
        if not ok or movie is None:
            return HtmlResponseData(
                status_code=code,
                html=render_simple_status_page(
                    title="Alternative Grab Refused",
                    badge=str(code),
                    message=msg,
                    tone="err" if code == 409 else "warn",
                ),
            )

        payload_res = await self.intake_push.fetch_push_payload(
            release_id=str(action["release_id"]),
            source_occurrence_id=str(action["source_occurrence_id"]),
        )
        if not payload_res.ok or not payload_res.push_payload:
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Release Payload Unavailable",
                    badge="503",
                    message="Could not resolve verified release push payload from intake archive. No Radarr write was attempted.",
                    tone="err",
                ),
            )

        return await self._execute_release_push_phase(
            action=action,
            radarr_movie_id=int(movie["id"]),
            push_payload=payload_res.push_payload,
            allowed_action_statuses=frozenset({"active"}),
        )

    # =========================================================================
    # 5. Ambiguous Radarr Reconciliation (§6.3 & Mandatory Case 5b / 5f)
    # =========================================================================

    def _matches_announced_release(
        self,
        *,
        expected_release_name: str,
        candidate_title: Optional[str],
        candidate_timestamp: Optional[str],
        attempt_started_at: Optional[str],
    ) -> bool:
        exp_norm = _normalize_release_match_key(expected_release_name)
        cand_norm = _normalize_release_match_key(candidate_title)
        if not exp_norm or not cand_norm:
            return False
        if cand_norm != exp_norm and exp_norm not in cand_norm:
            return False
        if candidate_timestamp and attempt_started_at:
            cand_dt = parse_iso_utc(candidate_timestamp)
            start_dt = parse_iso_utc(attempt_started_at)
            if cand_dt is not None and start_dt is not None:
                if cand_dt < (start_dt - timedelta(seconds=60)):
                    return False
        return True

    async def _reconcile_ambiguous_action(
        self,
        amb_act: dict[str, Any],
        group_attempts: tuple[dict[str, Any], ...],
        form_data: dict[str, str],
    ) -> HtmlResponseData:
        # Hard single-push invariant: if ANY radarr_release_push attempt for this action is ambiguous,
        # ALWAYS reconcile as Phase B (never fall back to Phase A under clock skew).
        radarr_att = next(
            (
                a
                for a in group_attempts
                if a["action_id"] == amb_act["action_id"]
                and a["phase"] == "radarr_release_push"
                and a["status"] == "ambiguous"
            ),
            None,
        )
        if radarr_att is None:
            radarr_att = next(
                (
                    a
                    for a in group_attempts
                    if a["action_id"] == amb_act["action_id"]
                    and a["phase"] in ("radarr_release_push", "radarr_add_movie", "radarr_watched")
                ),
                None,
            )
        phase = str(radarr_att["phase"]) if radarr_att else (
            "radarr_watched"
            if str(amb_act["action_type"]).startswith("watched")
            else "radarr_release_push"
        )
        att_id = str(radarr_att["attempt_id"]) if radarr_att else None
        att_started = str(radarr_att["started_at"]) if radarr_att else None

        # --- Phase C: radarr_watched (idempotent desired-state mutation) ---
        if phase == "radarr_watched":
            return await self._execute_watched(
                amb_act,
                form_data,
                is_reconciliation=True,
                existing_ambiguous_attempt_id=att_id,
            )

        tmdb_id = amb_act.get("bound_tmdb_id")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Cannot Reconcile", badge="409", message="Missing verified TMDB ID.", tone="err"
                ),
            )

        # --- Phase A: radarr_add_movie (pre-push — radarr_release_push was NEVER entered!) ---
        if phase == "radarr_add_movie":
            movies_res = await self.radarr.get_movies_by_tmdb(tmdb_id)
            if not movies_res.ok:
                return HtmlResponseData(
                    status_code=503,
                    html=render_simple_status_page(
                        title="Radarr Unavailable",
                        badge="ambiguous_radarr",
                        message="Radarr is unreachable during reconciliation. Status remains ambiguous; zero writes made.",
                        tone="warn",
                        post_button_label="Re-check Radarr Status & Reconcile",
                        hidden_fields={"confirm": "1", "reconcile": "1"},
                    ),
                )
            movies = [m for m in movies_res.data if isinstance(m, dict)]
            if len(movies) == 1:
                movie_id = int(movies[0]["id"])
                await asyncio.to_thread(
                    self.db.mark_phase1_add_movie_reconciled,
                    attempt_id=att_id,
                    action_id=str(amb_act["action_id"]),
                    radarr_movie_id=movie_id,
                    outcome_summary=f"reconciled_movie_present:{movie_id}",
                )
                if self._is_expired(amb_act):
                    await asyncio.to_thread(
                        self.db.mark_action_expired,
                        action_id=str(amb_act["action_id"]),
                        detail="expired_before_phase2_push",
                    )
                    return HtmlResponseData(
                        status_code=410,
                        html=render_simple_status_page(
                            title="Action Expired Before Push",
                            badge="expired",
                            message="Movie was added to Radarr, but the action link expired before release push. No push was sent.",
                            tone="warn",
                        ),
                    )
                payload_res = await self.intake_push.fetch_push_payload(
                    release_id=str(amb_act["release_id"]),
                    source_occurrence_id=str(amb_act["source_occurrence_id"]),
                )
                if not payload_res.ok or not payload_res.push_payload:
                    return HtmlResponseData(
                        status_code=503,
                        html=render_simple_status_page(
                            title="Release Payload Unavailable",
                            badge="503",
                            message="Movie exists in Radarr, but intake push payload could not be resolved right now.",
                            tone="warn",
                            post_button_label="Re-check Radarr Status & Reconcile",
                            hidden_fields={"confirm": "1", "reconcile": "1"},
                        ),
                    )
                return await self._execute_release_push_phase(
                    action=amb_act,
                    radarr_movie_id=movie_id,
                    push_payload=payload_res.push_payload,
                    allowed_action_statuses=frozenset({"ambiguous_radarr"}),
                )

            if len(movies) == 0:
                if self._is_expired(amb_act):
                    await asyncio.to_thread(
                        self.db.mark_action_expired,
                        action_id=str(amb_act["action_id"]),
                        detail="expired_with_movie_absent",
                    )
                    return HtmlResponseData(
                        status_code=410,
                        html=render_simple_status_page(
                            title="Action Expired",
                            badge="expired",
                            message="Movie is absent from Radarr and this link has expired; no mutation was attempted.",
                            tone="warn",
                        ),
                    )
                return await self._execute_add_grab(amb_act, form_data, is_phase1_retry=True)

            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Ambiguous Radarr Library",
                    badge="ambiguous_radarr",
                    message="Multiple Radarr movies matched this TMDB ID during reconciliation.",
                    tone="err",
                ),
            )

        # --- Phase B: radarr_release_push (non-idempotent acquisition: NEVER issue a second push!) ---
        movies_res = await self.radarr.get_movies_by_tmdb(tmdb_id)
        if not movies_res.ok:
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Radarr Unavailable During Reconciliation",
                    badge="ambiguous_radarr",
                    message="Could not reach Radarr to verify queue/history. Status remains ambiguous; zero pushes issued.",
                    tone="warn",
                    post_button_label="Re-check Radarr Status",
                    hidden_fields={"confirm": "1", "reconcile": "1"},
                ),
            )

        movies = [m for m in movies_res.data if isinstance(m, dict)]
        movie = movies[0] if len(movies) == 1 else None
        movie_id = (
            int(movie["id"])
            if movie and isinstance(movie.get("id"), int)
            else (int(amb_act["radarr_movie_id"]) if amb_act.get("radarr_movie_id") else None)
        )

        match_found = False
        match_source = ""
        expected_release = str(amb_act["bound_release_name"])
        bound_old_file_id = amb_act.get("bound_existing_file_id")

        if movie_id is not None:
            q_res, h_res = await asyncio.gather(
                self.radarr.get_queue_details(movie_id),
                self.radarr.get_movie_history(movie_id),
            )
            if not q_res.ok or not h_res.ok:
                return HtmlResponseData(
                    status_code=503,
                    html=render_simple_status_page(
                        title="Radarr Queue/History Unavailable",
                        badge="ambiguous_radarr",
                        message="Could not read Radarr queue/history. Status remains ambiguous; zero pushes issued.",
                        tone="warn",
                        post_button_label="Re-check Radarr Status",
                        hidden_fields={"confirm": "1", "reconcile": "1"},
                    ),
                )

            # 1. Check queue details
            for q_item in q_res.data:
                if not isinstance(q_item, dict):
                    continue
                if self._matches_announced_release(
                    expected_release_name=expected_release,
                    candidate_title=q_item.get("title") or q_item.get("sourceTitle"),
                    candidate_timestamp=q_item.get("added"),
                    attempt_started_at=att_started,
                ):
                    match_found = True
                    match_source = "queue"
                    break

            # 2. Check grab/import history
            if not match_found:
                for h_item in h_res.data:
                    if not isinstance(h_item, dict):
                        continue
                    ev = str(h_item.get("eventType") or "").strip().lower()
                    if ev not in ("grabbed", "downloadfolderimported", "grab"):
                        continue
                    h_data = h_item.get("data") if isinstance(h_item.get("data"), dict) else {}
                    for cand_field in (
                        h_item.get("sourceTitle"),
                        h_data.get("droppedPath"),
                        h_data.get("importedPath"),
                    ):
                        if self._matches_announced_release(
                            expected_release_name=expected_release,
                            candidate_title=cand_field,
                            candidate_timestamp=h_item.get("date"),
                            attempt_started_at=att_started,
                        ):
                            match_found = True
                            match_source = f"history:{ev}"
                            break
                    if match_found:
                        break

            # 3. Check newly imported movieFile (must NOT match old bound_existing_file_id!)
            if not match_found and movie is not None and isinstance(movie.get("movieFile"), dict):
                mf = movie["movieFile"]
                mf_id = mf.get("id")
                is_old_file = bound_old_file_id is not None and mf_id == bound_old_file_id
                if not is_old_file:
                    for cand_field in (mf.get("sceneName"), mf.get("relativePath")):
                        if self._matches_announced_release(
                            expected_release_name=expected_release,
                            candidate_title=cand_field,
                            candidate_timestamp=mf.get("dateAdded"),
                            attempt_started_at=att_started,
                        ):
                            match_found = True
                            match_source = "imported_file"
                            break

        if match_found:
            await asyncio.to_thread(
                self.db.seal_reconciled_attempt,
                attempt_id=att_id,
                action_id=str(amb_act["action_id"]),
                radarr_movie_id=movie_id,
                outcome_summary=f"reconciled_push_found_in_{match_source}",
                needs_feedback=True,
            )
            refreshed = await asyncio.to_thread(self.db.get_action_group_readonly, str(amb_act["token_sha256"]))
            updated = await self._run_isolated_feedback(refreshed.action or amb_act)
            return self._render_completion_html(updated, amb_act)

        # Match NOT FOUND (absent snapshot) -> MUST retain ambiguous_radarr and NEVER issue a 2nd push!
        await asyncio.to_thread(
            self.db.mark_action_reconciled_still_ambiguous,
            action_id=str(amb_act["action_id"]),
            detail="reconcile_snapshot_absent_no_second_push",
        )
        return HtmlResponseData(
            status_code=200,
            html=render_simple_status_page(
                title="Push Still Unconfirmed in Radarr",
                badge="ambiguous_radarr",
                message=(
                    "The announced release has not yet appeared in Radarr queue, history, or imported files. "
                    "Because the original push may still be completing inside Radarr, NO second release push "
                    "will be sent automatically. Re-check Radarr status shortly, or inspect Radarr directly."
                ),
                tone="warn",
                details=[
                    ("Release", expected_release),
                    ("Last Checked", utc_now_iso()),
                ],
                post_button_label="Re-check Radarr Status",
                hidden_fields={"confirm": "1", "reconcile": "1"},
            ),
        )
