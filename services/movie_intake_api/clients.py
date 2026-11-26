from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .security import redact_secrets


@dataclass(frozen=True)
class RadarrStatusResponse:
    status: str
    # absent | watched_unmonitored | owned_file | monitored_no_file | ambiguous | lookup_failed
    movie_id: Optional[int] = None
    file_id: Optional[int] = None
    existing_quality: Optional[str] = None
    monitored: Optional[bool] = None
    movie_file_snapshot: Optional[dict[str, Any]] = None
    alternative_grab_configured: bool = False
    detail: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "movie_id": self.movie_id,
            "file_id": self.file_id,
            "existing_quality": self.existing_quality,
            "monitored": self.monitored,
            "movie_file_snapshot": dict(self.movie_file_snapshot)
            if isinstance(self.movie_file_snapshot, dict)
            else None,
            "alternative_grab_configured": self.alternative_grab_configured,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DeliveryCardResponse:
    status: str  # delivered | ambiguous_delivery | failed
    ack_chat_id: Optional[str] = None
    ack_message_id: Optional[int] = None
    ack_thread_id: Optional[int] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class DeliveryEvalResponse:
    status: str  # sent | ambiguous_reply | failed
    detail: Optional[str] = None


@dataclass(frozen=True)
class TriageEvalResult:
    ok: bool
    payload: Optional[dict[str, Any]] = None
    error_redacted: Optional[str] = None


class TriageApiClient:
    """Async client calling local movie-triage-api POST /evaluate with X-Internal-Token."""

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        timeout_seconds: float = 85.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client

    async def evaluate(self, request_body: dict[str, Any]) -> TriageEvalResult:
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token

        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.post(
                f"{self.base_url}/evaluate",
                json=request_body,
                headers=headers,
            )
            if resp.status_code != 200:
                detail = redact_secrets(resp.text, max_chars=240)
                return TriageEvalResult(
                    ok=False,
                    error_redacted=f"Evaluator returned HTTP {resp.status_code}: {detail}",
                )
            data = resp.json()
            if not isinstance(data, dict):
                return TriageEvalResult(ok=False, error_redacted="Evaluator returned non-object JSON")
            return TriageEvalResult(ok=True, payload=data)
        except Exception as exc:
            return TriageEvalResult(
                ok=False,
                error_redacted=f"Evaluator request failed: {redact_secrets(str(exc), max_chars=240) or exc.__class__.__name__}",
            )
        finally:
            if owns_client:
                await client.aclose()


