from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import MAX_TL_BODY_BYTES, MAX_TL_COOKIE_BYTES
from .db import IntakeDB
from .security import PrivateFileError, read_owner_private_regular_file, redact_secrets

logger = logging.getLogger(__name__)

_TL_EXTERNAL_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_TL_CANONICAL_URL_RE = re.compile(r"^https://www\.torrentleech\.org/torrent/[1-9][0-9]{0,19}$")
_IMDB_VALID_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)
_IMDB_URL_TOKEN_RE = re.compile(r"imdb\.com/title/([^/?#\"'\s<>]+)", re.IGNORECASE)
_LOGIN_TEXT_RE = re.compile(
    r"\b(?:please\s+log\s*in|you\s+need\s+to\s+be\s+logged\s+in|member\s+login|sign\s+in\s+to\s+torrentleech)\b",
    re.IGNORECASE,
)

_MI_SECTION_GENERAL_RE = re.compile(r"^General\s*$", re.IGNORECASE)
_MI_SECTION_VIDEO_RE = re.compile(r"^Video(?:\s+#?\d+)?\s*$", re.IGNORECASE)
_MI_SECTION_AUDIO_RE = re.compile(r"^Audio(?:\s+#?\d+)?\s*$", re.IGNORECASE)
_MI_SECTION_TEXT_RE = re.compile(r"^(?:Text|Subtitles?)(?:\s+#?\d+)?\s*$", re.IGNORECASE)
_MI_SECTION_OTHER_RE = re.compile(r"^(?:Menu|Chapters?|Other|Image)(?:\s+#?\d+)?\s*$", re.IGNORECASE)

_MI_DURATION_HM_RE = re.compile(r"^\s*Duration\s*:\s*(\d{1,2})\s*h(?:\s*(\d{1,2})\s*min)?\b", re.IGNORECASE)
_MI_DURATION_M_RE = re.compile(r"^\s*Duration\s*:\s*(\d{1,4})\s*min\b", re.IGNORECASE)
_MI_DURATION_HMS_RE = re.compile(r"^\s*Duration\s*:\s*(\d{1,2}):(\d{2}):(\d{2})\b", re.IGNORECASE)
_MI_LANGUAGE_RE = re.compile(r"^\s*Language\s*:\s*([A-Za-z][A-Za-z\s\-/()]{0,48})$", re.IGNORECASE)

TL_TRANSIENT_FAILURE_OUTCOMES = frozenset(
    {
        "login_required",
        "listing_temporary_http",
        "cooldown_active",
    }
)

TL_INTEGRITY_CONFLICT_OUTCOMES = frozenset(
    {
        "listing_mismatch",
        "listing_id_conflict",
        "listing_id_malformed",
    }
)

TL_FAILED_OUTCOMES = TL_TRANSIENT_FAILURE_OUTCOMES | TL_INTEGRITY_CONFLICT_OUTCOMES


@dataclass(frozen=True)
class TorrentLeechResult:
    outcome: str
    # not_applicable | listing_id_found | listing_id_missing | listing_id_malformed
    # | listing_id_conflict | listing_mismatch | login_required
    # | listing_temporary_http | cooldown_active
    imdb_id: Optional[str] = None
    observed_imdb_ids: tuple[str, ...] = ()
    file_runtime_minutes: Optional[int] = None
    audio_languages: tuple[str, ...] = ()
    freeleech_badge: Optional[bool] = None
    sanitized_description: Optional[str] = None
    sanitized_nfo: Optional[str] = None
    diagnostic: Optional[str] = None


def build_canonical_tl_url(external_id: str) -> Optional[str]:
    clean = (external_id or "").strip()
    if not _TL_EXTERNAL_ID_RE.fullmatch(clean):
        return None
    url = f"https://www.torrentleech.org/torrent/{clean}"
    if not _TL_CANONICAL_URL_RE.fullmatch(url):
        return None
    return url


