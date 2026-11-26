from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .security import redact_secrets


@dataclass(frozen=True)
class TelegramSendResult:
    outcome: str  # ok | not_modified | failed_definitive | ambiguous
    ack_chat_id: Optional[str] = None
    ack_message_id: Optional[int] = None
    ack_thread_id: Optional[int] = None
    error_redacted: Optional[str] = None


@dataclass(frozen=True)
class RadarrReadResult:
    ok: bool
    data: Any = None
    status_code: Optional[int] = None
    error_redacted: Optional[str] = None


@dataclass(frozen=True)
class RadarrWriteResult:
    outcome: str
    # ok | already_exists | rejected | failed_not_transmitted | failed_client | ambiguous
    data: Any = None
    status_code: Optional[int] = None
    error_redacted: Optional[str] = None


@dataclass(frozen=True)
class IntakePushPayloadResult:
    ok: bool
    push_payload: Optional[dict[str, Any]] = None
    status_code: Optional[int] = None
    error_redacted: Optional[str] = None


@dataclass(frozen=True)
class FeedbackPostResult:
    saved: bool
    decision_saved: Optional[str] = None
    error_redacted: Optional[str] = None


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


class TelegramClient:
    """Async Telegram Bot API client with strict lost-ack ambiguity handling and secret hygiene."""

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        bot_token: str,
        timeout_seconds: float = 20.0,
        api_base_url: str = "https://api.telegram.org",
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.bot_token = bot_token
        self.timeout_seconds = timeout_seconds
        self.api_base_url = api_base_url.rstrip("/")
        self._external_client = http_client

    async def _call(
        self,
        method: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        data_fields: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
        is_edit: bool = False,
    ) -> TelegramSendResult:
        if not self.bot_token:
            return TelegramSendResult(outcome="failed_definitive", error_redacted="telegram_bot_token_missing")

        url = f"{self.api_base_url}/bot{self.bot_token}/{method}"
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            if files is not None:
                resp = await client.post(url, data=data_fields or {}, files=files)
            else:
                resp = await client.post(url, json=json_body or {})
        except httpx.ConnectError:
            # Connection refused / DNS failure before HTTP request bytes sent
            return TelegramSendResult(outcome="failed_definitive", error_redacted="telegram_connect_error")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            # Timeout or mid-stream disconnect after transmit -> ambiguous (never blind resend!)
            return TelegramSendResult(
                outcome="ambiguous",
                error_redacted=f"telegram_transport_ambiguous:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                return TelegramSendResult(outcome="ambiguous", error_redacted="telegram_invalid_json_200")
            if not isinstance(body, dict):
                return TelegramSendResult(outcome="ambiguous", error_redacted="telegram_non_dict_200")
            if body.get("ok") is False:
                desc = str(body.get("description") or "")
                if is_edit and "message is not modified" in desc.lower():
                    return TelegramSendResult(outcome="not_modified")
                return TelegramSendResult(
                    outcome="failed_definitive",
                    error_redacted=redact_secrets(desc, max_chars=200) or "telegram_ok_false",
                )
            if body.get("ok") is not True:
                return TelegramSendResult(outcome="ambiguous", error_redacted="telegram_missing_ok_flag")

            result = body.get("result")
            if not isinstance(result, dict):
                return TelegramSendResult(outcome="ambiguous", error_redacted="telegram_missing_result_object")

            msg_id = _parse_optional_int(result.get("message_id"))
            chat_obj = result.get("chat") if isinstance(result.get("chat"), dict) else {}
            raw_chat_id = chat_obj.get("id")
            chat_id = str(raw_chat_id).strip() if raw_chat_id is not None else ""
            thread_id = _parse_optional_int(result.get("message_thread_id"))
            if msg_id is None or msg_id <= 0 or not chat_id:
                return TelegramSendResult(
                    outcome="ambiguous",
                    error_redacted="telegram_200_missing_ack_ids",
                )
            return TelegramSendResult(
                outcome="ok",
                ack_chat_id=chat_id,
                ack_message_id=msg_id,
                ack_thread_id=thread_id,
            )

        if 400 <= resp.status_code < 500:
            desc = ""
            try:
                err_body = resp.json()
                if isinstance(err_body, dict):
                    desc = str(err_body.get("description") or "")
            except ValueError:
                desc = resp.text[:200]
            if is_edit and "message is not modified" in desc.lower():
                return TelegramSendResult(outcome="not_modified")
            return TelegramSendResult(
                outcome="failed_definitive",
                error_redacted=f"telegram_http_{resp.status_code}:{redact_secrets(desc, max_chars=180)}",
            )

        # 5xx after transmit cannot prove non-delivery
        return TelegramSendResult(
            outcome="ambiguous",
            error_redacted=f"telegram_http_{resp.status_code}",
        )

    async def send_photo(
        self,
        *,
        chat_id: str,
        photo_url: str,
        caption_html: str,
        reply_markup: dict[str, Any],
        thread_id: Optional[int] = None,
    ) -> TelegramSendResult:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption_html,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        }
        if thread_id is not None and thread_id > 0:
            body["message_thread_id"] = thread_id
        return await self._call("sendPhoto", json_body=body, is_edit=False)

    async def send_message(
        self,
        *,
        chat_id: str,
        text_html: str,
        reply_markup: Optional[dict[str, Any]] = None,
        thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> TelegramSendResult:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        if thread_id is not None and thread_id > 0:
            body["message_thread_id"] = thread_id
        if reply_to_message_id is not None and reply_to_message_id > 0:
            body["reply_to_message_id"] = reply_to_message_id
        return await self._call("sendMessage", json_body=body, is_edit=False)

    async def edit_message_caption(
        self,
        *,
        chat_id: str,
        message_id: int,
        caption_html: str,
        reply_markup: dict[str, Any],
    ) -> TelegramSendResult:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": caption_html,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        }
        return await self._call("editMessageCaption", json_body=body, is_edit=True)

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text_html: str,
        reply_markup: dict[str, Any],
    ) -> TelegramSendResult:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        }
        return await self._call("editMessageText", json_body=body, is_edit=True)

    async def send_document(
        self,
        *,
        chat_id: str,
        filename: str,
        content_bytes: bytes,
        caption: Optional[str] = None,
        thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> TelegramSendResult:
        fields: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption
        if thread_id is not None and thread_id > 0:
            fields["message_thread_id"] = str(thread_id)
        if reply_to_message_id is not None and reply_to_message_id > 0:
            fields["reply_to_message_id"] = str(reply_to_message_id)
        files = {"document": (filename, content_bytes, "text/plain; charset=utf-8")}
        return await self._call("sendDocument", data_fields=fields, files=files, is_edit=False)


class RadarrClient:
    """Async Radarr v3 client preserving provider URL prefixes and strict write semantics."""

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 20.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _get_json(self, path: str, *, params: Optional[dict[str, Any]] = None) -> RadarrReadResult:
        if not self.base_url or not self.api_key:
            return RadarrReadResult(ok=False, error_redacted="radarr_not_configured")
        url = f"{self.base_url}{path}"
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.get(url, params=params, headers=self._headers())
            if resp.status_code != 200:
                return RadarrReadResult(
                    ok=False,
                    status_code=resp.status_code,
                    error_redacted=f"radarr_http_{resp.status_code}",
                )
            try:
                data = resp.json()
            except ValueError:
                return RadarrReadResult(ok=False, status_code=200, error_redacted="radarr_invalid_json")
            return RadarrReadResult(ok=True, data=data, status_code=200)
        except Exception as exc:
            return RadarrReadResult(
                ok=False,
                error_redacted=f"radarr_read_failed:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()

    async def get_tags(self) -> RadarrReadResult:
        res = await self._get_json("/api/v3/tag")
        if res.ok and not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_tags_not_list")
        return res

    async def get_root_folders(self) -> RadarrReadResult:
        res = await self._get_json("/api/v3/rootfolder")
        if res.ok and not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_roots_not_list")
        return res

    async def get_quality_profiles(self) -> RadarrReadResult:
        res = await self._get_json("/api/v3/qualityprofile")
        if res.ok and not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_profiles_not_list")
        return res

    async def get_download_clients(self) -> RadarrReadResult:
        res = await self._get_json("/api/v3/downloadclient")
        if res.ok and not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_downloadclients_not_list")
        return res

    async def get_movies_by_tmdb(self, tmdb_id: int) -> RadarrReadResult:
        res = await self._get_json("/api/v3/movie", params={"tmdbId": tmdb_id})
        if not res.ok:
            return res
        if not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_movies_not_list")
        filtered = [
            m
            for m in res.data
            if isinstance(m, dict)
            and isinstance(m.get("tmdbId"), int)
            and not isinstance(m.get("tmdbId"), bool)
            and m["tmdbId"] == tmdb_id
        ]
        return RadarrReadResult(ok=True, data=filtered, status_code=res.status_code)

    async def get_movies_by_imdb(self, imdb_id: str) -> RadarrReadResult:
        # Radarr v3 GET /api/v3/movie only filters on `tmdbId` (ignoring `imdbId` and returning
        # the entire library). Fetch /api/v3/movie and filter strictly by exact normalized imdbId.
        target = imdb_id.strip().lower()
        if not target:
            return RadarrReadResult(ok=True, data=[], status_code=200)
        res = await self._get_json("/api/v3/movie")
        if not res.ok:
            return res
        if not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_movies_not_list")
        filtered = [
            m
            for m in res.data
            if isinstance(m, dict) and str(m.get("imdbId") or "").strip().lower() == target
        ]
        return RadarrReadResult(ok=True, data=filtered, status_code=res.status_code)

    async def get_movie_by_id(self, movie_id: int) -> RadarrReadResult:
        res = await self._get_json(f"/api/v3/movie/{movie_id}")
        if res.ok and not isinstance(res.data, dict):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_movie_not_dict")
        return res

    async def get_queue_details(self, movie_id: int) -> RadarrReadResult:
        res = await self._get_json("/api/v3/queue/details", params={"movieId": movie_id})
        if not res.ok:
            return res
        if not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_queue_not_list")
        filtered = [
            q
            for q in res.data
            if isinstance(q, dict)
            and (q.get("movieId") is None or q.get("movieId") == movie_id)
        ]
        return RadarrReadResult(ok=True, data=filtered, status_code=res.status_code)

    async def get_movie_history(self, movie_id: int) -> RadarrReadResult:
        res = await self._get_json("/api/v3/history/movie", params={"movieId": movie_id})
        if res.ok and not isinstance(res.data, list):
            return RadarrReadResult(ok=False, status_code=res.status_code, error_redacted="radarr_history_not_list")
        return res

    async def add_movie(self, payload: dict[str, Any]) -> RadarrWriteResult:
        if not self.base_url or not self.api_key:
            return RadarrWriteResult(outcome="failed_not_transmitted", error_redacted="radarr_not_configured")
        url = f"{self.base_url}/api/v3/movie"
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.ConnectError:
            return RadarrWriteResult(outcome="failed_not_transmitted", error_redacted="radarr_connect_error")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            return RadarrWriteResult(
                outcome="ambiguous",
                error_redacted=f"radarr_add_ambiguous:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code in (200, 201):
            try:
                data = resp.json()
            except ValueError:
                return RadarrWriteResult(outcome="ambiguous", status_code=resp.status_code, error_redacted="radarr_add_invalid_json")
            if isinstance(data, dict) and isinstance(data.get("id"), int) and not isinstance(data.get("id"), bool) and data["id"] > 0:
                return RadarrWriteResult(outcome="ok", data=data, status_code=resp.status_code)
            return RadarrWriteResult(outcome="ambiguous", status_code=resp.status_code, error_redacted="radarr_add_missing_id")

        if 400 <= resp.status_code < 500:
            body_low = (resp.text or "").lower()
            if resp.status_code == 400 and ("already" in body_low or "movieexistsvalidator" in body_low or "exists" in body_low):
                return RadarrWriteResult(
                    outcome="already_exists",
                    status_code=resp.status_code,
                    error_redacted="radarr_movie_already_exists",
                )
            return RadarrWriteResult(
                outcome="failed_client",
                status_code=resp.status_code,
                error_redacted=f"radarr_http_{resp.status_code}:{redact_secrets(resp.text, max_chars=180)}",
            )

        return RadarrWriteResult(
            outcome="ambiguous",
            status_code=resp.status_code,
            error_redacted=f"radarr_http_{resp.status_code}",
        )

    async def update_movie(self, movie_id: int, payload: dict[str, Any]) -> RadarrWriteResult:
        if not self.base_url or not self.api_key:
            return RadarrWriteResult(outcome="failed_not_transmitted", error_redacted="radarr_not_configured")
        url = f"{self.base_url}/api/v3/movie/{movie_id}"
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.put(url, json=payload, headers=self._headers())
        except httpx.ConnectError:
            return RadarrWriteResult(outcome="failed_not_transmitted", error_redacted="radarr_connect_error")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            return RadarrWriteResult(
                outcome="ambiguous",
                error_redacted=f"radarr_put_ambiguous:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code in (200, 202):
            try:
                data = resp.json()
            except ValueError:
                return RadarrWriteResult(outcome="ambiguous", status_code=resp.status_code, error_redacted="radarr_put_invalid_json")
            if isinstance(data, dict):
                return RadarrWriteResult(outcome="ok", data=data, status_code=resp.status_code)
            return RadarrWriteResult(outcome="ambiguous", status_code=resp.status_code, error_redacted="radarr_put_non_dict")

        if 400 <= resp.status_code < 500:
            return RadarrWriteResult(
                outcome="failed_client",
                status_code=resp.status_code,
                error_redacted=f"radarr_http_{resp.status_code}:{redact_secrets(resp.text, max_chars=180)}",
            )

        return RadarrWriteResult(
            outcome="ambiguous",
            status_code=resp.status_code,
            error_redacted=f"radarr_http_{resp.status_code}",
        )

    async def push_release(self, push_payload: dict[str, Any]) -> RadarrWriteResult:
        """
        POST /api/v3/release/push with strict single-decision acceptance check:
        Requires HTTP 200/201 AND JSON list of len == 1 with approved === True and rejected === False.
        HTTP 200 alone is insufficient.
        """
        if not self.base_url or not self.api_key:
            return RadarrWriteResult(outcome="failed_not_transmitted", error_redacted="radarr_not_configured")
        url = f"{self.base_url}/api/v3/release/push"
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.post(url, json=push_payload, headers=self._headers())
        except httpx.ConnectError:
            return RadarrWriteResult(outcome="failed_not_transmitted", error_redacted="radarr_connect_error")
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
            return RadarrWriteResult(
                outcome="ambiguous",
                error_redacted=f"radarr_push_ambiguous:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()

        if resp.status_code in (200, 201):
            try:
                data = resp.json()
            except ValueError:
                return RadarrWriteResult(
                    outcome="ambiguous",
                    status_code=resp.status_code,
                    error_redacted="radarr_push_invalid_json_200",
                )
            if not isinstance(data, list):
                return RadarrWriteResult(
                    outcome="ambiguous",
                    status_code=resp.status_code,
                    error_redacted="radarr_push_non_list_200",
                )
            if len(data) == 1 and isinstance(data[0], dict):
                decision = data[0]
                if decision.get("approved") is True and decision.get("rejected") is False:
                    return RadarrWriteResult(outcome="ok", data=decision, status_code=resp.status_code)
                rejs = decision.get("rejections")
                if isinstance(rejs, list) and rejs:
                    rej_str = "; ".join(redact_secrets(str(r), max_chars=120) for r in rejs[:5])
                else:
                    rej_str = f"approved={decision.get('approved')}, rejected={decision.get('rejected')}"
                return RadarrWriteResult(
                    outcome="rejected",
                    data=decision,
                    status_code=resp.status_code,
                    error_redacted=f"Radarr rejected release push: {rej_str}",
                )
            if len(data) == 0:
                return RadarrWriteResult(
                    outcome="rejected",
                    status_code=resp.status_code,
                    error_redacted="Radarr release/push returned zero decisions",
                )
            # Multiple decisions returned: if ANY was approved, outcome is ambiguous (never re-push!)
            any_approved = any(isinstance(item, dict) and item.get("approved") is True for item in data)
            if any_approved:
                return RadarrWriteResult(
                    outcome="ambiguous",
                    status_code=resp.status_code,
                    error_redacted=f"radarr_push_multiple_decisions_with_approval:{len(data)}",
                )
            return RadarrWriteResult(
                outcome="rejected",
                status_code=resp.status_code,
                error_redacted=f"Radarr release/push rejected ({len(data)} decisions, none approved)",
            )

        if resp.status_code in (400, 422):
            return RadarrWriteResult(
                outcome="rejected",
                status_code=resp.status_code,
                error_redacted=f"Radarr rejected release push (HTTP {resp.status_code}): {redact_secrets(resp.text, max_chars=180)}",
            )
        if 400 <= resp.status_code < 500:
            return RadarrWriteResult(
                outcome="failed_client",
                status_code=resp.status_code,
                error_redacted=f"radarr_http_{resp.status_code}:{redact_secrets(resp.text, max_chars=180)}",
            )
        return RadarrWriteResult(
            outcome="ambiguous",
            status_code=resp.status_code,
            error_redacted=f"radarr_http_{resp.status_code}",
        )


class IntakePushClient:
    """Fetches ephemeral Radarr push payload on demand from movie-intake-api (never persisted)."""

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        timeout_seconds: float = 15.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client

    async def fetch_push_payload(self, *, release_id: str, source_occurrence_id: str) -> IntakePushPayloadResult:
        if not self.base_url or not self.internal_token:
            return IntakePushPayloadResult(ok=False, error_redacted="intake_client_not_configured")
        url = f"{self.base_url}/internal/releases/{release_id}/push-payload"
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Token": self.internal_token,
        }
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.post(
                url,
                json={"source_occurrence_id": source_occurrence_id},
                headers=headers,
            )
            if resp.status_code != 200:
                return IntakePushPayloadResult(
                    ok=False,
                    status_code=resp.status_code,
                    error_redacted=f"intake_push_payload_http_{resp.status_code}",
                )
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("push_payload"), dict):
                return IntakePushPayloadResult(
                    ok=False,
                    status_code=200,
                    error_redacted="intake_push_payload_malformed",
                )
            payload = data["push_payload"]
            if not payload.get("title") or not payload.get("downloadUrl"):
                return IntakePushPayloadResult(
                    ok=False,
                    status_code=200,
                    error_redacted="intake_push_payload_missing_fields",
                )
            return IntakePushPayloadResult(ok=True, push_payload=payload, status_code=200)
        except Exception as exc:
            return IntakePushPayloadResult(
                ok=False,
                error_redacted=f"intake_push_payload_error:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()


class TriageFeedbackClient:
    """Posts isolated user decisions to movie-triage-api POST /feedback and validates exact ack."""

    is_shadow_stub: bool = False

    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str,
        timeout_seconds: float = 15.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client

    async def post_feedback(
        self,
        *,
        title: str,
        imdb_id: Optional[str],
        tmdb_id: Optional[int],
        decision: str,
        note: str,
    ) -> FeedbackPostResult:
        expected = decision.strip().upper()
        if expected not in ("WATCH", "SKIP"):
            return FeedbackPostResult(saved=False, error_redacted="invalid_feedback_decision")
        if not self.base_url:
            return FeedbackPostResult(saved=False, error_redacted="triage_api_url_not_configured")

        headers = {"Content-Type": "application/json"}
        if self.internal_token:
            headers["X-Internal-Token"] = self.internal_token

        body: dict[str, Any] = {
            "title": title,
            "decision": decision.strip().lower(),
            "note": note,
        }
        if imdb_id:
            body["imdb_id"] = imdb_id
        if tmdb_id is not None:
            body["tmdb_id"] = tmdb_id

        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_seconds)
        )
        try:
            resp = await client.post(f"{self.base_url}/feedback", json=body, headers=headers)
            if resp.status_code != 200:
                return FeedbackPostResult(
                    saved=False,
                    error_redacted=f"triage_feedback_http_{resp.status_code}",
                )
            data = resp.json()
            if not isinstance(data, dict):
                return FeedbackPostResult(saved=False, error_redacted="triage_feedback_non_dict")
            ack_status = str(data.get("status") or "").strip().lower()
            ack_decision = str(data.get("decision") or "").strip().upper()
            if ack_status == "saved" and ack_decision == expected:
                return FeedbackPostResult(saved=True, decision_saved=ack_decision)
            return FeedbackPostResult(
                saved=False,
                error_redacted=f"triage_feedback_ack_mismatch:status={ack_status},decision={ack_decision}",
            )
        except Exception as exc:
            return FeedbackPostResult(
                saved=False,
                error_redacted=f"triage_feedback_error:{redact_secrets(exc.__class__.__name__, max_chars=80)}",
            )
        finally:
            if owns_client:
                await client.aclose()


# =============================================================================
# Zero-Mutation Shadow Stubs (used automatically when SHADOW_REPLAY_ENABLED=true)
# =============================================================================


class ShadowStubTelegramClient(TelegramClient):
    is_shadow_stub: bool = True

    def __init__(self) -> None:
        super().__init__(bot_token="shadow-stub")
        self._next_msg_id = 5000
        self.sent_photos: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.edited_captions: list[dict[str, Any]] = []
        self.edited_texts: list[dict[str, Any]] = []
        self.sent_documents: list[dict[str, Any]] = []

    async def send_photo(
        self,
        *,
        chat_id: str,
        photo_url: str,
        caption_html: str,
        reply_markup: dict[str, Any],
        thread_id: Optional[int] = None,
    ) -> TelegramSendResult:
        self._next_msg_id += 1
        self.sent_photos.append(
            {"chat_id": chat_id, "photo_url": photo_url, "caption_html": caption_html, "reply_markup": reply_markup, "thread_id": thread_id, "message_id": self._next_msg_id}
        )
        return TelegramSendResult(outcome="ok", ack_chat_id=chat_id, ack_message_id=self._next_msg_id, ack_thread_id=thread_id)

    async def send_message(
        self,
        *,
        chat_id: str,
        text_html: str,
        reply_markup: Optional[dict[str, Any]] = None,
        thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> TelegramSendResult:
        self._next_msg_id += 1
        self.sent_messages.append(
            {"chat_id": chat_id, "text_html": text_html, "reply_markup": reply_markup, "thread_id": thread_id, "reply_to_message_id": reply_to_message_id, "message_id": self._next_msg_id}
        )
        return TelegramSendResult(outcome="ok", ack_chat_id=chat_id, ack_message_id=self._next_msg_id, ack_thread_id=thread_id)

    async def edit_message_caption(
        self,
        *,
        chat_id: str,
        message_id: int,
        caption_html: str,
        reply_markup: dict[str, Any],
    ) -> TelegramSendResult:
        self.edited_captions.append(
            {"chat_id": chat_id, "message_id": message_id, "caption_html": caption_html, "reply_markup": reply_markup}
        )
        return TelegramSendResult(outcome="ok", ack_chat_id=chat_id, ack_message_id=message_id)

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text_html: str,
        reply_markup: dict[str, Any],
    ) -> TelegramSendResult:
        self.edited_texts.append(
            {"chat_id": chat_id, "message_id": message_id, "text_html": text_html, "reply_markup": reply_markup}
        )
        return TelegramSendResult(outcome="ok", ack_chat_id=chat_id, ack_message_id=message_id)

    async def send_document(
        self,
        *,
        chat_id: str,
        filename: str,
        content_bytes: bytes,
        caption: Optional[str] = None,
        thread_id: Optional[int] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> TelegramSendResult:
        self._next_msg_id += 1
        self.sent_documents.append(
            {"chat_id": chat_id, "filename": filename, "content_bytes": content_bytes, "caption": caption, "thread_id": thread_id, "reply_to_message_id": reply_to_message_id, "message_id": self._next_msg_id}
        )
        return TelegramSendResult(outcome="ok", ack_chat_id=chat_id, ack_message_id=self._next_msg_id, ack_thread_id=thread_id)


class ShadowStubRadarrClient(RadarrClient):
    is_shadow_stub: bool = True

    def __init__(self) -> None:
        super().__init__(base_url="http://shadow-radarr.invalid", api_key="shadow-stub")
        self.tags: list[dict[str, Any]] = [{"id": 7, "label": "watched"}]
        self.root_folders: list[dict[str, Any]] = [{"id": 1, "path": "/media/movies"}]
        self.quality_profiles: list[dict[str, Any]] = [{"id": 4, "name": "Remux-2160p"}]
        self.download_clients: list[dict[str, Any]] = []
        self.movies: dict[int, dict[str, Any]] = {}
        self.queue: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.add_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.push_calls: list[dict[str, Any]] = []
        self._next_movie_id = 100

    async def get_tags(self) -> RadarrReadResult:
        return RadarrReadResult(ok=True, data=list(self.tags), status_code=200)

    async def get_root_folders(self) -> RadarrReadResult:
        return RadarrReadResult(ok=True, data=list(self.root_folders), status_code=200)

    async def get_quality_profiles(self) -> RadarrReadResult:
        return RadarrReadResult(ok=True, data=list(self.quality_profiles), status_code=200)

    async def get_download_clients(self) -> RadarrReadResult:
        return RadarrReadResult(ok=True, data=list(self.download_clients), status_code=200)

    async def get_movies_by_tmdb(self, tmdb_id: int) -> RadarrReadResult:
        matches = [dict(m) for m in self.movies.values() if m.get("tmdbId") == tmdb_id]
        return RadarrReadResult(ok=True, data=matches, status_code=200)

    async def get_movies_by_imdb(self, imdb_id: str) -> RadarrReadResult:
        low = imdb_id.lower()
        matches = [dict(m) for m in self.movies.values() if str(m.get("imdbId") or "").lower() == low]
        return RadarrReadResult(ok=True, data=matches, status_code=200)

    async def get_movie_by_id(self, movie_id: int) -> RadarrReadResult:
        m = self.movies.get(movie_id)
        if m is None:
            return RadarrReadResult(ok=False, status_code=404, error_redacted="radarr_http_404")
        return RadarrReadResult(ok=True, data=dict(m), status_code=200)

    async def get_queue_details(self, movie_id: int) -> RadarrReadResult:
        return RadarrReadResult(
            ok=True,
            data=[dict(q) for q in self.queue if q.get("movieId") == movie_id],
            status_code=200,
        )

    async def get_movie_history(self, movie_id: int) -> RadarrReadResult:
        return RadarrReadResult(
            ok=True,
            data=[dict(h) for h in self.history if h.get("movieId") == movie_id],
            status_code=200,
        )

    async def add_movie(self, payload: dict[str, Any]) -> RadarrWriteResult:
        self.add_calls.append(dict(payload))
        self._next_movie_id += 1
        entry = dict(payload)
        entry["id"] = self._next_movie_id
        entry.setdefault("hasFile", False)
        entry.setdefault("tags", [])
        self.movies[self._next_movie_id] = entry
        return RadarrWriteResult(outcome="ok", data=dict(entry), status_code=201)

    async def update_movie(self, movie_id: int, payload: dict[str, Any]) -> RadarrWriteResult:
        self.update_calls.append({"movie_id": movie_id, "payload": dict(payload)})
        if movie_id not in self.movies:
            return RadarrWriteResult(outcome="failed_client", status_code=404, error_redacted="radarr_http_404")
        updated = dict(payload)
        updated["id"] = movie_id
        self.movies[movie_id] = updated
        return RadarrWriteResult(outcome="ok", data=dict(updated), status_code=200)

    async def push_release(self, push_payload: dict[str, Any]) -> RadarrWriteResult:
        self.push_calls.append(dict(push_payload))
        return RadarrWriteResult(
            outcome="ok",
            data={"approved": True, "rejected": False, "rejections": []},
            status_code=200,
        )


class ShadowStubIntakePushClient(IntakePushClient):
    is_shadow_stub: bool = True

    def __init__(self) -> None:
        super().__init__(base_url="http://shadow-intake.invalid", internal_token="shadow-stub")
        self.calls: list[dict[str, str]] = []

    async def fetch_push_payload(self, *, release_id: str, source_occurrence_id: str) -> IntakePushPayloadResult:
        self.calls.append({"release_id": release_id, "source_occurrence_id": source_occurrence_id})
        return IntakePushPayloadResult(
            ok=True,
            push_payload={
                "title": "Shadow.Release.2024.2160p.REMUX-GROUP",
                "downloadUrl": "https://shadow-tracker.invalid/download/placeholder",
                "protocol": "torrent",
                "publishDate": "2026-11-25T12:00:00Z",
                "indexer": "torrentleech",
            },
            status_code=200,
        )


class ShadowStubTriageFeedbackClient(TriageFeedbackClient):
    is_shadow_stub: bool = True

    def __init__(self) -> None:
        super().__init__(base_url="http://shadow-triage.invalid", internal_token="shadow-stub")
        self.calls: list[dict[str, Any]] = []

    async def post_feedback(
        self,
        *,
        title: str,
        imdb_id: Optional[str],
        tmdb_id: Optional[int],
        decision: str,
        note: str,
    ) -> FeedbackPostResult:
        expected = decision.strip().upper()
        self.calls.append(
            {"title": title, "imdb_id": imdb_id, "tmdb_id": tmdb_id, "decision": expected, "note": note}
        )
        return FeedbackPostResult(saved=True, decision_saved=expected)
