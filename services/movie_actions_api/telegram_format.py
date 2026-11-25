from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Optional

from .security import redact_secrets

TELEGRAM_CAPTION_UTF16_LIMIT = 1024
TELEGRAM_TEXT_UTF16_LIMIT = 4096

_SAFE_POSTER_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png|webp)$")
_IMDB_ID_RE = re.compile(r"^tt\d{7,10}$", re.IGNORECASE)


def utf16_len(text: str) -> int:
    """Return exact Telegram UTF-16 code unit length (non-BMP emoji count as 2 units)."""
    return len(text.encode("utf-16-le")) // 2


def escape_html(value: Any) -> str:
    return html.escape(str(value), quote=True)


def truncate_plain_for_html_budget(raw: str, max_escaped_utf16: int) -> str:
    """
    Truncate plain text so that `html.escape(result, quote=True)` fits strictly within
    `max_escaped_utf16` UTF-16 code units, never splitting inside an HTML tag, HTML entity,
    or UTF-16 surrogate pair.
    """
    if max_escaped_utf16 <= 0 or not raw:
        return ""
    full_escaped = escape_html(raw)
    if utf16_len(full_escaped) <= max_escaped_utf16:
        return full_escaped

    ellipsis = "..."
    ellipsis_units = utf16_len(ellipsis)
    if max_escaped_utf16 <= ellipsis_units:
        # Fit as many plain characters as possible without ellipsis
        out: list[str] = []
        used = 0
        for ch in raw:
            esc = escape_html(ch)
            units = utf16_len(esc)
            if used + units > max_escaped_utf16:
                break
            out.append(esc)
            used += units
        return "".join(out)

    limit = max_escaped_utf16 - ellipsis_units
    out_chars: list[str] = []
    used = 0
    for ch in raw:
        esc = escape_html(ch)
        units = utf16_len(esc)
        if used + units > limit:
            break
        out_chars.append(esc)
        used += units
    return "".join(out_chars).rstrip() + ellipsis


def safe_poster_url(poster_path: Optional[str]) -> Optional[str]:
    if not poster_path or not isinstance(poster_path, str):
        return None
    cleaned = poster_path.strip()
    if _SAFE_POSTER_PATH_RE.fullmatch(cleaned):
        return f"https://image.tmdb.org/t/p/w500{cleaned}"
    return None


def _assemble_lines_within_budget(lines: list[tuple[str, str, str]], max_units: int) -> str:
    """
    Assemble HTML lines where each entry is `(prefix_html, plain_body, suffix_html)`.
    Guarantees final UTF-16 length <= max_units without ever cutting inside HTML tags/entities.
    """
    rendered: list[str] = []
    used = 0
    for prefix_html, plain_body, suffix_html in lines:
        sep = "\n" if rendered else ""
        sep_units = utf16_len(sep)
        wrapper_units = utf16_len(prefix_html) + utf16_len(suffix_html)
        remaining = max_units - used - sep_units - wrapper_units
        if remaining <= 0:
            break
        if plain_body == "":
            line = f"{prefix_html}{suffix_html}"
        else:
            fitted_body = truncate_plain_for_html_budget(plain_body, remaining)
            if not fitted_body and plain_body.strip():
                break
            line = f"{prefix_html}{fitted_body}{suffix_html}"
        line_units = sep_units + utf16_len(line)
        if used + line_units > max_units:
            break
        rendered.append(line)
        used += line_units
    return "\n".join(rendered)