class ActionsApiClient:
    """Async client calling local movie-actions-api /internal/* endpoints with X-Internal-Token."""

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        timeout_seconds: float = 20.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token
        return headers

    async def get_capabilities(self) -> Optional[bool]:
        """
        Query movie-actions-api GET /internal/capabilities for `alternative_grab_configured`.
        Returns True/False on valid HTTP 200 JSON, or None on transient/unreachable failure.
        """
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.get(
                f"{self.base_url}/internal/capabilities",
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not isinstance(data, dict):
                return None
            val = data.get("alternative_grab_configured")
            if isinstance(val, bool):
                return val
            return None
        except Exception:
            return None
        finally:
            if owns_client:
                await client.aclose()

    async def get_radarr_status(
        self,
        *,
        tmdb_id: Optional[int],
        imdb_id: Optional[str],
        release_title: Optional[str] = None,
    ) -> RadarrStatusResponse:
        params: dict[str, Any] = {}
        if tmdb_id is not None:
            params["tmdb_id"] = tmdb_id
        if imdb_id:
            params["imdb_id"] = imdb_id
        if release_title:
            params["release_title"] = release_title

        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.get(
                f"{self.base_url}/internal/radarr/status",
                params=params,
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return RadarrStatusResponse(
                    status="lookup_failed",
                    detail=f"actions_http_{resp.status_code}",
                )
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("status"), str):
                return RadarrStatusResponse(status="lookup_failed", detail="malformed_radarr_status")
            status = data["status"]
            if status not in (
                "absent",
                "watched_unmonitored",
                "owned_file",
                "monitored_no_file",
                "ambiguous",
                "lookup_failed",
            ):
                return RadarrStatusResponse(status="lookup_failed", detail="unknown_radarr_status")
            raw_mon = data.get("monitored")
            monitored = raw_mon if isinstance(raw_mon, bool) else None
            raw_snap = data.get("movie_file_snapshot")
            mf_snap = dict(raw_snap) if isinstance(raw_snap, dict) else None
            alt_cfg = bool(data.get("alternative_grab_configured"))
            return RadarrStatusResponse(
                status=status,
                movie_id=data.get("movie_id"),
                file_id=data.get("file_id"),
                existing_quality=data.get("existing_quality"),
                monitored=monitored,
                movie_file_snapshot=mf_snap,
                alternative_grab_configured=alt_cfg,
                detail=redact_secrets(str(data.get("detail") or ""), max_chars=200) or None,
            )
        except Exception as exc:
            return RadarrStatusResponse(
                status="lookup_failed",
                detail=redact_secrets(str(exc), max_chars=200) or exc.__class__.__name__,
            )
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _parse_optional_int(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text and (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
                try:
                    return int(text)
                except ValueError:
                    return None
        return None

    async def deliver_card(self, card_payload: dict[str, Any]) -> DeliveryCardResponse:
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.post(
                f"{self.base_url}/internal/deliver/card",
                json=card_payload,
                headers=self._headers(),
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    return DeliveryCardResponse(
                        status="ambiguous_delivery",
                        detail="invalid_card_ack_json",
                    )
                if not isinstance(data, dict):
                    return DeliveryCardResponse(status="ambiguous_delivery", detail="malformed_card_ack")
                st = str(data.get("status") or "").strip().lower()
                detail = redact_secrets(str(data.get("detail") or ""), max_chars=200) or None
                if st == "failed":
                    return DeliveryCardResponse(status="failed", detail=detail)
                if st == "delivered":
                    msg_id = self._parse_optional_int(data.get("ack_message_id"))
                    if msg_id is None or msg_id <= 0:
                        return DeliveryCardResponse(
                            status="ambiguous_delivery",
                            detail="unusable_delivered_ack_missing_message_id",
                        )
                    raw_chat = data.get("ack_chat_id")
                    chat_id = str(raw_chat).strip() if raw_chat is not None else ""
                    thread_id = self._parse_optional_int(data.get("ack_thread_id"))
                    return DeliveryCardResponse(
                        status="delivered",
                        ack_chat_id=chat_id,
                        ack_message_id=msg_id,
                        ack_thread_id=thread_id,
                    )
                # Unknown, empty, or explicit ambiguous_delivery status: never prove non-delivery
                return DeliveryCardResponse(
                    status="ambiguous_delivery",
                    detail=detail or (f"unexpected_card_ack_status:{st}" if st else "missing_card_ack_status"),
                )
            if 400 <= resp.status_code < 500:
                return DeliveryCardResponse(
                    status="failed",
                    detail=f"actions_http_{resp.status_code}",
                )
            # 5xx from actions-service after transmit may mean Telegram was already sent
            return DeliveryCardResponse(
                status="ambiguous_delivery",
                detail=f"actions_http_{resp.status_code}",
            )
        except httpx.ConnectError:
            # Connection refused before transmit: definitively not delivered
            return DeliveryCardResponse(status="failed", detail="actions_connect_error")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError, ValueError, TypeError) as exc:
            # Lost or unusable ack after transmit: MUST treat as ambiguous_delivery (never resend!)
            return DeliveryCardResponse(
                status="ambiguous_delivery",
                detail=redact_secrets(exc.__class__.__name__, max_chars=120),
            )
        finally:
            if owns_client:
                await client.aclose()

    async def deliver_evaluation(self, eval_reply_payload: dict[str, Any]) -> DeliveryEvalResponse:
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.post(
                f"{self.base_url}/internal/deliver/evaluation",
                json=eval_reply_payload,
                headers=self._headers(),
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    return DeliveryEvalResponse(status="ambiguous_reply", detail="invalid_eval_ack_json")
                if not isinstance(data, dict):
                    return DeliveryEvalResponse(status="ambiguous_reply", detail="malformed_eval_ack")
                st = str(data.get("status") or "").strip().lower()
                if st == "sent":
                    return DeliveryEvalResponse(status="sent")
                if st == "failed":
                    return DeliveryEvalResponse(status="failed", detail="actions_reply_failed")
                return DeliveryEvalResponse(
                    status="ambiguous_reply",
                    detail="actions_ambiguous_reply" if st == "ambiguous_reply" else "unusable_eval_ack",
                )
            if 400 <= resp.status_code < 500:
                return DeliveryEvalResponse(status="failed", detail=f"actions_http_{resp.status_code}")
            return DeliveryEvalResponse(status="ambiguous_reply", detail=f"actions_http_{resp.status_code}")
        except httpx.ConnectError:
            return DeliveryEvalResponse(status="failed", detail="actions_connect_error")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError, ValueError, TypeError) as exc:
            return DeliveryEvalResponse(
                status="ambiguous_reply",
                detail=redact_secrets(exc.__class__.__name__, max_chars=120),
            )
        finally:
            if owns_client:
                await client.aclose()


class ShadowStubTriageClient(TriageApiClient):
    """Zero-mutation in-memory triage stub used automatically when SHADOW_REPLAY_ENABLED=true."""

    is_shadow_stub: bool = True

    def __init__(self) -> None:
        super().__init__(base_url="http://shadow.invalid", internal_token="")
        self.calls: list[dict[str, Any]] = []

    async def evaluate(self, request_body: dict[str, Any]) -> TriageEvalResult:
        self.calls.append(dict(request_body))
        return TriageEvalResult(
            ok=False,
            error_redacted="shadow_replay_stub_evaluator_no_external_mutation",
        )


class ShadowStubActionsClient(ActionsApiClient):
    """Zero-mutation in-memory actions stub used automatically when SHADOW_REPLAY_ENABLED=true."""

    is_shadow_stub: bool = True

    def __init__(self, *, alternative_grab_configured: bool = False) -> None:
        super().__init__(base_url="http://shadow.invalid", internal_token="")
        self.alternative_grab_configured = alternative_grab_configured
        self.capability_calls: int = 0
        self.radarr_calls: list[dict[str, Any]] = []
        self.card_calls: list[dict[str, Any]] = []
        self.eval_reply_calls: list[dict[str, Any]] = []
        self._next_msg_id = 1000
        self._delivered_by_release: dict[str, DeliveryCardResponse] = {}

    async def get_capabilities(self) -> Optional[bool]:
        self.capability_calls += 1
        return self.alternative_grab_configured

    async def get_radarr_status(
        self,
        *,
        tmdb_id: Optional[int],
        imdb_id: Optional[str],
        release_title: Optional[str] = None,
    ) -> RadarrStatusResponse:
        self.radarr_calls.append({"tmdb_id": tmdb_id, "imdb_id": imdb_id, "release_title": release_title})
        return RadarrStatusResponse(
            status="absent",
            alternative_grab_configured=self.alternative_grab_configured,
            detail="shadow_stub",
        )

    async def deliver_card(self, card_payload: dict[str, Any]) -> DeliveryCardResponse:
        self.card_calls.append(dict(card_payload))
        rel_id = str(card_payload["release_id"])
        if rel_id in self._delivered_by_release:
            return self._delivered_by_release[rel_id]
        self._next_msg_id += 1
        resp = DeliveryCardResponse(
            status="delivered",
            ack_chat_id="shadow-chat",
            ack_message_id=self._next_msg_id,
            ack_thread_id=0,
        )
        self._delivered_by_release[rel_id] = resp
        return resp

    async def deliver_evaluation(self, eval_reply_payload: dict[str, Any]) -> DeliveryEvalResponse:
        self.eval_reply_calls.append(dict(eval_reply_payload))
        return DeliveryEvalResponse(status="sent", detail="shadow_stub")
