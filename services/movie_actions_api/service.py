from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from .alternative_grab import (
    QBittorrentClient,
    TorrentFetcher,
    build_movie_file_snapshot,
    compare_movie_file_snapshots,
    read_alt_grab_receipt,
    validate_legacy_alt_grab_source,
    validate_radarr_qbit_route,
    write_alt_grab_receipt,
)
from .clients import (
    IntakePushClient,
    RadarrClient,
    TelegramClient,
    TriageFeedbackClient,
)
from .config import ActionsConfig, AlternativeGrabConfig
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
from .security import PrivateFileError, hash_token, mint_opaque_token, redact_secrets
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
_MEDIA_EXT_RE = re.compile(r"\.(?:mkv|mp4|m2ts|iso|avi|ts|mov|wmv|torrent)$", re.IGNORECASE)
_SAFE_GUID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}:[a-z0-9][a-z0-9._-]{0,127}$", re.IGNORECASE)
_IMDB_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)


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
    stripped = _MEDIA_EXT_RE.sub("", str(name).strip())
    return _NORM_SPACE_RE.sub(" ", stripped.lower()).strip()


def _extract_safe_guid(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if _SAFE_GUID_RE.fullmatch(cleaned):
        return cleaned.lower()
    return None


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


def _movie_has_file_evidence(movie: dict[str, Any]) -> bool:
    """Return True if a Radarr movie dict has any evidence of owning a file on disk."""
    movie_file_obj = movie.get("movieFile")
    has_movie_file_obj = isinstance(movie_file_obj, dict) and bool(
        (
            isinstance(movie_file_obj.get("id"), int)
            and not isinstance(movie_file_obj.get("id"), bool)
            and movie_file_obj["id"] > 0
        )
        or str(movie_file_obj.get("relativePath") or "").strip()
        or str(movie_file_obj.get("path") or "").strip()
    )
    size_on_disk = movie.get("sizeOnDisk")
    has_size_on_disk = (
        isinstance(size_on_disk, (int, float))
        and not isinstance(size_on_disk, bool)
        and size_on_disk > 0
    )
    return bool(
        movie.get("hasFile")
        or _extract_file_id(movie) is not None
        or has_movie_file_obj
        or has_size_on_disk
    )


def _reconcile_radarr_row_snapshots(
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    conservative_for_grab: bool = True,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """
    PR #18 Round 3 Finding 1: Reconcile two observations of the same Radarr movie row ID
    returned by separate TMDb and IMDb lookups.
      - Always reject identity drift (conflicting tmdbId or imdbId across observations).
      - When `conservative_for_grab=True` (card-prep status & Add & Grab confirmation),
        merge monotonically so if EITHER observation shows file ownership, a watched tag,
        or unmonitored status, the merged snapshot preserves that ineligibility.
      - When `conservative_for_grab=False` (Watched mutation), retain the fresher `second`
        snapshot so any required PUT update is never skipped.
    """
    tmdb_1 = first.get("tmdbId")
    tmdb_2 = second.get("tmdbId")
    if (
        isinstance(tmdb_1, int)
        and not isinstance(tmdb_1, bool)
        and isinstance(tmdb_2, int)
        and not isinstance(tmdb_2, bool)
        and tmdb_1 != tmdb_2
    ):
        return None, "radarr_snapshot_tmdb_drift"

    imdb_1 = str(first.get("imdbId") or "").strip().lower()
    imdb_2 = str(second.get("imdbId") or "").strip().lower()
    if imdb_1 and imdb_2 and imdb_1 != imdb_2:
        return None, "radarr_snapshot_imdb_drift"

    if not conservative_for_grab:
        merged = dict(second)
        if not imdb_2 and imdb_1:
            merged["imdbId"] = first.get("imdbId")
        return merged, None

    merged = dict(second)
    if not imdb_2 and imdb_1:
        merged["imdbId"] = first.get("imdbId")

    tags_1 = [t for t in (first.get("tags") or []) if isinstance(t, int) and not isinstance(t, bool)] if isinstance(first.get("tags"), list) else []
    tags_2 = [t for t in (second.get("tags") or []) if isinstance(t, int) and not isinstance(t, bool)] if isinstance(second.get("tags"), list) else []
    merged["tags"] = list(dict.fromkeys([*tags_1, *tags_2]))
    merged["monitored"] = bool(first.get("monitored", False)) and bool(second.get("monitored", False))

    first_has_file = _movie_has_file_evidence(first)
    second_has_file = _movie_has_file_evidence(second)
    if first_has_file or second_has_file:
        src = second if second_has_file else first
        merged["hasFile"] = bool(first.get("hasFile")) or bool(second.get("hasFile"))
        if src.get("movieFileId") is not None:
            merged["movieFileId"] = src.get("movieFileId")
        if isinstance(src.get("movieFile"), dict):
            merged["movieFile"] = src.get("movieFile")
        if src.get("sizeOnDisk") is not None:
            merged["sizeOnDisk"] = src.get("sizeOnDisk")
    return merged, None


def _feedback_spec_for_action(action_type: str) -> Optional[tuple[str, str]]:
    """Return (decision, note) for action types that post to movie-triage-api /feedback."""
    if action_type == "add_grab":
        return ("watch", "Grabbed via Telegram")
    if action_type == "watched_good":
        return ("watch", "watched")
    if action_type == "watched_bad":
        return ("skip", "watched")
    if action_type == "skip":
        return ("skip", "Skipped via Telegram")
    # alternative_grab and watched_legacy_unrated send NO evaluator feedback
    return None


class ActionsService:
    """Authoritative coordinator for Radarr reads/writes, Telegram deliveries, actions, qBit alt-grab, and feedback."""

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
        alt_grab_config: Optional[AlternativeGrabConfig] = None,
        torrent_fetcher: Optional[TorrentFetcher] = None,
        qbit_client: Optional[QBittorrentClient] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.process_instance_id = process_instance_id
        self.radarr = radarr_client
        self.telegram = telegram_client
        self.intake_push = intake_push_client
        self.triage_feedback = triage_feedback_client
        self.alt_grab_config = alt_grab_config
        self.torrent_fetcher = torrent_fetcher
        self.qbit_client = qbit_client

    @property
    def alternative_grab_configured(self) -> bool:
        return self.alt_grab_config is not None

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
            return {
                "status": "ambiguous",
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": "missing_movie_identifiers",
            }

        tags_res = await self.radarr.get_tags()
        if not tags_res.ok:
            return {
                "status": "lookup_failed",
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": tags_res.error_redacted or "radarr_tags_failed",
            }
        watched_tag_id = _find_watched_tag_id(tags_res.data)

        deduped: dict[Any, dict[str, Any]] = {}
        fallback_idx = 0

        def _merge_candidate_row(row_obj: dict[str, Any]) -> Optional[str]:
            nonlocal fallback_idx
            mid = row_obj.get("id")
            key = ("id", int(mid)) if isinstance(mid, int) and not isinstance(mid, bool) else ("idx", fallback_idx)
            if key[0] == "idx":
                fallback_idx += 1
            if key not in deduped:
                deduped[key] = row_obj
                return None
            reconciled, drift = _reconcile_radarr_row_snapshots(
                deduped[key],
                row_obj,
                conservative_for_grab=True,
            )
            if drift is not None or reconciled is None:
                return drift or "radarr_snapshot_drift"
            deduped[key] = reconciled
            return None

        if tmdb_id is not None:
            m_res = await self.radarr.get_movies_by_tmdb(tmdb_id)
            if not m_res.ok:
                return {
                    "status": "lookup_failed",
                    "alternative_grab_configured": self.alternative_grab_configured,
                    "detail": m_res.error_redacted or "radarr_tmdb_lookup_failed",
                }
            for m in m_res.data:
                if not isinstance(m, dict):
                    continue
                drift = _merge_candidate_row(m)
                if drift is not None:
                    return {
                        "status": "ambiguous",
                        "alternative_grab_configured": self.alternative_grab_configured,
                        "detail": drift,
                    }

        if clean_imdb:
            # Always cross-check imdbId (even when tmdb_id matched) so duplicate library
            # rows sharing the same IMDb across distinct TMDb IDs are flagged as ambiguous,
            # and same-row state changes between lookups are conservatively reconciled.
            i_res = await self.radarr.get_movies_by_imdb(clean_imdb)
            if not i_res.ok:
                return {
                    "status": "lookup_failed",
                    "alternative_grab_configured": self.alternative_grab_configured,
                    "detail": i_res.error_redacted or "radarr_imdb_lookup_failed",
                }
            for m in i_res.data:
                if not isinstance(m, dict):
                    continue
                drift = _merge_candidate_row(m)
                if drift is not None:
                    return {
                        "status": "ambiguous",
                        "alternative_grab_configured": self.alternative_grab_configured,
                        "detail": drift,
                    }

        matches = list(deduped.values())
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": "multiple_radarr_movies_for_ids",
            }

        matched_movie: Optional[dict[str, Any]] = matches[0] if matches else None
        if matched_movie is not None:
            cand_tmdb = matched_movie.get("tmdbId")
            cand_imdb = str(matched_movie.get("imdbId") or "").strip().lower()
            if (
                tmdb_id is not None
                and isinstance(cand_tmdb, int)
                and not isinstance(cand_tmdb, bool)
                and cand_tmdb != tmdb_id
            ):
                return {
                    "status": "ambiguous",
                    "alternative_grab_configured": self.alternative_grab_configured,
                    "detail": "radarr_tmdb_id_mismatch_on_imdb_match",
                }
            if clean_imdb and cand_imdb and cand_imdb != clean_imdb:
                return {
                    "status": "ambiguous",
                    "alternative_grab_configured": self.alternative_grab_configured,
                    "detail": "radarr_imdb_id_mismatch",
                }

        if matched_movie is None:
            return {
                "status": "absent",
                "movie_id": None,
                "file_id": None,
                "existing_quality": None,
                "monitored": None,
                "movie_file_snapshot": None,
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": None,
            }

        movie_id = matched_movie.get("id")
        if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id <= 0:
            return {
                "status": "ambiguous",
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": "radarr_movie_missing_valid_id",
            }

        raw_tags = matched_movie.get("tags")
        movie_tags = [t for t in raw_tags if isinstance(t, int) and not isinstance(t, bool)] if isinstance(raw_tags, list) else []
        has_watched = watched_tag_id is not None and watched_tag_id in movie_tags
        monitored = bool(matched_movie.get("monitored", False))
        file_id = _extract_file_id(matched_movie)
        has_file = _movie_has_file_evidence(matched_movie)
        existing_quality = _extract_existing_quality(matched_movie)

        if has_watched and not monitored:
            return {
                "status": "watched_unmonitored",
                "movie_id": movie_id,
                "file_id": file_id,
                "existing_quality": existing_quality,
                "monitored": monitored,
                "movie_file_snapshot": None,
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": "watched_tag_unmonitored",
            }
        if has_file:
            mf_snap = build_movie_file_snapshot(matched_movie)
            return {
                "status": "owned_file",
                "movie_id": movie_id,
                "file_id": file_id,
                "existing_quality": existing_quality,
                "monitored": monitored,
                "movie_file_snapshot": mf_snap,
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": "has_file" if bool(matched_movie.get("hasFile", False)) else "file_evidence_present",
            }
        if monitored:
            return {
                "status": "monitored_no_file",
                "movie_id": movie_id,
                "file_id": None,
                "existing_quality": None,
                "monitored": True,
                "movie_file_snapshot": None,
                "alternative_grab_configured": self.alternative_grab_configured,
                "detail": "monitored_without_file",
            }
        return {
            "status": "absent",
            "movie_id": movie_id,
            "file_id": None,
            "existing_quality": None,
            "monitored": False,
            "movie_file_snapshot": None,
            "alternative_grab_configured": self.alternative_grab_configured,
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
        if delivery_kind not in ("actionable_grab", "alternative_grab", "evidence_review", "retired"):
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

        if delivery_kind in ("evidence_review", "retired"):
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
        bound_snapshot_json: Optional[str] = None

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

        if delivery_kind == "alternative_grab":
            if not self.alternative_grab_configured:
                raise DeliveryValidationError("Refused: Alternative Grab is not configured")
            if not clean_imdb or not _IMDB_RE.fullmatch(clean_imdb):
                raise DeliveryValidationError("Refused: alternative_grab requires valid verified_imdb_id")

            bound_existing_movie_id = card_payload.get("bound_existing_movie_id")
            bound_existing_file_id = card_payload.get("bound_existing_file_id")
            raw_reasons = card_payload.get("rejection_reasons")
            # Issue #13 §4b: Never stringify non-string/unreadable rejection items;
            # pass the raw list directly to strict legacy validation.
            raw_reasons_list: list[Any] = raw_reasons if isinstance(raw_reasons, list) else []
            indexer_str = str(card_payload.get("indexer") or "").strip()
            external_id_str = str(card_payload.get("external_id") or "").strip()

            src_ok, matched_classes, src_reason = validate_legacy_alt_grab_source(
                indexer=indexer_str,
                external_id=external_id_str,
                exact_release_name=exact_release_name,
                title=verified_title,
                year=clean_year,
                rejections=raw_reasons_list,
            )
            if not src_ok:
                raise DeliveryValidationError(f"Refused: alternative_grab source/rejection check failed ({src_reason})")
            reasons_list = [r.strip() for r in raw_reasons_list]
            bound_rejection_class = matched_classes[0]

            mf_snap = card_payload.get("bound_movie_file_snapshot")
            if not isinstance(mf_snap, dict):
                raise DeliveryValidationError("Refused: alternative_grab requires bound_movie_file_snapshot")
            if mf_snap.get("monitored") is not True:
                raise DeliveryValidationError("Refused: alternative_grab requires a monitored Radarr movie")
            if (
                not isinstance(bound_existing_movie_id, int)
                or isinstance(bound_existing_movie_id, bool)
                or bound_existing_movie_id <= 0
                or not isinstance(bound_existing_file_id, int)
                or isinstance(bound_existing_file_id, bool)
                or bound_existing_file_id <= 0
                or mf_snap.get("movie_id") != bound_existing_movie_id
                or mf_snap.get("file_id") != bound_existing_file_id
            ):
                raise DeliveryValidationError(
                    "Refused: alternative_grab requires matching positive movie_id and file_id in snapshot"
                )
            for req_int in ("quality_profile_id", "size"):
                val = mf_snap.get(req_int)
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    raise DeliveryValidationError(f"Refused: alternative_grab snapshot missing valid {req_int}")
            if not isinstance(mf_snap.get("relative_path"), str) or not mf_snap["relative_path"].strip():
                raise DeliveryValidationError("Refused: alternative_grab snapshot missing relative_path")
            if not isinstance(mf_snap.get("quality"), str) or not mf_snap["quality"].strip():
                raise DeliveryValidationError("Refused: alternative_grab snapshot missing quality")

            bound_existing_file_quality = str(mf_snap["quality"]).strip()
            bound_snapshot_obj = {
                "snapshot": {
                    "movie_id": bound_existing_movie_id,
                    "file_id": bound_existing_file_id,
                    "monitored": True,
                    "quality_profile_id": int(mf_snap["quality_profile_id"]),
                    "relative_path": str(mf_snap["relative_path"]).strip(),
                    "edition": str(mf_snap.get("edition") or "").strip(),
                    "release_group": str(mf_snap.get("release_group") or "").strip(),
                    "size": int(mf_snap["size"]),
                    "quality": bound_existing_file_quality,
                },
                "rejection_reasons": reasons_list,
                "indexer": indexer_str,
                "external_id": external_id_str,
            }
            bound_snapshot_json = json.dumps(bound_snapshot_obj, sort_keys=True, separators=(",", ":"))

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
                    bound_snapshot_json=bound_snapshot_json if act_type == "alternative_grab" else None,
                )
            )
        return records, raw_tokens

    async def deliver_card(self, card_payload: dict[str, Any]) -> dict[str, Any]:
        new_actions, raw_tokens = self._validate_and_mint_actions(card_payload)

        release_id = str(card_payload["release_id"]).strip()
        card_revision = int(card_payload["card_revision"])
        delivery_kind = str(card_payload["delivery_kind"]).strip()
        preserve_eval_reply = card_payload.get("preserve_eval_reply") is True
        target_chat_id = self.config.telegram_chat_id
        if not target_chat_id:
            return {"status": "failed", "detail": "telegram_chat_id_not_configured"}
        preferred_topic = (
            str(card_payload.get("preferred_topic_kind") or "").strip() or None
            if self.alternative_grab_configured
            else None
        )
        target_thread_id = self.config.topic_for_delivery_kind(
            delivery_kind, preferred_topic_kind=preferred_topic
        )

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
                "eval_reply_preserved": bool(
                    preserve_eval_reply and plan.eval_reply_status in ("sent", "ambiguous_reply")
                ),
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

        # Path 1: Edit an existing acknowledged card on a higher verified revision / conflict downgrade / retirement
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
                eval_reply_preserved = await asyncio.to_thread(
                    self.db.finish_card_delivery_success,
                    release_id,
                    card_revision=card_revision,
                    delivery_kind=delivery_kind,
                    ack_chat_id=plan.ack_chat_id,
                    ack_message_id=plan.ack_message_id,
                    ack_thread_id=plan.ack_thread_id,
                    used_poster=plan.used_poster,
                    is_edit=True,
                    preserve_eval_reply=preserve_eval_reply,
                )
                return {
                    "status": "delivered",
                    "ack_chat_id": plan.ack_chat_id,
                    "ack_message_id": plan.ack_message_id,
                    "ack_thread_id": plan.ack_thread_id,
                    "eval_reply_preserved": eval_reply_preserved,
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
                    keep_prior_superseded=True,
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
                # Issue #13 §6c: Store ONLY the thread ID Telegram actually acknowledged.
                # Never fabricate ack_thread_id from target_thread_id when Telegram omits it.
                final_thread = photo_res.ack_thread_id
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
            # Issue #13 §6c: Store ONLY the thread ID Telegram actually acknowledged.
            final_thread = msg_res.ack_thread_id
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
        expected_thread = eval_payload.get("ack_thread_id")
        plan = await asyncio.to_thread(
            self.db.begin_eval_reply,
            release_id=release_id,
            card_revision=card_revision,
            expected_ack_chat_id=str(expected_chat) if expected_chat is not None else None,
            expected_ack_message_id=int(expected_msg) if isinstance(expected_msg, int) and not isinstance(expected_msg, bool) else None,
            expected_ack_thread_id=(
                int(expected_thread)
                if isinstance(expected_thread, int) and not isinstance(expected_thread, bool)
                else None
            ),
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
        assert plan.reply_attempt_id is not None
        reply_attempt_id = plan.reply_attempt_id
        evaluation_obj = (
            eval_payload.get("evaluation") if isinstance(eval_payload.get("evaluation"), dict) else None
        )
        formatted = format_evaluation_reply(
            release_id=release_id,
            evaluation=evaluation_obj,
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
                    card_revision=card_revision,
                    reply_attempt_id=reply_attempt_id,
                    status="failed",
                    error_redacted=msg_res.error_redacted or "telegram_reply_failed",
                )
                return {"status": "failed", "detail": msg_res.error_redacted}
            if msg_res.outcome != "ok" or not msg_res.ack_message_id:
                await asyncio.to_thread(
                    self.db.finish_eval_reply,
                    release_id,
                    card_revision=card_revision,
                    reply_attempt_id=reply_attempt_id,
                    status="ambiguous_reply",
                    error_redacted=msg_res.error_redacted or "telegram_reply_ambiguous",
                )
                return {"status": "ambiguous_reply", "detail": msg_res.error_redacted}
            reply_msg_id = msg_res.ack_message_id
            if formatted.attach_txt:
                still_owner = await asyncio.to_thread(
                    self.db.record_eval_reply_text_sent,
                    release_id,
                    card_revision=card_revision,
                    reply_attempt_id=reply_attempt_id,
                    reply_message_id=reply_msg_id,
                )
                if not still_owner:
                    return {
                        "status": "sent",
                        "reply_message_id": reply_msg_id,
                        "detail": "superseded_during_send",
                    }

        doc_msg_id: Optional[int] = None
        if formatted.attach_txt and formatted.txt_content and formatted.txt_filename:
            doc_caption = (
                "Full evaluation report"
                if evaluation_obj is not None
                else "Full sanitized evaluator error details"
            )
            doc_res = await self.telegram.send_document(
                chat_id=plan.ack_chat_id,
                filename=formatted.txt_filename,
                content_bytes=formatted.txt_content.encode("utf-8"),
                caption=doc_caption,
                thread_id=plan.ack_thread_id,
                reply_to_message_id=plan.ack_message_id,
            )
            if doc_res.outcome == "failed_definitive":
                await asyncio.to_thread(
                    self.db.finish_eval_reply,
                    release_id,
                    card_revision=card_revision,
                    reply_attempt_id=reply_attempt_id,
                    status="failed",
                    reply_message_id=reply_msg_id,
                    error_redacted=doc_res.error_redacted or "telegram_doc_failed",
                )
                return {"status": "failed", "detail": doc_res.error_redacted}
            if doc_res.outcome != "ok" or not doc_res.ack_message_id:
                await asyncio.to_thread(
                    self.db.finish_eval_reply,
                    release_id,
                    card_revision=card_revision,
                    reply_attempt_id=reply_attempt_id,
                    status="ambiguous_reply",
                    reply_message_id=reply_msg_id,
                    error_redacted=doc_res.error_redacted or "telegram_doc_ambiguous",
                )
                return {"status": "ambiguous_reply", "detail": doc_res.error_redacted}
            doc_msg_id = doc_res.ack_message_id

        updated = await asyncio.to_thread(
            self.db.finish_eval_reply,
            release_id,
            card_revision=card_revision,
            reply_attempt_id=reply_attempt_id,
            status="sent",
            reply_message_id=reply_msg_id,
            doc_message_id=doc_msg_id,
        )
        return {
            "status": "sent",
            "reply_message_id": reply_msg_id,
            "doc_message_id": doc_msg_id,
            **({"detail": "superseded_during_send"} if not updated else {}),
        }

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

        if act_type == "alternative_grab":
            outcome_note = str(done_act.get("last_error_redacted") or "queued")
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Alternative Grab Completed",
                    badge="completed_all",
                    message=(
                        f"Alternative Grab completed in qBittorrent ({outcome_note}). "
                        "No Radarr mutations or evaluator feedback writes were made."
                    ),
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

    def _parse_alt_grab_meta(self, action: dict[str, Any]) -> tuple[bool, Optional[dict[str, Any]], str]:
        raw_json = action.get("bound_snapshot_json")
        if not isinstance(raw_json, str) or not raw_json.strip():
            return False, None, "Alternative Grab action lacks saved movie/file snapshot."
        try:
            meta = json.loads(raw_json)
        except ValueError:
            return False, None, "Alternative Grab snapshot metadata is corrupted."
        if not isinstance(meta, dict):
            return False, None, "Alternative Grab snapshot metadata is invalid."
        snap = meta.get("snapshot")
        reasons = meta.get("rejection_reasons")
        indexer = str(meta.get("indexer") or "").strip()
        external_id = str(meta.get("external_id") or "").strip()
        if not isinstance(snap, dict) or not isinstance(reasons, list):
            return False, None, "Alternative Grab snapshot or rejection reasons missing."
        src_ok, _, src_reason = validate_legacy_alt_grab_source(
            indexer=indexer,
            external_id=external_id,
            exact_release_name=str(action.get("bound_release_name") or ""),
            title=str(action.get("bound_title") or ""),
            year=action.get("bound_year"),
            rejections=[str(r) for r in reasons],
        )
        if not src_ok:
            return False, None, f"Alternative Grab source/rejection check failed ({src_reason})."
        return True, {"snapshot": snap, "rejection_reasons": reasons, "indexer": indexer, "external_id": external_id}, "ok"

    async def _validate_alternative_grab_live(
        self,
        action: dict[str, Any],
    ) -> tuple[bool, Optional[dict[str, Any]], Optional[dict[str, Any]], int, str]:
        """
        Revalidate exact legacy Alternative Grab invariants against live Radarr movie/file snapshot
        and single-enabled HTTPS qBittorrent download client route.
        Returns (ok, movie, meta, http_code, message).
        """
        if self.alt_grab_config is None or self.qbit_client is None or self.torrent_fetcher is None:
            return False, None, None, 409, "Alternative Grab is not configured on this service."

        meta_ok, meta, meta_msg = self._parse_alt_grab_meta(action)
        if not meta_ok or meta is None:
            return False, None, None, 409, meta_msg

        tmdb_id = action.get("bound_tmdb_id")
        imdb_id = str(action.get("bound_imdb_id") or "").strip().lower()
        exp_movie_id = action.get("bound_existing_movie_id")
        exp_file_id = action.get("bound_existing_file_id")
        if not (
            isinstance(tmdb_id, int)
            and not isinstance(tmdb_id, bool)
            and tmdb_id > 0
            and bool(_IMDB_RE.fullmatch(imdb_id))
            and isinstance(exp_movie_id, int)
            and not isinstance(exp_movie_id, bool)
            and exp_movie_id > 0
            and isinstance(exp_file_id, int)
            and not isinstance(exp_file_id, bool)
            and exp_file_id > 0
        ):
            return False, None, None, 409, "Alternative Grab action lacks valid verified movie/file bindings."

        tags_res, movies_res, dl_res = await asyncio.gather(
            self.radarr.get_tags(),
            self.radarr.get_movies_by_tmdb(tmdb_id),
            self.radarr.get_download_clients(),
        )
        if not tags_res.ok or not movies_res.ok or not dl_res.ok:
            return False, None, None, 503, "Radarr is temporarily unreachable; cannot revalidate Alternative Grab."

        movies = [m for m in movies_res.data if isinstance(m, dict)]
        if len(movies) != 1:
            return False, None, None, 409, "Existing Radarr movie no longer matches uniquely; Alternative Grab refused."
        movie = movies[0]
        if movie.get("id") != exp_movie_id:
            return False, None, None, 409, "Bound Radarr movie ID changed; Alternative Grab refused."
        live_imdb = str(movie.get("imdbId") or "").strip().lower()
        if live_imdb != imdb_id:
            return False, None, None, 409, "Radarr movie IMDb ID does not match verified binding; Alternative Grab refused."

        watched_tag_id = _find_watched_tag_id(tags_res.data)
        m_tags = movie.get("tags") if isinstance(movie.get("tags"), list) else []
        if watched_tag_id is not None and watched_tag_id in m_tags and not bool(movie.get("monitored", False)):
            return False, None, None, 409, "Movie is now marked watched and unmonitored; Alternative Grab refused."

        current_snap = build_movie_file_snapshot(movie)
        if current_snap is None:
            return False, None, None, 409, "Radarr movie is no longer monitored with a valid existing file; Alternative Grab refused."
        snap_ok, drift_reason = compare_movie_file_snapshots(meta["snapshot"], current_snap)
        if not snap_ok:
            return (
                False,
                None,
                None,
                409,
                f"Existing Radarr movie/file changed since card creation ({drift_reason}); Alternative Grab refused.",
            )

        route_ok, route_reason = validate_radarr_qbit_route(
            dl_res.data if isinstance(dl_res.data, list) else [],
            configured_url=self.alt_grab_config.url,
            configured_category=self.alt_grab_config.category,
        )
        if not route_ok:
            return (
                False,
                None,
                None,
                409,
                f"Radarr qBittorrent route validation failed ({route_reason}); Alternative Grab refused.",
            )
        return True, movie, meta, 200, "Eligible for Alternative Grab"

    def _check_fresh_action_delivery_and_sibling_guards(
        self,
        snap: ActionGroupSnapshot,
        *,
        is_post: bool = False,
    ) -> Optional[HtmlResponseData]:
        """
        Issue #13 §2b: Before permitting a fresh (`active`) action initiation:
          1. Reject if the release's latest delivery card_revision is newer than this action's
             card_revision, or if the latest delivery was retired / downgraded to evidence_review.
          2. For `add_grab` / `alternative_grab`, reject if a sibling `watched_*` action on this
             release has already been attempted/completed/ambiguous.
        """
        action = snap.action
        if action is None or str(action["status"]) != "active":
            return None

        delivery = snap.delivery
        if delivery is not None:
            latest_rev = int(delivery.get("card_revision") or 0)
            latest_kind = str(delivery.get("delivery_kind") or "")
            act_rev = int(action.get("card_revision") or 0)
            if latest_rev > act_rev or latest_kind in ("retired", "evidence_review"):
                reason_msg = (
                    "This proposal was retired and is no longer eligible for action."
                    if latest_kind == "retired"
                    else "This card revision was superseded by a newer revision."
                )
                return HtmlResponseData(
                    status_code=410,
                    html=render_simple_status_page(
                        title="Action Superseded",
                        badge="superseded",
                        message=reason_msg,
                        tone="warn",
                    ),
                )

        action_type = str(action["action_type"])
        if action_type in ("add_grab", "alternative_grab"):
            watched_sib = next(
                (
                    a
                    for a in snap.release_actions
                    if str(a.get("action_type") or "") in ("watched_good", "watched_bad", "watched_legacy_unrated")
                    and str(a.get("status") or "")
                    in ("attempting", "ambiguous_radarr", "completed_radarr", "completed_all")
                ),
                None,
            )
            if watched_sib is not None:
                return HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Grab Blocked (Already Marked Watched)",
                        badge="409",
                        message=(
                            "This movie was already marked Watched on this card "
                            f"({watched_sib.get('action_type')}: {watched_sib.get('status')}). "
                            "No Radarr or download write was performed."
                        ),
                        tone="warn",
                    ),
                )
        return None

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
            is_alt_qbit = str(amb_act["action_type"]) == "alternative_grab"
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title=(
                        "Alternative Grab Outcome Uncertain (Reconciliation Required)"
                        if is_alt_qbit
                        else "Radarr Outcome Unconfirmed (Reconciliation Required)"
                    ),
                    badge="ambiguous_radarr",
                    message=(
                        "A previous qBittorrent submission did not return a confirmed transfer. "
                        "Click below to re-check live qBittorrent state read-only without repeating the add."
                        if is_alt_qbit
                        else "A previous Radarr request did not return a clear confirmation. "
                        "Click below to re-check live Radarr state safely without issuing duplicate release pushes."
                    ),
                    tone="warn",
                    details=details,
                    post_button_label=(
                        "Re-check qBittorrent Status & Reconcile"
                        if is_alt_qbit
                        else "Re-check Radarr Status & Reconcile"
                    ),
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
                    title="Release Push Rejected",
                    badge="rejected_push",
                    message=str(action.get("last_error_redacted") or "Release submission was refused."),
                    tone="err",
                    details=[("Release", str(action["bound_release_name"]))],
                ),
            )

        guard_resp = self._check_fresh_action_delivery_and_sibling_guards(snap, is_post=False)
        if guard_resp is not None:
            return guard_resp

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
            ok, movie, _meta, code, msg = await self._validate_alternative_grab_live(action)
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
            assert self.alt_grab_config is not None
            return HtmlResponseData(
                status_code=200,
                html=render_confirmation_page(
                    action=action,
                    radarr_summary=(
                        f"Existing monitored movie ID {movie['id']} (file ID {action['bound_existing_file_id']}, {quality_label}). "
                        f"Will submit validated .torrent bytes directly to qBittorrent category '{self.alt_grab_config.category}' (no Radarr or evaluator writes)."
                    ),
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
            if action_type == "add_grab":
                block_resp = self._check_add_grab_existing_movie_eligibility(
                    action=action,
                    existing=existing,
                    tags=tags_res.data,
                )
                if block_resp is not None:
                    return block_resp
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

        # 4b. Issue #13 §2b: Fresh initiation guard — latest revision, retired status, sibling watched
        guard_resp = self._check_fresh_action_delivery_and_sibling_guards(snap, is_post=True)
        if guard_resp is not None:
            if guard_resp.status_code == 410:
                await asyncio.to_thread(
                    self.db.mark_action_superseded,
                    action_id=str(action["action_id"]),
                    detail="stale_or_retired_delivery_revision",
                )
            return guard_resp

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

        existing, tags_list, radarr_err = await self._lookup_live_radarr_state(
            action=action,
            mutation_label="watched mutation",
            operation="watched",
            reconcile_retry_button=is_reconciliation,
        )
        if radarr_err is not None:
            return radarr_err
        assert tags_list is not None

        watched_tag_id = _find_watched_tag_id(tags_list)
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
        needs_feedback = _feedback_spec_for_action(str(action["action_type"])) is not None

        # On reconciliation: ALWAYS fetch the single authoritative movie record by ID AFTER all
        # concurrent lookup reads (TMDb, IMDb, tags) have finished before sealing recovery, so a
        # concurrent Radarr edit occurring while any lookup was pending cannot cause false completion.
        if is_reconciliation and existing is not None and isinstance(existing.get("id"), int) and not isinstance(existing.get("id"), bool):
            auth_rec = await self.radarr.get_movie_by_id(int(existing["id"]))
            if not auth_rec.ok or not isinstance(auth_rec.data, dict):
                if auth_rec.status_code == 404:
                    existing = None
                else:
                    return HtmlResponseData(
                        status_code=503,
                        html=render_simple_status_page(
                            title="Radarr Authoritative Read Failed",
                            badge="ambiguous_radarr",
                            message="Could not confirm current Radarr movie state during reconciliation; retry safely.",
                            tone="warn",
                            post_button_label="Re-check Radarr Status & Reconcile",
                            hidden_fields={"confirm": "1", "reconcile": "1"},
                        ),
                    )
            else:
                existing = dict(auth_rec.data)
                id_err = self._check_radarr_existing_movie_identity(action=action, existing=existing)
                if id_err is not None:
                    return id_err
                m_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
                if watched_tag_id in m_tags and existing.get("monitored") is False:
                    sealed = await asyncio.to_thread(
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
                    done_sib = next(
                        (a for a in refreshed.group_actions if a["status"] in ("completed_all", "completed_radarr")),
                        refreshed.action or action,
                    )
                    if sealed and needs_feedback and done_sib["status"] == "completed_radarr":
                        done_sib = await self._run_isolated_feedback(done_sib)
                    return self._render_completion_html(done_sib, action)

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
                # Delayed original POST or concurrent add won the race -> re-read with FULL
                # two-ID (TMDb + IMDb) identity verification before issuing any PUT update!
                existing, _reread_tags, reread_err = await self._lookup_live_radarr_state(
                    action=action,
                    mutation_label="Watched",
                    operation="watched",
                )
                if reread_err is not None:
                    is_unavailable = reread_err.status_code == 503
                    await asyncio.to_thread(
                        self.db.finish_write_attempt,
                        attempt_id=attempt_id,
                        action_id=str(action["action_id"]),
                        attempt_status="ambiguous" if is_unavailable else "failed_definitive",
                        action_status="ambiguous_radarr" if (is_unavailable or is_reconciliation) else "active",
                        outcome_summary=(
                            "watched_add_400_reread_failed"
                            if is_unavailable
                            else "watched_add_400_identity_conflict"
                        ),
                        last_error_redacted=(
                            "Movie already existed in Radarr but could not be re-read"
                            if is_unavailable
                            else "Movie already existed in Radarr with conflicting TMDB/IMDb identity"
                        ),
                    )
                    return reread_err
                if existing is None or not isinstance(existing.get("id"), int):
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
                movie_id = int(existing["id"])
                existing_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
                if watched_tag_id not in existing_tags or existing.get("monitored") is not False:
                    updated_payload = {
                        "monitored": False,
                        "tags": [watched_tag_id],
                        "applyTags": "add",
                    }
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
            # Update ONLY monitoring and additive watched tag (Radarr v3 PUT /api/v3/movie/editor)
            # so concurrent edits to qualityProfileId, unrelated tags, or files are never clobbered.
            updated_payload = {
                "monitored": False,
                "tags": [watched_tag_id],
                "applyTags": "add",
            }
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
            fp_obj: dict[str, Any] = {
                "tmdb_id": action.get("bound_tmdb_id"),
                "radarr_movie_id": radarr_movie_id,
                "release_name": action.get("bound_release_name"),
                "indexer": push_payload.get("indexer"),
                "action_type": action["action_type"],
            }
            safe_guid = _extract_safe_guid(push_payload.get("guid"))
            if safe_guid:
                fp_obj["guid"] = safe_guid
            fp = json.dumps(fp_obj, sort_keys=True)
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

    def _check_radarr_existing_movie_identity(
        self,
        *,
        action: dict[str, Any],
        existing: dict[str, Any],
    ) -> Optional[HtmlResponseData]:
        """
        Issue #13 §2b: Verify both TMDB and IMDb IDs where available on an existing Radarr row
        before any Radarr write so identity drift blocks mutations.
        """
        bound_tmdb = action.get("bound_tmdb_id")
        live_tmdb = existing.get("tmdbId")
        if (
            isinstance(bound_tmdb, int)
            and not isinstance(bound_tmdb, bool)
            and isinstance(live_tmdb, int)
            and not isinstance(live_tmdb, bool)
            and live_tmdb != bound_tmdb
        ):
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Radarr Identity Drift Detected",
                    badge="409",
                    message=(
                        f"Radarr movie TMDB ID ({live_tmdb}) no longer matches the verified binding ({bound_tmdb}). "
                        "Refusing Radarr write."
                    ),
                    tone="err",
                ),
            )

        bound_imdb = str(action.get("bound_imdb_id") or "").strip().lower() or None
        live_imdb = str(existing.get("imdbId") or "").strip().lower() or None
        if bound_imdb and live_imdb and live_imdb != bound_imdb:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Radarr Identity Drift Detected",
                    badge="409",
                    message=(
                        f"Radarr movie IMDb ID ({live_imdb}) no longer matches the verified binding ({bound_imdb}). "
                        "Refusing Radarr write."
                    ),
                    tone="err",
                ),
            )
        return None

    async def _lookup_live_radarr_state(
        self,
        *,
        action: dict[str, Any],
        mutation_label: str = "Radarr write",
        operation: str = "grab",
        unavailable_badge: str = "503",
        reconcile_retry_button: bool = False,
    ) -> tuple[Optional[dict[str, Any]], Optional[list[dict[str, Any]]], Optional[HtmlResponseData]]:
        """
        Query live Radarr state across BOTH bound TMDB ID and bound IMDb ID (when available)
        plus live tags. Detects cross-TMDb IMDb collisions, multi-row ambiguity, and identity
        drift before any mutation.
        - `operation="grab"`: conservatively reconciles concurrent snapshots of the same row ID
          so if either shows ownership, watched, or unmonitored status, grab is withheld.
        - `operation="watched"`: NEVER synthesizes a hybrid record from diverging snapshots; if
          concurrent TMDb/IMDb observations disagree on any field or presence, fetches the single
          authoritative record via `GET /api/v3/movie/{id}` so recovery verification and PUT
          updates operate on one coherent current movie record.
        Returns `(existing_movie_or_none, tags_list, error_response_or_none)`.
        """
        if operation not in ("grab", "watched"):
            raise ValueError(f"Unsupported Radarr lookup operation: {operation!r}")

        tmdb_id = action.get("bound_tmdb_id")
        if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool) or tmdb_id <= 0:
            return (
                None,
                None,
                HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Invalid Action", badge="409", message="Missing verified TMDB ID.", tone="err"
                    ),
                ),
            )

        bound_imdb = str(action.get("bound_imdb_id") or "").strip().lower() or None
        completion_seq = 0

        async def _timed_tmdb() -> tuple[int, RadarrReadResult]:
            nonlocal completion_seq
            res = await self.radarr.get_movies_by_tmdb(tmdb_id)
            completion_seq += 1
            return completion_seq, res

        async def _timed_imdb() -> tuple[int, RadarrReadResult]:
            nonlocal completion_seq
            assert bound_imdb is not None
            res = await self.radarr.get_movies_by_imdb(bound_imdb)
            completion_seq += 1
            return completion_seq, res

        if bound_imdb:
            (tmdb_seq, movies_tmdb_res), (imdb_seq, movies_imdb_res), tags_res = await asyncio.gather(
                _timed_tmdb(),
                _timed_imdb(),
                self.radarr.get_tags(),
            )
            reads_ok = movies_tmdb_res.ok and movies_imdb_res.ok and tags_res.ok
            ordered_batches = (
                [movies_tmdb_res.data or [], movies_imdb_res.data or []]
                if tmdb_seq <= imdb_seq
                else [movies_imdb_res.data or [], movies_tmdb_res.data or []]
            )
        else:
            (_tmdb_seq, movies_tmdb_res), tags_res = await asyncio.gather(
                _timed_tmdb(),
                self.radarr.get_tags(),
            )
            movies_imdb_res = None
            reads_ok = movies_tmdb_res.ok and tags_res.ok
            ordered_batches = [movies_tmdb_res.data or []]

        if not reads_ok:
            return (
                None,
                None,
                HtmlResponseData(
                    status_code=503,
                    html=render_simple_status_page(
                        title="Radarr Unavailable",
                        badge=unavailable_badge,
                        message="Radarr could not be reached; aborting before any write.",
                        tone="warn",
                        post_button_label="Re-check Radarr Status & Reconcile" if reconcile_retry_button else None,
                        hidden_fields={"confirm": "1", "reconcile": "1"} if reconcile_retry_button else None,
                    ),
                ),
            )

        deduped: dict[Any, dict[str, Any]] = {}
        observed_counts: dict[Any, int] = {}
        watched_snapshot_diverged = False
        fallback_idx = 0
        conservative_for_grab = operation == "grab"
        for batch in ordered_batches:
            for m in batch:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id")
                key = ("id", int(mid)) if isinstance(mid, int) and not isinstance(mid, bool) else ("idx", fallback_idx)
                if key[0] == "idx":
                    fallback_idx += 1
                if key not in deduped:
                    deduped[key] = m
                    observed_counts[key] = 1
                else:
                    observed_counts[key] = observed_counts.get(key, 1) + 1
                    if not conservative_for_grab and deduped[key] != m:
                        watched_snapshot_diverged = True
                    reconciled, drift = _reconcile_radarr_row_snapshots(
                        deduped[key],
                        m,
                        conservative_for_grab=conservative_for_grab,
                    )
                    if drift is not None or reconciled is None:
                        return (
                            None,
                            None,
                            HtmlResponseData(
                                status_code=409,
                                html=render_simple_status_page(
                                    title="Ambiguous Radarr Match",
                                    badge="ambiguous",
                                    message=f"Radarr movie identity changed across concurrent TMDB/IMDb lookups ({drift}); refusing {mutation_label}.",
                                    tone="err",
                                ),
                            ),
                        )
                    deduped[key] = reconciled

        matches = list(deduped.values())
        if len(matches) > 1:
            return (
                None,
                None,
                HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Ambiguous Radarr Match",
                        badge="ambiguous",
                        message=f"Multiple Radarr movies match this release's TMDB/IMDb IDs; refusing {mutation_label}.",
                        tone="err",
                    ),
                ),
            )

        existing = matches[0] if matches else None
        if existing is not None:
            id_err = self._check_radarr_existing_movie_identity(action=action, existing=existing)
            if id_err is not None:
                return None, None, id_err

        # PR #18 Round 4 Findings 1 & 2: For Watched operations, whenever concurrent TMDb/IMDb
        # reads returned diverging snapshots (or disagreed on row presence), fetch the single
        # authoritative record by movie ID after both reads finish so recovery verification and
        # PUT updates never act on a synthetic or stale snapshot.
        if operation == "watched" and existing is not None:
            mid = existing.get("id")
            only_one_batch_found = (
                bound_imdb is not None
                and isinstance(mid, int)
                and not isinstance(mid, bool)
                and observed_counts.get(("id", int(mid)), 0) < 2
            )
            if (watched_snapshot_diverged or only_one_batch_found) and isinstance(mid, int) and not isinstance(mid, bool):
                auth_res = await self.radarr.get_movie_by_id(int(mid))
                if not auth_res.ok or not isinstance(auth_res.data, dict):
                    if auth_res.status_code == 404:
                        existing = None
                    else:
                        return (
                            None,
                            None,
                            HtmlResponseData(
                                status_code=503,
                                html=render_simple_status_page(
                                    title="Radarr Authoritative Read Failed",
                                    badge=unavailable_badge,
                                    message="Concurrent Radarr lookups diverged and the authoritative movie read could not be confirmed; retry safely.",
                                    tone="warn",
                                    post_button_label="Re-check Radarr Status & Reconcile" if reconcile_retry_button else None,
                                    hidden_fields={"confirm": "1", "reconcile": "1"} if reconcile_retry_button else None,
                                ),
                            ),
                        )
                else:
                    existing = dict(auth_res.data)
                    id_err = self._check_radarr_existing_movie_identity(action=action, existing=existing)
                    if id_err is not None:
                        return None, None, id_err

        tags_list = [t for t in (tags_res.data or []) if isinstance(t, dict)]
        return existing, tags_list, None

    def _check_add_grab_existing_movie_eligibility(
        self,
        *,
        action: dict[str, Any],
        existing: dict[str, Any],
        tags: list[dict[str, Any]],
    ) -> Optional[HtmlResponseData]:
        """
        Issue #13 §2b & PR #18 Blocker 2: Final eligibility checks for ordinary Add & Grab against live Radarr state:
          1. Bound TMDB + IMDb IDs match (no identity drift).
          2. Movie does NOT already own a file (`hasFile`, positive `movieFileId`, `movieFile`, or `sizeOnDisk > 0`).
          3. Movie is NOT tagged `watched` and unmonitored.
        A monitored movie with no file (and matching IDs, not watched+unmonitored) remains eligible.
        """
        id_err = self._check_radarr_existing_movie_identity(action=action, existing=existing)
        if id_err is not None:
            return id_err

        if _movie_has_file_evidence(existing):
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Movie Already Owned in Radarr",
                    badge="409",
                    message=(
                        f"Radarr movie ID {existing.get('id')} already has a file on disk. "
                        "Ordinary Add & Grab is refused; zero Radarr writes were performed."
                    ),
                    tone="warn",
                ),
            )

        watched_tag_id = _find_watched_tag_id(tags)
        m_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
        has_watched = watched_tag_id is not None and watched_tag_id in m_tags
        if has_watched and not bool(existing.get("monitored")):
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Movie Already Watched & Unmonitored",
                    badge="409",
                    message=(
                        f"Radarr movie ID {existing.get('id')} is tagged 'watched' and unmonitored. "
                        "Add & Grab is refused; zero Radarr writes were performed."
                    ),
                    tone="warn",
                ),
            )
        return None

    async def _verify_pre_first_push_eligibility(
        self,
        *,
        action: dict[str, Any],
        require_movie_present: bool = True,
        unavailable_badge: str = "503",
        reconcile_retry_button: bool = False,
    ) -> tuple[Optional[dict[str, Any]], Optional[HtmlResponseData]]:
        """
        Single unified eligibility decision applied immediately before the FIRST release push
        in EVERY route (fresh Add & Grab after Phase 1 or on existing movie, and Phase A timeout recovery).
        Verifies latest delivery revision, retirement/downgrade, expiry, conflicting sibling Watched
        actions, and fresh live Radarr IDs / monitoring / watched tags / file ownership.
        """
        fresh_snap = await asyncio.to_thread(self.db.get_action_group_readonly, str(action["token_sha256"]))
        fresh_act = fresh_snap.action or action

        delivery = fresh_snap.delivery
        if delivery is not None:
            latest_rev = int(delivery.get("card_revision") or 0)
            latest_kind = str(delivery.get("delivery_kind") or "")
            act_rev = int(fresh_act.get("card_revision") or 0)
            if latest_rev > act_rev or latest_kind in ("retired", "evidence_review"):
                msg = (
                    "This proposal was retired before release push; zero push was sent."
                    if latest_kind == "retired"
                    else "This card was superseded by a newer revision before release push; zero push was sent."
                )
                return (
                    None,
                    HtmlResponseData(
                        status_code=410,
                        html=render_simple_status_page(
                            title="Action Superseded Before Push",
                            badge="superseded",
                            message=msg,
                            tone="warn",
                        ),
                    ),
                )

        if self._is_expired(fresh_act):
            return (
                None,
                HtmlResponseData(
                    status_code=410,
                    html=render_simple_status_page(
                        title="Action Expired Before Push",
                        badge="expired",
                        message="This action link expired before release push; zero push was sent.",
                        tone="warn",
                    ),
                ),
            )

        watched_sib = next(
            (
                a
                for a in fresh_snap.release_actions
                if str(a.get("action_type") or "") in ("watched_good", "watched_bad", "watched_legacy_unrated")
                and str(a.get("status") or "")
                in ("attempting", "ambiguous_radarr", "completed_radarr", "completed_all")
            ),
            None,
        )
        if watched_sib is not None:
            return (
                None,
                HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Release Push Blocked (Marked Watched)",
                        badge="409",
                        message=(
                            "This movie was marked Watched on this card "
                            f"({watched_sib.get('action_type')}: {watched_sib.get('status')}). "
                            "Release push was aborted; zero push was sent."
                        ),
                        tone="warn",
                    ),
                ),
            )

        existing, tags_list, radarr_err = await self._lookup_live_radarr_state(
            action=fresh_act,
            mutation_label="release push",
            unavailable_badge=unavailable_badge,
            reconcile_retry_button=reconcile_retry_button,
        )
        if radarr_err is not None:
            return None, radarr_err
        assert tags_list is not None

        if existing is None:
            if require_movie_present:
                return (
                    None,
                    HtmlResponseData(
                        status_code=409,
                        html=render_simple_status_page(
                            title="Movie Missing Before Release Push",
                            badge="409",
                            message="Expected movie could not be verified in Radarr before release push; aborting push.",
                            tone="warn",
                        ),
                    ),
                )
            return None, None

        elig_err = self._check_add_grab_existing_movie_eligibility(
            action=fresh_act,
            existing=existing,
            tags=tags_list,
        )
        if elig_err is not None:
            return None, elig_err

        return existing, None

    async def _execute_add_grab(
        self,
        action: dict[str, Any],
        form_data: dict[str, str],
        *,
        is_phase1_retry: bool = False,
    ) -> HtmlResponseData:
        """Two-phase Add & Grab: pre-write checks -> Phase 1 add movie (if absent) -> unified pre-push check -> Phase 2 release push."""
        tmdb_id = action.get("bound_tmdb_id")
        if not isinstance(tmdb_id, int) or tmdb_id <= 0:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Invalid Action", badge="409", message="Missing verified TMDB ID.", tone="err"
                ),
            )

        # Pre-write check 1: lookup live movie across BOTH TMDB + IMDb & tags in Radarr
        existing, tags_list, radarr_err = await self._lookup_live_radarr_state(
            action=action,
            mutation_label="Add & Grab",
        )
        if radarr_err is not None:
            return radarr_err
        assert tags_list is not None

        if existing is not None:
            elig_err = self._check_add_grab_existing_movie_eligibility(
                action=action,
                existing=existing,
                tags=tags_list,
            )
            if elig_err is not None:
                return elig_err

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
                reread_movie, reread_tags, reread_err = await self._lookup_live_radarr_state(
                    action=action,
                    mutation_label="Add & Grab",
                )
                if reread_err is not None:
                    is_unavailable = reread_err.status_code == 503
                    await asyncio.to_thread(
                        self.db.finish_write_attempt,
                        attempt_id=att1_id,
                        action_id=str(action["action_id"]),
                        attempt_status="ambiguous" if is_unavailable else "failed_definitive",
                        action_status="ambiguous_radarr" if (is_unavailable or is_phase1_retry) else "active",
                        outcome_summary="add_400_reread_failed" if is_unavailable else "already_exists_ineligible",
                        last_error_redacted="Existing Radarr movie failed post-add verification",
                    )
                    return reread_err
                if reread_movie is not None and reread_tags is not None:
                    elig_err = self._check_add_grab_existing_movie_eligibility(
                        action=action,
                        existing=reread_movie,
                        tags=reread_tags,
                    )
                    if elig_err is not None:
                        await asyncio.to_thread(
                            self.db.finish_write_attempt,
                            attempt_id=att1_id,
                            action_id=str(action["action_id"]),
                            attempt_status="failed_definitive",
                            action_status="ambiguous_radarr" if is_phase1_retry else "active",
                            outcome_summary="already_exists_ineligible",
                            last_error_redacted="Existing Radarr movie failed post-add eligibility checks",
                        )
                        return elig_err
                    radarr_movie_id = int(reread_movie["id"])
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

            # PR #18 Blocker 2: Unified final eligibility check IMMEDIATELY BEFORE opening
            # the first release push (catches concurrent Watched, retirement, or live Radarr drift
            # that occurred while radarr_add_movie was in flight!).
            assert radarr_movie_id is not None
            live_post_add, pre_push_err = await self._verify_pre_first_push_eligibility(
                action=action,
                require_movie_present=True,
            )
            if pre_push_err is not None or live_post_add is None:
                is_transient = pre_push_err is not None and pre_push_err.status_code == 503
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=att1_id,
                    action_id=str(action["action_id"]),
                    attempt_status="succeeded",
                    action_status="ambiguous_radarr" if is_transient else "superseded",
                    outcome_summary=f"{phase1_outcome};blocked_before_push",
                    radarr_movie_id=radarr_movie_id,
                    last_error_redacted="Release push aborted by pre-push eligibility check",
                )
                assert pre_push_err is not None
                return pre_push_err
            radarr_movie_id = int(live_post_add["id"])

            # Atomically close Phase 1 and open Phase 2 in ONE transaction (re-verifies delivery
            # revision, retirement, expiry, and sibling Watched inside BEGIN IMMEDIATE!)
            fp2_obj: dict[str, Any] = {
                "tmdb_id": tmdb_id,
                "radarr_movie_id": radarr_movie_id,
                "release_name": action.get("bound_release_name"),
                "indexer": push_payload.get("indexer"),
                "action_type": action["action_type"],
            }
            safe_guid2 = _extract_safe_guid(push_payload.get("guid"))
            if safe_guid2:
                fp2_obj["guid"] = safe_guid2
            fp2 = json.dumps(fp2_obj, sort_keys=True)
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
            if preopened_phase2_attempt_id is None:
                return HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Push Conflict",
                        badge="409",
                        message="Action was retired, superseded, marked Watched, or already claimed before release push; zero push sent.",
                        tone="warn",
                    ),
                )
        else:
            # Movie already existed: apply the same unified pre-first-push check right before Phase 2
            live_pre_push, pre_push_err = await self._verify_pre_first_push_eligibility(
                action=action,
                require_movie_present=True,
            )
            if pre_push_err is not None or live_pre_push is None:
                assert pre_push_err is not None
                return pre_push_err
            radarr_movie_id = int(live_pre_push["id"])

        # Phase 2: radarr_release_push (ONLY announced release)
        assert radarr_movie_id is not None
        return await self._execute_release_push_phase(
            action=action,
            radarr_movie_id=radarr_movie_id,
            push_payload=push_payload,
            allowed_action_statuses=phase2_allowed,
            preopened_attempt_id=preopened_phase2_attempt_id,
        )

    async def _confirm_qbit_transfer_bounded(
        self,
        qbit_http: Any,
        *,
        info_hash: str,
        expected_category: str,
        attempts: int = 3,
    ) -> tuple[str, Optional[str]]:
        """
        Bounded read-only verification against qBittorrent GET /api/v2/torrents/info?hashes=<hash>.
        Returns ('queued', None) iff exact hash exists in expected category,
        ('wrong_category', found_cat) if hash exists in another category,
        or ('absent', err_or_none) if not confirmed within bounded checks.
        """
        assert self.qbit_client is not None
        last_err: Optional[str] = None
        for idx in range(attempts):
            info_res = await self.qbit_client.get_torrent_info(qbit_http, info_hash)
            if info_res.ok and isinstance(info_res.data, list):
                for entry in info_res.data:
                    if isinstance(entry, dict) and str(entry.get("hash") or "").strip().lower() == info_hash:
                        found_cat = str(entry.get("category") or "").strip()
                        if found_cat == expected_category:
                            return "queued", None
                        return "wrong_category", found_cat
            else:
                last_err = info_res.error_redacted or "qbit_info_failed"
            if idx + 1 < attempts:
                await asyncio.sleep(0.15)
        return "absent", last_err

    async def _execute_alternative_grab(
        self,
        action: dict[str, Any],
        form_data: dict[str, str],
    ) -> HtmlResponseData:
        """
        Legacy Alternative Grab ("Grab Anyway") execution via HTTPS qBittorrent Web API v2:
        - Takes exclusive action lock (qbit_alt_preflight -> atomic handoff -> qbit_alternative_grab).
        - Validates source, 1-8 rejection rules, live Radarr movie/file snapshot, and single enabled qBit route
          BEFORE AND AFTER fetching the bound canonical TorrentLeech .torrent bytes (<= 4 MB, no redirects, v1 hash).
        - Uses a fresh qBittorrent session (never reusing tracker cookies), checks already-present, category,
          and autorun disabled, writes a durable 0600 receipt keyed to tracker external_id, submits ONE multipart
          bytes-only POST /api/v2/torrents/add (no URLs, no pause/start override), and confirms via bounded read-only
          torrents/info checks. Zero Radarr writes and zero evaluator /feedback writes.
        """
        if self.alt_grab_config is None or self.qbit_client is None or self.torrent_fetcher is None:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Alternative Grab Unavailable",
                    badge="refused",
                    message="Alternative Grab is not configured on this service.",
                    tone="err",
                ),
            )
        cfg = self.alt_grab_config

        # 1. Acquire exclusive preflight action lock immediately
        lock_ok, preflight_att_id, lock_reason = await asyncio.to_thread(
            self.db.begin_write_attempt,
            action_id=str(action["action_id"]),
            completion_group=str(action["completion_group"]),
            phase="qbit_alt_preflight",
            process_instance_id=self.process_instance_id,
            lease_expires_at=self._lease_iso(self.config.radarr_timeout_seconds * 2),
            request_fingerprint=json.dumps(
                {"action_id": action["action_id"], "release_id": action["release_id"], "phase": "preflight"},
                sort_keys=True,
            ),
            allowed_action_statuses=frozenset({"active"}),
        )
        if not lock_ok or not preflight_att_id:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Alternative Grab Conflict",
                    badge="409",
                    message=f"Alternative Grab cannot start ({lock_reason}). Refresh to view current status.",
                    tone="warn",
                ),
            )

        async def _release_preflight(
            *,
            http_code: int,
            title: str,
            badge: str,
            message: str,
            action_status: str = "active",
            tone: str = "err",
        ) -> HtmlResponseData:
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=preflight_att_id,
                action_id=str(action["action_id"]),
                attempt_status="failed_definitive",
                action_status=action_status,
                outcome_summary=badge,
                last_error_redacted=message,
            )
            return HtmlResponseData(
                status_code=http_code,
                html=render_simple_status_page(title=title, badge=badge, message=message, tone=tone),
            )

        qbit_http: Any = None
        owns_qbit = False
        try:
            # 2. Pre-fetch live Radarr movie/file snapshot + download-client route revalidation
            ok1, movie1, meta, code1, msg1 = await self._validate_alternative_grab_live(action)
            if not ok1 or movie1 is None or meta is None:
                return await _release_preflight(
                    http_code=code1,
                    title="Alternative Grab Refused",
                    badge="refused" if code1 == 409 else str(code1),
                    message=msg1,
                    tone="err" if code1 == 409 else "warn",
                )
            external_id = str(meta["external_id"])
            movie_id = int(movie1["id"])

            # 3. Check durable private receipt keyed to tracker external_id BEFORE any fetch/add
            try:
                existing_receipt = await asyncio.to_thread(read_alt_grab_receipt, cfg.state_dir, external_id)
            except (PrivateFileError, OSError, ValueError) as exc:
                return await _release_preflight(
                    http_code=503,
                    title="Alternative Grab Receipt Store Unsafe",
                    badge="refused",
                    message=f"Refused: private receipt directory check failed ({redact_secrets(exc.__class__.__name__, max_chars=80)}).",
                )
            if existing_receipt and existing_receipt.get("status") in ("submitting", "uncertain", "queued", "already_present"):
                prior_status = str(existing_receipt.get("status") or "")
                prior_hash = str(existing_receipt.get("info_hash") or "").strip().lower()
                if prior_hash:
                    qbit_http, owns_qbit, login_res = await self.qbit_client.open_authenticated_session()
                    if login_res.ok:
                        c_state, _ = await self._confirm_qbit_transfer_bounded(
                            qbit_http, info_hash=prior_hash, expected_category=cfg.category, attempts=1
                        )
                        if c_state == "queued" or (
                            c_state == "wrong_category" and prior_status == "already_present"
                        ):
                            outcome_lbl = "queued" if c_state == "queued" else "already_present"
                            await asyncio.to_thread(
                                self.db.finish_write_attempt,
                                attempt_id=preflight_att_id,
                                action_id=str(action["action_id"]),
                                attempt_status="succeeded",
                                action_status="completed_all",
                                outcome_summary=outcome_lbl,
                                radarr_movie_id=movie_id,
                                last_error_redacted=outcome_lbl,
                            )
                            return HtmlResponseData(
                                status_code=200,
                                html=render_simple_status_page(
                                    title="Alternative Grab Already Recorded",
                                    badge=outcome_lbl,
                                    message=f"Existing receipt confirmed in qBittorrent ({outcome_lbl}); zero duplicate adds sent.",
                                    tone="ok",
                                ),
                            )
                # Prior submission receipt exists (even if hash missing or unconfirmed) -> mark uncertain without re-adding
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=preflight_att_id,
                    action_id=str(action["action_id"]),
                    attempt_status="ambiguous",
                    action_status="ambiguous_radarr",
                    outcome_summary="uncertain_prior_receipt",
                    radarr_movie_id=movie_id,
                    last_error_redacted="Prior receipt exists for this tracker release; refusing duplicate submission.",
                )
                return HtmlResponseData(
                    status_code=503,
                    html=render_simple_status_page(
                        title="Alternative Grab Uncertain",
                        badge="uncertain",
                        message="A prior submission receipt exists for this tracker release. Re-check qBittorrent status rather than re-submitting.",
                        tone="warn",
                        post_button_label="Re-check qBittorrent Status & Reconcile",
                        hidden_fields={"confirm": "1", "reconcile": "1"},
                    ),
                )

            # 4. Resolve ephemeral bound download URL and fetch + validate v1 .torrent bytes (<= 4 MB, no redirects)
            payload_res = await self.intake_push.fetch_push_payload(
                release_id=str(action["release_id"]),
                source_occurrence_id=str(action["source_occurrence_id"]),
            )
            if not payload_res.ok or not payload_res.push_payload:
                return await _release_preflight(
                    http_code=503,
                    title="Release Payload Unavailable",
                    badge="503",
                    message="Could not resolve verified release payload from intake archive. No download-client write was attempted.",
                    tone="warn",
                )
            dl_url = str(payload_res.push_payload.get("downloadUrl") or "").strip()
            fetch_res = await self.torrent_fetcher.fetch_bound_torrent(
                download_url=dl_url, external_id=external_id
            )
            if not fetch_res.ok or not fetch_res.torrent_bytes or not fetch_res.info_hash:
                return await _release_preflight(
                    http_code=409,
                    title="Alternative Grab Refused",
                    badge="refused",
                    message=f"Bound torrent fetch/validation failed ({fetch_res.error_redacted or 'invalid_torrent'}). Zero qBittorrent writes made.",
                )
            torrent_bytes = fetch_res.torrent_bytes
            info_hash = fetch_res.info_hash

            # 5. Post-fetch revalidation (exact legacy invariant before any submission!)
            ok2, movie2, _meta2, code2, msg2 = await self._validate_alternative_grab_live(action)
            if not ok2 or movie2 is None:
                return await _release_preflight(
                    http_code=code2,
                    title="Alternative Grab Refused",
                    badge="refused" if code2 == 409 else str(code2),
                    message=f"Post-fetch revalidation failed: {msg2}",
                    tone="err" if code2 == 409 else "warn",
                )

            # 6. Open fresh isolated HTTPS qBittorrent session (never sharing tracker cookies)
            qbit_http, owns_qbit, login_res = await self.qbit_client.open_authenticated_session()
            if not login_res.ok:
                return await _release_preflight(
                    http_code=503,
                    title="qBittorrent Unavailable",
                    badge="503",
                    message=f"Could not authenticate to qBittorrent ({login_res.error_redacted or 'login_failed'}). Zero writes made.",
                    tone="warn",
                )

            # Check already present by v1 info hash (leave existing transfer & category completely untouched!)
            info_pre = await self.qbit_client.get_torrent_info(qbit_http, info_hash)
            if not info_pre.ok:
                return await _release_preflight(
                    http_code=503,
                    title="qBittorrent Unavailable",
                    badge="503",
                    message=f"Could not query qBittorrent torrents/info ({info_pre.error_redacted}). Zero writes made.",
                    tone="warn",
                )
            if any(
                isinstance(item, dict) and str(item.get("hash") or "").strip().lower() == info_hash
                for item in (info_pre.data or [])
            ):
                try:
                    await asyncio.to_thread(
                        write_alt_grab_receipt,
                        cfg.state_dir,
                        external_id,
                        {
                            "tracker": "torrentleech",
                            "external_id": external_id,
                            "release_id": str(action["release_id"]),
                            "action_id": str(action["action_id"]),
                            "info_hash": info_hash,
                            "category": cfg.category,
                            "status": "already_present",
                            "updated_at": utc_now_iso(),
                        },
                    )
                except (PrivateFileError, OSError):
                    pass
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=preflight_att_id,
                    action_id=str(action["action_id"]),
                    attempt_status="succeeded",
                    action_status="completed_all",
                    outcome_summary="already_present",
                    radarr_movie_id=movie_id,
                    last_error_redacted="already_present",
                )
                return HtmlResponseData(
                    status_code=200,
                    html=render_simple_status_page(
                        title="Already Present in qBittorrent",
                        badge="already_present",
                        message="Torrent is already present in qBittorrent; existing transfer and category were left untouched.",
                        tone="ok",
                    ),
                )

            # Confirm fixed Radarr category exists in qBittorrent
            cats_res = await self.qbit_client.get_categories(qbit_http)
            if not cats_res.ok or not isinstance(cats_res.data, dict):
                return await _release_preflight(
                    http_code=503,
                    title="qBittorrent Unavailable",
                    badge="503",
                    message=f"Could not read qBittorrent categories ({cats_res.error_redacted}). Zero writes made.",
                    tone="warn",
                )
            if cfg.category not in cats_res.data:
                return await _release_preflight(
                    http_code=409,
                    title="Alternative Grab Refused",
                    badge="refused",
                    message=f"Configured category '{cfg.category}' does not exist in qBittorrent; Alternative Grab refused.",
                )

            # Confirm qBittorrent autorun hooks are positively disabled (Issue #13 §4b)
            prefs_res = await self.qbit_client.get_preferences(qbit_http)
            if not prefs_res.ok or not isinstance(prefs_res.data, dict):
                return await _release_preflight(
                    http_code=503,
                    title="qBittorrent Unavailable",
                    badge="503",
                    message=f"Could not read qBittorrent preferences ({prefs_res.error_redacted}). Zero writes made.",
                    tone="warn",
                )
            autorun_enabled = prefs_res.data.get("autorun_enabled")
            autorun_on_add = prefs_res.data.get("autorun_on_torrent_added_enabled")
            if autorun_enabled is not False or autorun_on_add is not False:
                return await _release_preflight(
                    http_code=409,
                    title="Alternative Grab Refused",
                    badge="refused",
                    message=(
                        "qBittorrent preferences did not positively confirm both "
                        "'autorun_enabled=false' and 'autorun_on_torrent_added_enabled=false'; "
                        "Alternative Grab refused."
                    ),
                )

            # 7. Write durable 0600 receipt keyed to external_id BEFORE opening qbit_alternative_grab attempt
            receipt_base = {
                "tracker": "torrentleech",
                "external_id": external_id,
                "release_id": str(action["release_id"]),
                "action_id": str(action["action_id"]),
                "info_hash": info_hash,
                "category": cfg.category,
                "status": "submitting",
                "updated_at": utc_now_iso(),
            }
            try:
                await asyncio.to_thread(write_alt_grab_receipt, cfg.state_dir, external_id, receipt_base)
            except (PrivateFileError, OSError) as exc:
                return await _release_preflight(
                    http_code=503,
                    title="Alternative Grab Receipt Failed",
                    badge="refused",
                    message=f"Could not write durable private receipt before submission ({redact_secrets(exc.__class__.__name__, max_chars=80)}). Zero qBittorrent writes made.",
                )

            # Atomic lock handoff: qbit_alt_preflight (succeeded) -> qbit_alternative_grab (attempting)
            fp_qbit = json.dumps(
                {"info_hash_prefix": info_hash[:12], "category": cfg.category, "external_id": external_id},
                sort_keys=True,
            )
            add_att_id = await asyncio.to_thread(
                self.db.complete_alt_preflight_and_begin_qbit_add,
                preflight_attempt_id=preflight_att_id,
                action_id=str(action["action_id"]),
                completion_group=str(action["completion_group"]),
                radarr_movie_id=movie_id,
                preflight_outcome=f"preflight_ok:{info_hash[:12]}",
                process_instance_id=self.process_instance_id,
                qbit_lease_expires_at=self._lease_iso(self.config.radarr_timeout_seconds),
                qbit_request_fingerprint=fp_qbit,
            )
            if add_att_id is None:
                return HtmlResponseData(
                    status_code=409,
                    html=render_simple_status_page(
                        title="Alternative Grab Conflict",
                        badge="409",
                        message="Another qBittorrent submission already exists or preflight claim was superseded; refusing duplicate add.",
                        tone="warn",
                    ),
                )

            # 8. Submit ONE multipart POST /api/v2/torrents/add (validated bytes + fixed category ONLY)
            add_res = await self.qbit_client.add_torrent_bytes(
                qbit_http,
                torrent_bytes=torrent_bytes,
                category=cfg.category,
                filename=f"{external_id}.torrent",
            )
            if add_res.outcome in ("failed_not_transmitted", "refused"):
                final_status = "rejected_push" if add_res.outcome == "refused" else "active"
                try:
                    await asyncio.to_thread(
                        write_alt_grab_receipt,
                        cfg.state_dir,
                        external_id,
                        dict(receipt_base, status="refused", updated_at=utc_now_iso()),
                    )
                except (PrivateFileError, OSError):
                    pass
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=add_att_id,
                    action_id=str(action["action_id"]),
                    attempt_status="failed_definitive",
                    action_status=final_status,
                    outcome_summary=add_res.error_redacted or "qbit_add_refused",
                    radarr_movie_id=movie_id,
                    last_error_redacted=add_res.error_redacted or "qBittorrent refused torrent submission.",
                )
                return HtmlResponseData(
                    status_code=502 if add_res.outcome == "failed_not_transmitted" else 200,
                    html=render_simple_status_page(
                        title="Alternative Grab Refused by qBittorrent",
                        badge="refused",
                        message=str(add_res.error_redacted or "qBittorrent refused the torrent submission."),
                        tone="err",
                    ),
                )

            # 9. Bounded read-only confirmation (even after a lost HTTP ack!)
            confirm_state, confirm_detail = await self._confirm_qbit_transfer_bounded(
                qbit_http, info_hash=info_hash, expected_category=cfg.category, attempts=3
            )
            if confirm_state == "queued":
                try:
                    await asyncio.to_thread(
                        write_alt_grab_receipt,
                        cfg.state_dir,
                        external_id,
                        dict(receipt_base, status="queued", updated_at=utc_now_iso()),
                    )
                except (PrivateFileError, OSError):
                    pass
                await asyncio.to_thread(
                    self.db.finish_write_attempt,
                    attempt_id=add_att_id,
                    action_id=str(action["action_id"]),
                    attempt_status="succeeded",
                    action_status="completed_all",
                    outcome_summary="queued",
                    radarr_movie_id=movie_id,
                    last_error_redacted="queued",
                )
                return HtmlResponseData(
                    status_code=200,
                    html=render_simple_status_page(
                        title="Alternative Grab Queued in qBittorrent",
                        badge="queued",
                        message=(
                            f"Validated torrent queued in qBittorrent category '{cfg.category}'. "
                            "No Radarr mutations or evaluator feedback writes were made."
                        ),
                        tone="ok",
                        details=[
                            ("Movie", str(action["bound_title"])),
                            ("Release", str(action["bound_release_name"])),
                            ("Category", cfg.category),
                        ],
                    ),
                )

            # Unconfirmed / lost ack -> uncertain (NEVER auto-repeat torrents/add!)
            uncertain_note = (
                f"qbit_transfer_wrong_category:{confirm_detail}"
                if confirm_state == "wrong_category"
                else (add_res.error_redacted or confirm_detail or "qbit_transfer_not_confirmed_in_category")
            )
            try:
                await asyncio.to_thread(
                    write_alt_grab_receipt,
                    cfg.state_dir,
                    external_id,
                    dict(receipt_base, status="uncertain", updated_at=utc_now_iso()),
                )
            except (PrivateFileError, OSError):
                pass
            await asyncio.to_thread(
                self.db.finish_write_attempt,
                attempt_id=add_att_id,
                action_id=str(action["action_id"]),
                attempt_status="ambiguous",
                action_status="ambiguous_radarr",
                outcome_summary="uncertain",
                radarr_movie_id=movie_id,
                last_error_redacted=uncertain_note,
            )
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="Alternative Grab Outcome Uncertain",
                    badge="uncertain",
                    message=(
                        "qBittorrent did not confirm the transfer in the expected category. "
                        "Submission will NOT be automatically repeated; re-check live qBittorrent status below."
                    ),
                    tone="warn",
                    details=[("Detail", uncertain_note)],
                    post_button_label="Re-check qBittorrent Status & Reconcile",
                    hidden_fields={"confirm": "1", "reconcile": "1"},
                ),
            )
        finally:
            if owns_qbit and qbit_http is not None:
                await qbit_http.aclose()

    async def _reconcile_ambiguous_qbit_alt_grab(
        self,
        amb_act: dict[str, Any],
        qbit_att: Optional[dict[str, Any]],
        preflight_att: Optional[dict[str, Any]],
    ) -> HtmlResponseData:
        """Read-only reconciliation for uncertain qBittorrent Alternative Grab (zero duplicate adds)."""
        if self.alt_grab_config is None or self.qbit_client is None:
            return HtmlResponseData(
                status_code=409,
                html=render_simple_status_page(
                    title="Cannot Reconcile Alternative Grab",
                    badge="409",
                    message="Alternative Grab qBittorrent client is not configured.",
                    tone="err",
                ),
            )
        cfg = self.alt_grab_config

        # If ONLY preflight crashed (no qbit_alternative_grab attempt was ever opened), check receipt first
        if qbit_att is None and preflight_att is not None:
            meta_ok, meta, _ = self._parse_alt_grab_meta(amb_act)
            ext_id = str(meta["external_id"]) if (meta_ok and meta) else ""
            prior_receipt = None
            if ext_id:
                try:
                    prior_receipt = await asyncio.to_thread(read_alt_grab_receipt, cfg.state_dir, ext_id)
                except (PrivateFileError, OSError, ValueError):
                    prior_receipt = None
            if not prior_receipt or prior_receipt.get("status") not in ("submitting", "uncertain", "queued", "already_present"):
                if self._is_expired(amb_act):
                    await asyncio.to_thread(
                        self.db.mark_action_expired,
                        action_id=str(amb_act["action_id"]),
                        detail="expired_after_interrupted_preflight",
                    )
                    return HtmlResponseData(
                        status_code=410,
                        html=render_simple_status_page(
                            title="Action Expired",
                            badge="expired",
                            message="This Alternative Grab link expired after an interrupted read-only preflight; zero qBittorrent writes were made.",
                            tone="warn",
                        ),
                    )
                # Safe: crashed during read-only preflight before any submission or receipt write
                reset_ok = await asyncio.to_thread(
                    self.db.reset_interrupted_alt_preflight,
                    attempt_id=str(preflight_att["attempt_id"]),
                    action_id=str(amb_act["action_id"]),
                    completion_group=str(amb_act["completion_group"]),
                )
                if not reset_ok:
                    return HtmlResponseData(
                        status_code=409,
                        html=render_simple_status_page(
                            title="Alternative Grab Conflict",
                            badge="409",
                            message="Preflight state changed concurrently; refresh to view current status.",
                            tone="warn",
                        ),
                    )
                return HtmlResponseData(
                    status_code=200,
                    html=render_simple_status_page(
                        title="Preflight Reset (Zero Writes Sent)",
                        badge="active",
                        message="The previous read-only preflight was interrupted before contacting qBittorrent. You may now confirm Alternative Grab again.",
                        tone="warn",
                        post_button_label="Confirm Alternative Grab",
                        hidden_fields={"confirm": "1"},
                    ),
                )
            # Receipt exists -> fall through to read-only qBittorrent hash check
            info_hash = str(prior_receipt.get("info_hash") or "").strip().lower()
            expected_cat = str(prior_receipt.get("category") or cfg.category).strip()
            att_id = str(preflight_att["attempt_id"])
            external_id = ext_id
        else:
            info_hash = ""
            expected_cat = cfg.category
            external_id = ""
            att_id = str(qbit_att["attempt_id"]) if qbit_att else None
            if qbit_att and isinstance(qbit_att.get("request_fingerprint"), str):
                try:
                    fp = json.loads(qbit_att["request_fingerprint"])
                    if isinstance(fp, dict):
                        cand_hash = str(fp.get("info_hash") or "").strip().lower()
                        if len(cand_hash) == 40 and all(ch in "0123456789abcdef" for ch in cand_hash):
                            info_hash = cand_hash
                        expected_cat = str(fp.get("category") or cfg.category).strip()
                        external_id = str(fp.get("external_id") or "").strip()
                except ValueError:
                    pass
            if not external_id:
                meta_ok, meta, _ = self._parse_alt_grab_meta(amb_act)
                if meta_ok and meta:
                    external_id = str(meta["external_id"])
            if (len(info_hash) != 40 or not all(ch in "0123456789abcdef" for ch in info_hash)) and external_id:
                try:
                    rec = await asyncio.to_thread(read_alt_grab_receipt, cfg.state_dir, external_id)
                    if rec:
                        rec_hash = str(rec.get("info_hash") or "").strip().lower()
                        if len(rec_hash) == 40 and all(ch in "0123456789abcdef" for ch in rec_hash):
                            info_hash = rec_hash
                        if rec.get("category"):
                            expected_cat = str(rec.get("category")).strip()
                except (PrivateFileError, OSError, ValueError):
                    pass

        if len(info_hash) != 40 or not all(ch in "0123456789abcdef" for ch in info_hash):
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Alternative Grab Remains Uncertain",
                    badge="uncertain",
                    message="Missing stored torrent info hash for read-only reconciliation; refusing duplicate submission.",
                    tone="warn",
                ),
            )

        qbit_http, owns_qbit, login_res = await self.qbit_client.open_authenticated_session()
        if not login_res.ok:
            return HtmlResponseData(
                status_code=503,
                html=render_simple_status_page(
                    title="qBittorrent Unavailable",
                    badge="uncertain",
                    message="qBittorrent is unreachable during reconciliation. Status remains uncertain; zero writes made.",
                    tone="warn",
                    post_button_label="Re-check qBittorrent Status & Reconcile",
                    hidden_fields={"confirm": "1", "reconcile": "1"},
                ),
            )
        try:
            c_state, c_detail = await self._confirm_qbit_transfer_bounded(
                qbit_http, info_hash=info_hash, expected_category=expected_cat, attempts=1
            )
        finally:
            if owns_qbit and qbit_http is not None:
                await qbit_http.aclose()

        if c_state == "queued":
            outcome_lbl = "queued"
            if external_id:
                try:
                    await asyncio.to_thread(
                        write_alt_grab_receipt,
                        cfg.state_dir,
                        external_id,
                        {
                            "tracker": "torrentleech",
                            "external_id": external_id,
                            "release_id": str(amb_act["release_id"]),
                            "action_id": str(amb_act["action_id"]),
                            "info_hash": info_hash,
                            "category": expected_cat,
                            "status": outcome_lbl,
                            "updated_at": utc_now_iso(),
                        },
                    )
                except (PrivateFileError, OSError):
                    pass
            await asyncio.to_thread(
                self.db.seal_reconciled_attempt,
                attempt_id=att_id,
                action_id=str(amb_act["action_id"]),
                radarr_movie_id=amb_act.get("radarr_movie_id") or amb_act.get("bound_existing_movie_id"),
                outcome_summary=outcome_lbl,
                needs_feedback=False,
            )
            return HtmlResponseData(
                status_code=200,
                html=render_simple_status_page(
                    title="Alternative Grab Confirmed in qBittorrent",
                    badge=outcome_lbl,
                    message=(
                        f"Reconciliation confirmed the transfer in qBittorrent ({outcome_lbl}) with zero duplicate adds. "
                        "No Radarr mutations or evaluator feedback writes were made."
                    ),
                    tone="ok",
                ),
            )

        still_detail = (
            f"qbit_transfer_wrong_category:{c_detail}"
            if c_state == "wrong_category"
            else (c_detail or "qbit_transfer_not_yet_visible_in_category")
        )
        await asyncio.to_thread(
            self.db.mark_action_reconciled_still_ambiguous,
            action_id=str(amb_act["action_id"]),
            detail=still_detail,
        )
        return HtmlResponseData(
            status_code=200,
            html=render_simple_status_page(
                title="Alternative Grab Still Uncertain",
                badge="uncertain",
                message=(
                    "Torrent is not yet visible in qBittorrent in the expected category. "
                    "Status remains uncertain and NO second submission was sent."
                ),
                tone="warn",
                post_button_label="Re-check qBittorrent Status & Reconcile",
                hidden_fields={"confirm": "1", "reconcile": "1"},
            ),
        )

    # =========================================================================
    # 5. Ambiguous Radarr / qBittorrent Reconciliation (§6.3 & Mandatory Case 5b / 5f)
    # =========================================================================

    def _within_attempt_window(
        self,
        *,
        candidate_timestamp: Optional[str],
        attempt_started_at: Optional[str],
    ) -> bool:
        """
        Issue #13 §4c: Require BOTH attempt_started_at and candidate_timestamp to be present
        and parseable ISO-8601 UTC timestamps; absent or unreadable timestamps MUST fail
        reconciliation so stale/uncorrelated records cannot emit 'Grabbed via Telegram'.
        """
        if (
            not isinstance(candidate_timestamp, str)
            or not candidate_timestamp.strip()
            or not isinstance(attempt_started_at, str)
            or not attempt_started_at.strip()
        ):
            return False
        cand_dt = parse_iso_utc(candidate_timestamp)
        start_dt = parse_iso_utc(attempt_started_at)
        if cand_dt is None or start_dt is None:
            return False
        return cand_dt >= (start_dt - timedelta(seconds=60))

    def _matches_announced_release(
        self,
        *,
        expected_release_name: str,
        candidate_value: Optional[str],
        candidate_timestamp: Optional[str],
        attempt_started_at: Optional[str],
        is_path: bool = False,
    ) -> bool:
        """
        Require EXACT normalized release identity (never substring containment):
        - For titles/sceneName: strip only a known trailing media/torrent extension and compare exact equality.
        - For filesystem paths: split path segments and require exact equality against a full path component
          (release folder name or filename stem), rejecting variants like `-REPACK` / `-PROPER`.
        """
        exp_norm = _normalize_release_match_key(expected_release_name)
        if not exp_norm or not candidate_value or not isinstance(candidate_value, str):
            return False
        if not self._within_attempt_window(
            candidate_timestamp=candidate_timestamp,
            attempt_started_at=attempt_started_at,
        ):
            return False

        if is_path:
            segments = [seg for seg in re.split(r"[\\/]+", candidate_value.strip()) if seg]
            return any(_normalize_release_match_key(seg) == exp_norm for seg in segments)

        return _normalize_release_match_key(candidate_value) == exp_norm

    def _matches_guid(
        self,
        *,
        expected_guid: Optional[str],
        candidate_guid: Any,
        candidate_timestamp: Optional[str],
        attempt_started_at: Optional[str],
    ) -> bool:
        if not expected_guid:
            return False
        cand = _extract_safe_guid(candidate_guid)
        if not cand or cand != expected_guid:
            return False
        return self._within_attempt_window(
            candidate_timestamp=candidate_timestamp,
            attempt_started_at=attempt_started_at,
        )

    async def _reconcile_ambiguous_action(
        self,
        amb_act: dict[str, Any],
        group_attempts: tuple[dict[str, Any], ...],
        form_data: dict[str, str],
    ) -> HtmlResponseData:
        # Handle qBittorrent Alternative Grab reconciliation separately (zero Radarr writes / zero duplicate adds)
        if str(amb_act["action_type"]) == "alternative_grab":
            qbit_att = next(
                (
                    a
                    for a in group_attempts
                    if a["action_id"] == amb_act["action_id"]
                    and a["phase"] == "qbit_alternative_grab"
                    and a["status"] == "ambiguous"
                ),
                None,
            )
            if qbit_att is None:
                qbit_att = next(
                    (
                        a
                        for a in group_attempts
                        if a["action_id"] == amb_act["action_id"]
                        and a["phase"] == "qbit_alternative_grab"
                        and a["status"] in ("attempting", "succeeded")
                    ),
                    None,
                )
            preflight_att = next(
                (
                    a
                    for a in group_attempts
                    if a["action_id"] == amb_act["action_id"]
                    and a["phase"] == "qbit_alt_preflight"
                    and a["status"] == "ambiguous"
                ),
                None,
            )
            if preflight_att is None:
                preflight_att = next(
                    (
                        a
                        for a in group_attempts
                        if a["action_id"] == amb_act["action_id"]
                        and a["phase"] == "qbit_alt_preflight"
                    ),
                    None,
                )
            return await self._reconcile_ambiguous_qbit_alt_grab(amb_act, qbit_att, preflight_att)

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
        expected_guid: Optional[str] = None
        if radarr_att and isinstance(radarr_att.get("request_fingerprint"), str):
            try:
                fp_parsed = json.loads(radarr_att["request_fingerprint"])
                if isinstance(fp_parsed, dict):
                    expected_guid = _extract_safe_guid(fp_parsed.get("guid"))
            except ValueError:
                expected_guid = None

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
            if self._is_expired(amb_act):
                expired_ok = await asyncio.to_thread(
                    self.db.mark_action_expired,
                    action_id=str(amb_act["action_id"]),
                    detail="expired_before_phase2_push",
                )
                if not expired_ok:
                    return HtmlResponseData(
                        status_code=409,
                        html=render_simple_status_page(
                            title="Reconciliation State Changed",
                            badge="409",
                            message="Action state changed concurrently; refresh to view current status.",
                            tone="warn",
                        ),
                    )
                return HtmlResponseData(
                    status_code=410,
                    html=render_simple_status_page(
                        title="Action Expired Before Push",
                        badge="expired",
                        message="Action link expired before release push. No push was sent.",
                        tone="warn",
                    ),
                )

            # PR #18 Round 2 Blocker 3b: Fast local authorization check first, then fetch
            # the intake push payload BEFORE the authoritative live Radarr eligibility check
            # so any Radarr state change while reading the archive is caught before pushing.
            pre_delivery = await asyncio.to_thread(self.db.get_delivery, str(amb_act["release_id"]))
            if pre_delivery is not None:
                pre_rev = int(pre_delivery["card_revision"])
                pre_kind = str(pre_delivery["delivery_kind"])
                if pre_kind in ("retired", "evidence_review") or pre_rev != int(amb_act["card_revision"]):
                    await asyncio.to_thread(
                        self.db.mark_action_superseded,
                        action_id=str(amb_act["action_id"]),
                        detail="superseded_or_retired_before_recovery_push",
                    )
                    return HtmlResponseData(
                        status_code=410,
                        html=render_simple_status_page(
                            title="Action Superseded",
                            badge="superseded",
                            message="Card was retired, downgraded, or replaced by a newer revision. Zero push was sent.",
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
                        message="Could not resolve verified intake push payload right now; zero push was sent.",
                        tone="warn",
                        post_button_label="Re-check Radarr Status & Reconcile",
                        hidden_fields={"confirm": "1", "reconcile": "1"},
                    ),
                )

            # PR #18 Blocker 2 & Round 2 Blocker 3b: Apply the unified pre-first-push
            # eligibility gate AFTER payload retrieval and IMMEDIATELY BEFORE advancing
            # to Phase 2 release push (zero external I/O between check and push)!
            live_movie, pre_push_err = await self._verify_pre_first_push_eligibility(
                action=amb_act,
                require_movie_present=False,
                unavailable_badge="ambiguous_radarr",
                reconcile_retry_button=True,
            )
            if pre_push_err is not None:
                if pre_push_err.status_code == 410:
                    await asyncio.to_thread(
                        self.db.mark_action_superseded,
                        action_id=str(amb_act["action_id"]),
                        detail="superseded_or_retired_before_recovery_push",
                    )
                return pre_push_err

            if live_movie is not None:
                movie_id = int(live_movie["id"])
                advanced = await asyncio.to_thread(
                    self.db.mark_phase1_add_movie_reconciled,
                    attempt_id=att_id,
                    action_id=str(amb_act["action_id"]),
                    completion_group=str(amb_act["completion_group"]),
                    radarr_movie_id=movie_id,
                    outcome_summary=f"reconciled_movie_present:{movie_id}",
                )
                if not advanced:
                    return HtmlResponseData(
                        status_code=409,
                        html=render_simple_status_page(
                            title="Reconciliation State Changed",
                            badge="409",
                            message=(
                                "Action was retired, marked Watched, or already advanced by another reconciliation. "
                                "Zero release push was issued."
                            ),
                            tone="warn",
                            post_button_label="Re-check Radarr Status",
                            hidden_fields={"confirm": "1", "reconcile": "1"},
                        ),
                    )
                return await self._execute_release_push_phase(
                    action=amb_act,
                    radarr_movie_id=movie_id,
                    push_payload=payload_res.push_payload,
                    allowed_action_statuses=frozenset({"ambiguous_radarr"}),
                )

            return await self._execute_add_grab(amb_act, form_data, is_phase1_retry=True)

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

            # 1. Check queue details (exact title or exact GUID)
            for q_item in q_res.data:
                if not isinstance(q_item, dict):
                    continue
                if self._matches_guid(
                    expected_guid=expected_guid,
                    candidate_guid=q_item.get("guid"),
                    candidate_timestamp=q_item.get("added"),
                    attempt_started_at=att_started,
                ) or self._matches_announced_release(
                    expected_release_name=expected_release,
                    candidate_value=q_item.get("title") or q_item.get("sourceTitle"),
                    candidate_timestamp=q_item.get("added"),
                    attempt_started_at=att_started,
                    is_path=False,
                ):
                    match_found = True
                    match_source = "queue"
                    break

            # 2. Check grab/import history (exact GUID, exact sourceTitle, or exact path component)
            if not match_found:
                for h_item in h_res.data:
                    if not isinstance(h_item, dict):
                        continue
                    ev = str(h_item.get("eventType") or "").strip().lower()
                    if ev not in ("grabbed", "downloadfolderimported", "grab"):
                        continue
                    h_data = h_item.get("data") if isinstance(h_item.get("data"), dict) else {}
                    if self._matches_guid(
                        expected_guid=expected_guid,
                        candidate_guid=h_data.get("guid"),
                        candidate_timestamp=h_item.get("date"),
                        attempt_started_at=att_started,
                    ):
                        match_found = True
                        match_source = f"history_guid:{ev}"
                        break
                    for cand_field, field_is_path in (
                        (h_item.get("sourceTitle"), False),
                        (h_data.get("droppedPath"), True),
                        (h_data.get("importedPath"), True),
                    ):
                        if self._matches_announced_release(
                            expected_release_name=expected_release,
                            candidate_value=cand_field,
                            candidate_timestamp=h_item.get("date"),
                            attempt_started_at=att_started,
                            is_path=field_is_path,
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
                    for cand_field, field_is_path in (
                        (mf.get("sceneName"), False),
                        (mf.get("relativePath"), True),
                    ):
                        if self._matches_announced_release(
                            expected_release_name=expected_release,
                            candidate_value=cand_field,
                            candidate_timestamp=mf.get("dateAdded"),
                            attempt_started_at=att_started,
                            is_path=field_is_path,
                        ):
                            match_found = True
                            match_source = "imported_file"
                            break

        if match_found:
            sealed = await asyncio.to_thread(
                self.db.seal_reconciled_attempt,
                attempt_id=att_id,
                action_id=str(amb_act["action_id"]),
                radarr_movie_id=movie_id,
                outcome_summary=f"reconciled_push_found_in_{match_source}",
                needs_feedback=True,
            )
            refreshed = await asyncio.to_thread(self.db.get_action_group_readonly, str(amb_act["token_sha256"]))
            current = refreshed.action or amb_act
            if sealed and current["status"] == "completed_radarr":
                current = await self._run_isolated_feedback(current)
            return self._render_completion_html(current, amb_act)

        # Match NOT FOUND (absent snapshot) -> MUST retain ambiguous_radarr (never 2nd push),
        # AND must NEVER downgrade an action that another reconciler already completed!
        still_ambiguous = await asyncio.to_thread(
            self.db.mark_action_reconciled_still_ambiguous,
            action_id=str(amb_act["action_id"]),
            detail="reconcile_snapshot_absent_no_second_push",
        )
        if not still_ambiguous:
            refreshed = await asyncio.to_thread(self.db.get_action_group_readonly, str(amb_act["token_sha256"]))
            done_act = next(
                (a for a in refreshed.group_actions if a["status"] in ("completed_all", "completed_radarr")),
                None,
            )
            if done_act is not None:
                return self._render_completion_html(done_act, amb_act)
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