def build_card_html(
    card_payload: dict[str, Any],
    *,
    max_utf16_units: int,
) -> str:
    """Format Telegram HTML card strictly within `max_utf16_units` (1024 caption or 4096 text)."""
    delivery_kind = str(card_payload.get("delivery_kind") or "evidence_review")
    release_name = redact_secrets(str(card_payload.get("exact_release_name") or ""), max_chars=260)
    indexer = redact_secrets(str(card_payload.get("indexer") or "unknown"), max_chars=60)
    category = redact_secrets(str(card_payload.get("category") or ""), max_chars=80)
    size_gb = card_payload.get("size_gb")
    freeleech = card_payload.get("freeleech")
    radarr_note = redact_secrets(str(card_payload.get("radarr_note") or ""), max_chars=160)
    conflict_reason = redact_secrets(str(card_payload.get("conflict_reason") or ""), max_chars=300)
    tl_outcome = redact_secrets(str(card_payload.get("tl_outcome") or ""), max_chars=60)

    verified_title = redact_secrets(str(card_payload.get("verified_title") or ""), max_chars=160)
    verified_year = card_payload.get("verified_year")
    verified_tmdb = card_payload.get("verified_tmdb_id")
    verified_imdb = card_payload.get("verified_imdb_id")
    card_revision = int(card_payload.get("card_revision") or 1)

    lines: list[tuple[str, str, str]] = []
    if delivery_kind == "actionable_grab":
        header = f"{verified_title} ({verified_year})" if verified_title and verified_year else (verified_title or "Verified Movie")
        if card_revision > 1:
            header = f"{header} [Updated rev {card_revision}]"
        lines.append(("🎬 <b>", header, "</b>"))
    elif delivery_kind == "alternative_grab":
        header = f"{verified_title} ({verified_year})" if verified_title and verified_year else (verified_title or "Verified Movie")
        if card_revision > 1:
            header = f"{header} [Updated rev {card_revision}]"
        lines.append(("🔁 <b>Alternative Grab (Grab Anyway): ", header, "</b>"))
        rej_class = redact_secrets(str(card_payload.get("bound_rejection_class") or ""), max_chars=80)
        if rej_class:
            lines.append(("<b>Quality Policy:</b> ", rej_class, ""))
    else:
        header = "Evidence Review (Manual Check Required)"
        if card_revision > 1:
            header = f"{header} [Updated rev {card_revision}]"
        lines.append(("⚠️ <b>", header, "</b>"))

    lines.append(("<code>", release_name, "</code>"))

    meta_parts: list[str] = [f"Indexer: {indexer}"]
    if category:
        meta_parts.append(category)
    if isinstance(size_gb, (int, float)) and not isinstance(size_gb, bool):
        meta_parts.append(f"{float(size_gb):.2f} GB")
    if freeleech is True:
        meta_parts.append("Freeleech")
    elif freeleech is False:
        meta_parts.append("Not Freeleech")
    lines.append(("", " | ".join(meta_parts), ""))

    tech_parts: list[str] = []
    file_rt = card_payload.get("file_runtime_minutes")
    if isinstance(file_rt, int) and not isinstance(file_rt, bool) and file_rt > 0:
        tech_parts.append(f"File Runtime: {file_rt}m")
    audio_langs = card_payload.get("audio_languages")
    if isinstance(audio_langs, list) and audio_langs:
        clean_audio = [redact_secrets(str(a), max_chars=30) for a in audio_langs[:5] if str(a).strip()]
        if clean_audio:
            tech_parts.append(f"Audio: {', '.join(clean_audio)}")
    if tech_parts:
        lines.append(("", " | ".join(tech_parts), ""))

    if radarr_note:
        lines.append(("<b>Radarr:</b> ", radarr_note, ""))

    if delivery_kind == "evidence_review":
        if conflict_reason:
            lines.append(("<b>Review Reason:</b> ", conflict_reason, ""))
        if tl_outcome:
            lines.append(("<b>TL Check:</b> ", tl_outcome, ""))
        obs_parts: list[str] = []
        if card_payload.get("observed_meta_imdb"):
            obs_parts.append(f"meta_imdb={redact_secrets(str(card_payload['observed_meta_imdb']), max_chars=24)}")
        if card_payload.get("observed_meta_tmdb"):
            obs_parts.append(f"meta_tmdb={card_payload['observed_meta_tmdb']}")
        if card_payload.get("observed_tl_imdb"):
            obs_parts.append(f"tl_imdb={redact_secrets(str(card_payload['observed_tl_imdb']), max_chars=24)}")
        if obs_parts:
            lines.append(("<b>Observed IDs:</b> ", ", ".join(obs_parts), ""))
    else:
        link_bits: list[str] = []
        if isinstance(verified_imdb, str) and _IMDB_ID_RE.fullmatch(verified_imdb.strip()):
            clean_imdb = verified_imdb.strip().lower()
            link_bits.append(f'<a href="https://www.imdb.com/title/{clean_imdb}/">IMDb ({clean_imdb})</a>')
        if isinstance(verified_tmdb, int) and not isinstance(verified_tmdb, bool) and verified_tmdb > 0:
            link_bits.append(f'<a href="https://www.themoviedb.org/movie/{verified_tmdb}">TMDB ({verified_tmdb})</a>')
        if link_bits:
            lines.append((" | ".join(link_bits), "", ""))

    return _assemble_lines_within_budget(lines, max_utf16_units)