def load_tl_cookies(cookie_file: Path) -> dict[str, str]:
    """
    Read TorrentLeech cookies strictly from an owner-only regular file (<= 16 KB,
    O_RDONLY | O_NOFOLLOW | O_NONBLOCK, fstat verified). Only cookies scoped to
    torrentleech.org are accepted.
    """
    raw = read_owner_private_regular_file(cookie_file, max_bytes=MAX_TL_COOKIE_BYTES)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateFileError("Cookie file is not valid UTF-8 JSON") from exc

    cookies: dict[str, str] = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
        parsed = parsed["cookies"]

    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip().lower()
            if domain and not (
                domain == "torrentleech.org" or domain.endswith(".torrentleech.org")
            ):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name and value:
                cookies[name] = value
    elif isinstance(parsed, dict):
        for k, v in parsed.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                cookies[k.strip()] = v.strip()
    else:
        raise PrivateFileError("Unsupported cookie file JSON structure")

    if not cookies:
        raise PrivateFileError("Cookie file contains no usable TorrentLeech cookies")
    return cookies


def parse_mediainfo_file_signals(nfo_text: Optional[str]) -> tuple[Optional[int], list[str]]:
    """
    Parse MediaInfo/NFO strictly by section headers:
      - Actual file duration ONLY from General (preferred) or Video sections (never catalogue runtime).
      - Audio track languages ONLY from Audio sections (strictly separated from Text/Subtitle sections).
    """
    if not nfo_text:
        return None, []

    section: Optional[str] = None
    general_durations: set[int] = set()
    video_durations: set[int] = set()
    audio_langs: list[str] = []
    seen_langs: set[str] = set()

    for raw_line in nfo_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _MI_SECTION_GENERAL_RE.match(line):
            section = "general"
            continue
        if _MI_SECTION_VIDEO_RE.match(line):
            section = "video"
            continue
        if _MI_SECTION_AUDIO_RE.match(line):
            section = "audio"
            continue
        if _MI_SECTION_TEXT_RE.match(line):
            section = "text"
            continue
        if _MI_SECTION_OTHER_RE.match(line):
            section = "other"
            continue

        if section in ("general", "video"):
            minutes: Optional[int] = None
            m_hm = _MI_DURATION_HM_RE.match(line)
            if m_hm:
                h_val = int(m_hm.group(1))
                m_val = int(m_hm.group(2) or 0)
                if m_val < 60:
                    total = h_val * 60 + m_val
                    if 1 <= total <= 1440:
                        minutes = total
            else:
                m_min = _MI_DURATION_M_RE.match(line)
                if m_min:
                    total = int(m_min.group(1))
                    if 1 <= total <= 1440:
                        minutes = total
                else:
                    m_hms = _MI_DURATION_HMS_RE.match(line)
                    if m_hms:
                        h_val, m_val, s_val = int(m_hms.group(1)), int(m_hms.group(2)), int(m_hms.group(3))
                        if m_val < 60 and s_val < 60:
                            total = h_val * 60 + m_val + (1 if s_val >= 30 else 0)
                            if 1 <= total <= 1440:
                                minutes = total
            if minutes is not None:
                if section == "general":
                    general_durations.add(minutes)
                else:
                    video_durations.add(minutes)

        elif section == "audio":
            m_lang = _MI_LANGUAGE_RE.match(line)
            if m_lang:
                lang = re.sub(r"\s+", " ", m_lang.group(1)).strip()
                # Strip parenthetical notes like "English (US)" -> "English"
                lang = re.sub(r"\s*\([^)]*\)\s*", "", lang).strip()
                low = lang.lower()
                if lang and low not in seen_langs:
                    seen_langs.add(low)
                    audio_langs.append(lang)

    file_runtime: Optional[int] = None
    if len(general_durations) == 1:
        file_runtime = next(iter(general_durations))
    elif not general_durations and len(video_durations) == 1:
        file_runtime = next(iter(video_durations))

    return file_runtime, audio_langs[:6]


def parse_retry_after_seconds(header_value: Optional[str], *, failures: int = 0) -> float:
    default_backoff = min(1800.0, 30.0 * (2 ** min(failures, 6)))
    if not header_value or not header_value.strip():
        return default_backoff
    text = header_value.strip()
    if text.isdigit():
        return float(max(5, min(int(text), 3600)))
    try:
        target = email.utils.parsedate_to_datetime(text)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        return max(5.0, min(delta, 3600.0))
    except (TypeError, ValueError):
        return default_backoff