def build_inline_keyboard(
    *,
    delivery_kind: str,
    public_base_url: str,
    action_tokens: dict[str, str],
) -> dict[str, Any]:
    """Build Telegram InlineKeyboardMarkup for verified cards; empty keyboard for evidence_review."""
    if delivery_kind == "evidence_review" or not action_tokens:
        return {"inline_keyboard": []}

    base = public_base_url.rstrip("/")
    rows: list[list[dict[str, str]]] = []
    if delivery_kind == "actionable_grab" and "add_grab" in action_tokens:
        rows.append([{"text": "⬇️ Add & Grab", "url": f"{base}/action/{action_tokens['add_grab']}"}])
    elif delivery_kind == "alternative_grab" and "alternative_grab" in action_tokens:
        rows.append(
            [{"text": "🔁 Grab Anyway (Alternative)", "url": f"{base}/action/{action_tokens['alternative_grab']}"}]
        )

    watched_row: list[dict[str, str]] = []
    if "watched_good" in action_tokens:
        watched_row.append({"text": "👍 Watched – Good", "url": f"{base}/action/{action_tokens['watched_good']}"})
    if "watched_bad" in action_tokens:
        watched_row.append({"text": "👎 Watched – Bad", "url": f"{base}/action/{action_tokens['watched_bad']}"})
    if watched_row:
        rows.append(watched_row)

    if "skip" in action_tokens:
        rows.append([{"text": "⏭️ Skip", "url": f"{base}/action/{action_tokens['skip']}"}])

    return {"inline_keyboard": rows}


@dataclass(frozen=True)
class FormattedEvaluationReply:
    html_text: str
    attach_txt: bool
    txt_filename: Optional[str] = None
    txt_content: Optional[str] = None


def _build_evaluation_plain_text(evaluation: dict[str, Any]) -> str:
    ident = evaluation.get("identification") if isinstance(evaluation.get("identification"), dict) else {}
    ratings = evaluation.get("ratings") if isinstance(evaluation.get("ratings"), dict) else {}
    recog = evaluation.get("recognition") if isinstance(evaluation.get("recognition"), dict) else {}
    aid = evaluation.get("decision_aid") if isinstance(evaluation.get("decision_aid"), dict) else {}

    out: list[str] = []
    title = redact_secrets(str(ident.get("canonical_title") or "Unknown"), max_chars=200)
    year = ident.get("year")
    verdict = redact_secrets(str(aid.get("verdict") or "UNKNOWN"), max_chars=24)
    out.append(f"Movie Evaluation Report: {title} ({year})" if year else f"Movie Evaluation Report: {title}")
    out.append(f"Verdict: {verdict} (Recommendation only — not an automatic grab)")
    if aid.get("verdict_reason"):
        out.append(f"Reason: {redact_secrets(str(aid['verdict_reason']), max_chars=None)}")
    if ident.get("director"):
        out.append(f"Director: {redact_secrets(str(ident['director']), max_chars=200)}")
    if ident.get("match_confidence"):
        out.append(f"Match Confidence: {redact_secrets(str(ident['match_confidence']), max_chars=40)}")
    if ident.get("imdb_id") or ident.get("tmdb_id"):
        out.append(f"IDs: imdb={ident.get('imdb_id')} tmdb={ident.get('tmdb_id')}")
    if ident.get("runtime_minutes"):
        out.append(f"Runtime: {ident.get('runtime_minutes')} min")
    genres = ident.get("genres")
    if isinstance(genres, list) and genres:
        out.append(f"Genres: {', '.join(redact_secrets(str(g), max_chars=40) for g in genres)}")
    if ident.get("disambiguation_note"):
        out.append(f"Disambiguation: {redact_secrets(str(ident['disambiguation_note']), max_chars=None)}")

    rating_bits: list[str] = []
    if ratings.get("rotten_tomatoes_critics") is not None:
        rating_bits.append(f"RT Critics {ratings['rotten_tomatoes_critics']}%")
    if ratings.get("rotten_tomatoes_audience") is not None:
        rating_bits.append(f"RT Audience {ratings['rotten_tomatoes_audience']}%")
    if ratings.get("metacritic") is not None:
        rating_bits.append(f"Metacritic {ratings['metacritic']}")
    if ratings.get("imdb") is not None:
        rating_bits.append(f"IMDb {ratings['imdb']}/10")
    if ratings.get("letterboxd") is not None:
        rating_bits.append(f"Letterboxd {ratings['letterboxd']}/5")
    if rating_bits:
        out.append(f"Ratings: {' | '.join(rating_bits)}")

    awards = recog.get("awards_highlights")
    if isinstance(awards, list) and awards:
        out.append(f"Awards: {'; '.join(redact_secrets(str(a), max_chars=None) for a in awards)}")
    if recog.get("critic_consensus"):
        out.append(f"Consensus: {redact_secrets(str(recog['critic_consensus']), max_chars=None)}")
    if aid.get("one_line_pitch"):
        out.append(f"Pitch: {redact_secrets(str(aid['one_line_pitch']), max_chars=None)}")
    sim = aid.get("similar_to")
    if isinstance(sim, list) and sim:
        out.append(f"Similar To: {', '.join(redact_secrets(str(s), max_chars=120) for s in sim)}")
    flags = aid.get("red_flags")
    if isinstance(flags, list) and flags:
        out.append(f"Red Flags: {'; '.join(redact_secrets(str(f), max_chars=None) for f in flags)}")
    return "\n".join(out) + "\n"