_VOID_HTML_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)


class _TorrentLeechHtmlParser(HTMLParser):
    """Stdlib HTML parser enforcing strict TL heading, login, IMDb, and NFO invariants."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_login_form = False
        self.has_password_input = False
        self.in_title = False
        self.page_title_parts: list[str] = []

        # Heading capture: every element whose id is torrentnameid / torrentName
        self._heading_stack: list[dict[str, Any]] = []
        self.headings: list[str] = []
        self.freeleech_badge_found = False

        # IMDb token extraction
        self.imdb_tokens: list[str] = []
        self._imdb_field_depth = 0
        self._imdb_field_saw_href = False
        self._imdb_field_parts: list[str] = []

        # NFO / MediaInfo and description capture
        self._nfo_depth = 0
        self._nfo_blocks: list[list[str]] = []
        self._desc_depth = 0
        self._desc_blocks: list[list[str]] = []
        self._body_text_parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        low_tag = tag.lower()
        is_void = low_tag in _VOID_HTML_TAGS
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        elem_id = attr_map.get("id", "").strip()
        elem_id_low = elem_id.lower()
        classes = {c.strip().lower() for c in attr_map.get("class", "").split() if c.strip()}

        if low_tag in ("script", "style"):
            self._ignored_depth += 1
            return

        if low_tag == "title":
            self.in_title = True

        if low_tag == "form":
            action = attr_map.get("action", "").lower()
            if "login" in action or "signin" in action:
                self.has_login_form = True

        if low_tag == "input" and attr_map.get("type", "").strip().lower() == "password":
            self.has_password_input = True

        # Check heading containers (#torrentnameid / #torrentName)
        if elem_id_low in ("torrentnameid", "torrentname") and not is_void:
            self._heading_stack.append({"depth": 1, "parts": [], "badge_depth": 0})
        elif self._heading_stack and not is_void:
            top = self._heading_stack[-1]
            top["depth"] += 1
            # Strip ONLY the exact known freeleech badge: span.new.label.labelfreeleech
            if low_tag == "span" and {"new", "label", "labelfreeleech"}.issubset(classes):
                self.freeleech_badge_found = True
                top["badge_depth"] += 1
            elif top["badge_depth"] > 0:
                top["badge_depth"] += 1

        # IMDb links and explicit IMDb attributes/elements
        href = attr_map.get("href", "")
        if href:
            href_matches = list(_IMDB_URL_TOKEN_RE.finditer(href))
            if href_matches:
                if self._imdb_field_depth > 0:
                    self._imdb_field_saw_href = True
                for match in href_matches:
                    self.imdb_tokens.append(match.group(1).strip())

        data_imdb = attr_map.get("data-imdb", "").strip() or attr_map.get("data-imdb-id", "").strip()
        if data_imdb:
            self.imdb_tokens.append(data_imdb)

        if not is_void:
            if elem_id_low in ("imdb", "imdbid", "imdb_id", "torrentimdb"):
                self._imdb_field_depth = 1
                self._imdb_field_saw_href = False
                self._imdb_field_parts = []
            elif self._imdb_field_depth > 0:
                self._imdb_field_depth += 1

            # NFO / MediaInfo capture
            if self._nfo_depth > 0:
                self._nfo_depth += 1
            elif (
                low_tag == "pre"
                or elem_id_low in ("nfo", "nfo_text", "mediainfo", "torrentnfo")
                or bool(classes & {"nfo", "mediainfo", "nfo-text"})
            ):
                self._nfo_depth = 1
                self._nfo_blocks.append([])

            # Description capture
            if self._desc_depth > 0:
                self._desc_depth += 1
            elif elem_id_low in ("description", "torrentdescription", "details-description") or bool(
                classes & {"torrent-description", "description-body", "details-description"}
            ):
                self._desc_depth = 1
                self._desc_blocks.append([])

    def handle_endtag(self, tag: str) -> None:
        low_tag = tag.lower()
        if low_tag in _VOID_HTML_TAGS:
            return
        if low_tag in ("script", "style"):
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
            return

        if low_tag == "title":
            self.in_title = False

        if self._heading_stack:
            top = self._heading_stack[-1]
            if top["badge_depth"] > 0:
                top["badge_depth"] -= 1
            top["depth"] -= 1
            if top["depth"] == 0:
                finished = self._heading_stack.pop()
                normalized = re.sub(r"\s+", " ", "".join(finished["parts"])).strip()
                self.headings.append(normalized)

        if self._imdb_field_depth > 0:
            self._imdb_field_depth -= 1
            if self._imdb_field_depth == 0:
                if not self._imdb_field_saw_href:
                    raw_field = re.sub(r"\s+", " ", "".join(self._imdb_field_parts)).strip()
                    if raw_field and raw_field.lower() not in ("n/a", "-", "none", "unknown", "imdb", "imdb:"):
                        url_matches = _IMDB_URL_TOKEN_RE.findall(raw_field)
                        if url_matches:
                            self.imdb_tokens.extend(url_matches)
                        else:
                            for tok in re.split(r"[\s,;]+", raw_field):
                                cleaned = tok.strip()
                                if cleaned and cleaned.lower() not in ("imdb", "imdb:", "id", "id:", "n/a", "-"):
                                    self.imdb_tokens.append(cleaned)
                self._imdb_field_saw_href = False

        if self._nfo_depth > 0:
            self._nfo_depth -= 1

        if self._desc_depth > 0:
            self._desc_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        if self.in_title:
            self.page_title_parts.append(data)
        if self._heading_stack:
            top = self._heading_stack[-1]
            if top["badge_depth"] == 0:
                top["parts"].append(data)
        if self._imdb_field_depth > 0:
            self._imdb_field_parts.append(data)
        if self._nfo_depth > 0 and self._nfo_blocks:
            self._nfo_blocks[-1].append(data)
        if self._desc_depth > 0 and self._desc_blocks:
            self._desc_blocks[-1].append(data)
        if len(self._body_text_parts) < 200:
            self._body_text_parts.append(data)


def parse_tl_listing_html(html_text: str, *, exact_release_name: str) -> TorrentLeechResult:
    """
    Parse and verify a TorrentLeech listing page against all handover invariants:
      1. Detect login HTML even on HTTP 200 -> login_required.
      2. Require >=1 #torrentnameid/#torrentName heading AND require ALL headings to match
         exact_release_name after whitespace normalization and stripping ONLY
         span.new.label.labelfreeleech -> else listing_mismatch (discard metadata).
      3. Produce distinct IMDb outcomes: listing_id_found, listing_id_missing,
         listing_id_malformed, listing_id_conflict.
      4. Extract sanitized MediaInfo (file runtime & audio strictly separate from subtitles)
         and sanitized description.
    """
    parser = _TorrentLeechHtmlParser()
    parser.feed(html_text)
    parser.close()

    page_title = re.sub(r"\s+", " ", "".join(parser.page_title_parts)).strip().lower()
    body_sample = re.sub(r"\s+", " ", "".join(parser._body_text_parts)).strip()

    # 1) Login detection even on HTTP 200
    if not parser.headings and (
        parser.has_login_form
        or parser.has_password_input
        or "login" in page_title
        or "sign in" in page_title
        or bool(_LOGIN_TEXT_RE.search(body_sample))
    ):
        return TorrentLeechResult(
            outcome="login_required",
            diagnostic="login_html_detected_on_200",
        )
    if parser.has_login_form and parser.has_password_input:
        return TorrentLeechResult(
            outcome="login_required",
            diagnostic="login_form_present",
        )

    # 2) Strict ALL-heading match
    expected_norm = re.sub(r"\s+", " ", (exact_release_name or "").strip())
    if not expected_norm or not parser.headings:
        return TorrentLeechResult(
            outcome="listing_mismatch",
            diagnostic="missing_release_heading",
        )

    for heading in parser.headings:
        if heading != expected_norm:
            return TorrentLeechResult(
                outcome="listing_mismatch",
                diagnostic="heading_mismatch",
            )

    # 3) Distinct IMDb extraction outcomes
    valid_ids: list[str] = []
    seen_valid: set[str] = set()
    malformed_found = False

    for raw_tok in parser.imdb_tokens:
        tok = raw_tok.strip().rstrip("/")
        if not tok:
            continue
        if _IMDB_VALID_RE.fullmatch(tok):
            low = tok.lower()
            if low not in seen_valid:
                seen_valid.add(low)
                valid_ids.append(low)
        else:
            malformed_found = True

    if malformed_found:
        outcome = "listing_id_malformed"
        chosen_imdb: Optional[str] = None
    elif len(valid_ids) > 1:
        outcome = "listing_id_conflict"
        chosen_imdb = None
    elif len(valid_ids) == 1:
        outcome = "listing_id_found"
        chosen_imdb = valid_ids[0]
    else:
        outcome = "listing_id_missing"
        chosen_imdb = None

    # 4) MediaInfo (parsed before redaction so Duration/Language lines are intact) + sanitization
    raw_nfo = "\n\n".join("".join(block).strip() for block in parser._nfo_blocks if "".join(block).strip())
    raw_desc = "\n\n".join("".join(block).strip() for block in parser._desc_blocks if "".join(block).strip())

    file_runtime, audio_langs = parse_mediainfo_file_signals(raw_nfo)
    sanitized_nfo = redact_secrets(raw_nfo, max_chars=2500) or None
    sanitized_desc = redact_secrets(raw_desc, max_chars=1500) or None

    return TorrentLeechResult(
        outcome=outcome,
        imdb_id=chosen_imdb,
        observed_imdb_ids=tuple(valid_ids),
        file_runtime_minutes=file_runtime,
        audio_languages=tuple(audio_langs),
        freeleech_badge=parser.freeleech_badge_found,
        sanitized_description=sanitized_desc,
        sanitized_nfo=sanitized_nfo,
    )


class TorrentLeechScraper:
    """
    Serialized single-worker TorrentLeech listing scraper enforcing:
      - owner-only regular cookie file (<= 16 KB, O_NOFOLLOW)
      - canonical HTTPS allowlist only, redirects disabled, never fetch download_url
      - bounded body size (<= 1,000,000 bytes) and timeouts
      - persistent Retry-After / failure cooldowns in intake.db
    """

    def __init__(
        self,
        *,
        db: IntakeDB,
        cookie_file: Path,
        enabled: bool = True,
        min_interval_seconds: float = 1.5,
        timeout_seconds: float = 15.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.db = db
        self.cookie_file = cookie_file
        self.enabled = enabled
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self._external_client = http_client
        self._semaphore = asyncio.Semaphore(1)
        self._last_request_monotonic: float = 0.0

    async def fetch_listing(
        self,
        *,
        indexer_norm: str,
        external_id: str,
        exact_release_name: str,
    ) -> TorrentLeechResult:
        if not self.enabled or indexer_norm != "torrentleech":
            return TorrentLeechResult(outcome="not_applicable")

        url = build_canonical_tl_url(external_id)
        if url is None:
            return TorrentLeechResult(
                outcome="listing_mismatch",
                diagnostic="invalid_tl_external_id",
            )

        async with self._semaphore:
            cooldown_active = await asyncio.to_thread(self.db.is_tl_cooldown_active)
            if cooldown_active:
                return TorrentLeechResult(
                    outcome="cooldown_active",
                    diagnostic="tl_cooldown_active",
                )

            try:
                cookies = await asyncio.to_thread(load_tl_cookies, self.cookie_file)
            except PrivateFileError as exc:
                logger.warning("TorrentLeech cookie validation failed: %s", redact_secrets(str(exc), max_chars=200))
                return TorrentLeechResult(
                    outcome="login_required",
                    diagnostic="cookie_file_unavailable_or_unsafe",
                )

            if self.min_interval_seconds > 0.0 and self._last_request_monotonic > 0.0:
                elapsed = time.monotonic() - self._last_request_monotonic
                if elapsed < self.min_interval_seconds:
                    await asyncio.sleep(self.min_interval_seconds - elapsed)

            self._last_request_monotonic = time.monotonic()
            return await self._perform_http_fetch(
                url=url,
                cookies=cookies,
                exact_release_name=exact_release_name,
            )

    async def _record_cooldown(self, *, retry_after: Optional[str], reason: str) -> None:
        row = await asyncio.to_thread(self.db.get_tl_cooldown)
        failures = int(row["consecutive_failures"]) if row else 0
        seconds = parse_retry_after_seconds(retry_after, failures=failures)
        await asyncio.to_thread(
            self.db.record_tl_cooldown,
            cooldown_seconds=seconds,
            reason=reason,
        )

    async def _perform_http_fetch(
        self,
        *,
        url: str,
        cookies: dict[str, str],
        exact_release_name: str,
    ) -> TorrentLeechResult:
        headers = {
            "User-Agent": "movie-intake-api/1.0",
            "Accept": "text/html,application/xhtml+xml",
        }
        timeout = httpx.Timeout(connect=10.0, read=self.timeout_seconds, write=10.0, pool=10.0)
        owns_client = self._external_client is None
        client = (
            self._external_client
            if self._external_client is not None
            else httpx.AsyncClient(follow_redirects=False, timeout=timeout)
        )
        try:
            async with client.stream(
                "GET",
                url,
                cookies=cookies,
                headers=headers,
                follow_redirects=False,
            ) as resp:
                status = resp.status_code

                # Redirects are strictly disabled — inspect without following
                if 300 <= status < 400:
                    loc = (resp.headers.get("location") or "").lower()
                    if "login" in loc or "signin" in loc:
                        return TorrentLeechResult(
                            outcome="login_required",
                            diagnostic=f"redirect_to_login:{status}",
                        )
                    await self._record_cooldown(
                        retry_after=resp.headers.get("retry-after"),
                        reason=f"unexpected_redirect_{status}",
                    )
                    return TorrentLeechResult(
                        outcome="listing_temporary_http",
                        diagnostic=f"unexpected_redirect:{status}",
                    )

                if status == 401:
                    return TorrentLeechResult(
                        outcome="login_required",
                        diagnostic="http_401",
                    )

                if status in (429, 500, 502, 503, 504):
                    await self._record_cooldown(
                        retry_after=resp.headers.get("retry-after"),
                        reason=f"http_{status}",
                    )
                    return TorrentLeechResult(
                        outcome="listing_temporary_http",
                        diagnostic=f"http_{status}",
                    )

                if status == 403:
                    # Bounded read to distinguish login gate vs temporary block/rate limit
                    sample = bytearray()
                    async for chunk in resp.aiter_bytes():
                        sample.extend(chunk)
                        if len(sample) > 65_536:
                            break
                    text_sample = sample.decode("utf-8", errors="replace")
                    if _LOGIN_TEXT_RE.search(text_sample) or 'type="password"' in text_sample.lower():
                        return TorrentLeechResult(outcome="login_required", diagnostic="http_403_login")
                    await self._record_cooldown(
                        retry_after=resp.headers.get("retry-after"),
                        reason="http_403",
                    )
                    return TorrentLeechResult(outcome="listing_temporary_http", diagnostic="http_403")

                if status != 200:
                    return TorrentLeechResult(
                        outcome="listing_temporary_http",
                        diagnostic=f"http_{status}",
                    )

                # Enforce strict 1,000,000-byte streaming cap
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_TL_BODY_BYTES:
                        return TorrentLeechResult(
                            outcome="listing_temporary_http",
                            diagnostic="response_body_exceeded_limit",
                        )

                html_text = buf.decode("utf-8", errors="replace")
                result = parse_tl_listing_html(html_text, exact_release_name=exact_release_name)
                if result.outcome != "login_required":
                    await asyncio.to_thread(self.db.clear_tl_cooldown)
                return result
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            await self._record_cooldown(retry_after=None, reason=exc.__class__.__name__)
            return TorrentLeechResult(
                outcome="listing_temporary_http",
                diagnostic=redact_secrets(exc.__class__.__name__, max_chars=100),
            )
        finally:
            if owns_client:
                await client.aclose()