def format_evaluation_reply(
    *,
    release_id: str,
    evaluation: Optional[dict[str, Any]],
    error_redacted: Optional[str],
    max_utf16_units: int = TELEGRAM_TEXT_UTF16_LIMIT,
) -> FormattedEvaluationReply:
    """
    Format evaluation reply strictly within Telegram's 4,096 UTF-16 budget.
    When full details exceed budget, returns a compact summary within budget + full `.txt` attachment.
    """
    if not isinstance(evaluation, dict):
        clean_err = redact_secrets(error_redacted or "Evaluator unavailable", max_chars=600)
        html_text = _assemble_lines_within_budget(
            [
                ("⚠️ <b>Evaluation Unavailable</b>", "", ""),
                ("<code>", clean_err, "</code>"),
            ],
            max_utf16_units,
        )
        return FormattedEvaluationReply(html_text=html_text, attach_txt=False)

    ident = evaluation.get("identification") if isinstance(evaluation.get("identification"), dict) else {}
    ratings = evaluation.get("ratings") if isinstance(evaluation.get("ratings"), dict) else {}
    recog = evaluation.get("recognition") if isinstance(evaluation.get("recognition"), dict) else {}
    aid = evaluation.get("decision_aid") if isinstance(evaluation.get("decision_aid"), dict) else {}

    title = redact_secrets(str(ident.get("canonical_title") or "Unknown"), max_chars=None)
    year = ident.get("year")
    verdict = redact_secrets(str(aid.get("verdict") or "UNKNOWN"), max_chars=30)
    confidence = redact_secrets(str(ident.get("match_confidence") or ""), max_chars=30)
    verdict_icon = {"WATCH": "✅", "MAYBE": "🤔", "SKIP": "⛔"}.get(verdict.upper(), "ℹ️")

    full_lines: list[tuple[str, str, str]] = [
        (
            f"{verdict_icon} <b>Verdict: ",
            f"{verdict} (Recommendation)",
            "</b>",
        ),
        (
            "<b>Film:</b> ",
            f"{title} ({year})" if year else title,
            f" [{escape_html(confidence)}]" if confidence else "",
        ),
    ]
    if aid.get("verdict_reason"):
        full_lines.append(("<b>Why:</b> ", redact_secrets(str(aid["verdict_reason"]), max_chars=None), ""))
    if aid.get("one_line_pitch"):
        full_lines.append(("<b>Pitch:</b> ", redact_secrets(str(aid["one_line_pitch"]), max_chars=None), ""))

    rating_bits: list[str] = []
    if ratings.get("rotten_tomatoes_critics") is not None:
        rating_bits.append(f"RT {ratings['rotten_tomatoes_critics']}%")
    if ratings.get("metacritic") is not None:
        rating_bits.append(f"MC {ratings['metacritic']}")
    if ratings.get("imdb") is not None:
        rating_bits.append(f"IMDb {ratings['imdb']}")
    if ratings.get("letterboxd") is not None:
        rating_bits.append(f"LB {ratings['letterboxd']}")
    if rating_bits:
        full_lines.append(("<b>Ratings:</b> ", " | ".join(rating_bits), ""))

    if ident.get("director"):
        full_lines.append(("<b>Director:</b> ", redact_secrets(str(ident["director"]), max_chars=None), ""))
    awards = recog.get("awards_highlights")
    if isinstance(awards, list) and awards:
        full_lines.append(
            ("<b>Awards:</b> ", "; ".join(redact_secrets(str(a), max_chars=None) for a in awards), "")
        )
    if recog.get("critic_consensus"):
        full_lines.append(
            ("<b>Consensus:</b> ", redact_secrets(str(recog["critic_consensus"]), max_chars=None), "")
        )
    sim = aid.get("similar_to")
    if isinstance(sim, list) and sim:
        full_lines.append(
            ("<b>Similar:</b> ", ", ".join(redact_secrets(str(s), max_chars=None) for s in sim), "")
        )
    flags = aid.get("red_flags")
    if isinstance(flags, list) and flags:
        full_lines.append(
            ("<b>Red Flags:</b> ", "; ".join(redact_secrets(str(f), max_chars=None) for f in flags), "")
        )
    if ident.get("disambiguation_note"):
        full_lines.append(
            ("<b>Note:</b> ", redact_secrets(str(ident["disambiguation_note"]), max_chars=None), "")
        )

    # Check exact un-truncated HTML length first
    untruncated_parts = [f"{p}{escape_html(b)}{s}" for p, b, s in full_lines]
    full_html = "\n".join(untruncated_parts)
    if utf16_len(full_html) <= max_utf16_units:
        return FormattedEvaluationReply(html_text=full_html, attach_txt=False)

    # Exceeds Telegram 4,096 UTF-16 budget -> compact summary + private .txt attachment
    compact_lines: list[tuple[str, str, str]] = [
        (f"{verdict_icon} <b>Verdict: ", f"{verdict} (Recommendation)", "</b>"),
        (
            "<b>Film:</b> ",
            f"{title} ({year})" if year else title,
            f" [{escape_html(confidence)}]" if confidence else "",
        ),
    ]
    if aid.get("verdict_reason"):
        compact_lines.append(("<b>Why:</b> ", redact_secrets(str(aid["verdict_reason"]), max_chars=500), ""))
    if rating_bits:
        compact_lines.append(("<b>Ratings:</b> ", " | ".join(rating_bits), ""))
    if aid.get("one_line_pitch"):
        compact_lines.append(("<b>Pitch:</b> ", redact_secrets(str(aid["one_line_pitch"]), max_chars=400), ""))
    compact_lines.append(("<i>Full evaluation report attached as .txt below.</i>", "", ""))

    summary_html = _assemble_lines_within_budget(compact_lines, max_utf16_units)
    short_id = re.sub(r"[^a-zA-Z0-9_-]", "", release_id)[:12] or "report"
    txt_content = _build_evaluation_plain_text(evaluation)
    return FormattedEvaluationReply(
        html_text=summary_html,
        attach_txt=True,
        txt_filename=f"evaluation_{short_id}.txt",
        txt_content=txt_content,
    )
