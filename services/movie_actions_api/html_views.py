from __future__ import annotations

import html
from typing import Any, Optional

from .security import redact_secrets

SECURITY_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store, no-cache, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _page_shell(*, title: str, badge: str, body_html: str) -> str:
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{_e(title)}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:680px;margin:24px auto;padding:0 16px;"
        "background:#111418;color:#eef2f7;line-height:1.5}"
        ".card{background:#1b2028;border:1px solid #2d3644;border-radius:10px;padding:20px}"
        ".badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;"
        "background:#273449;color:#9cc4ff;margin-bottom:10px}"
        ".warn{background:#3b2616;border:1px solid #8a5324;padding:12px;border-radius:8px;margin:12px 0}"
        ".ok{background:#163322;border:1px solid #2b7a4b;padding:12px;border-radius:8px;margin:12px 0}"
        ".err{background:#3a191c;border:1px solid #9e3138;padding:12px;border-radius:8px;margin:12px 0}"
        "label{display:block;margin:12px 0 4px;font-weight:600}"
        "select,button{width:100%;padding:10px 12px;border-radius:8px;font-size:15px}"
        "select{background:#111418;color:#eef2f7;border:1px solid #3c4758}"
        "button{margin-top:16px;background:#2563eb;color:#fff;border:none;font-weight:600;cursor:pointer}"
        "button.secondary{background:#475569}"
        "code{background:#111418;padding:2px 6px;border-radius:4px;word-break:break-all}"
        "</style></head><body><div class=\"card\">"
        f"<div class=\"badge\">{_e(badge)}</div>"
        f"<h1>{_e(title)}</h1>"
        f"{body_html}"
        "</div></body></html>"
    )


def render_simple_status_page(
    *,
    title: str,
    badge: str,
    message: str,
    tone: str = "ok",
    details: Optional[list[tuple[str, str]]] = None,
    post_button_label: Optional[str] = None,
    hidden_fields: Optional[dict[str, str]] = None,
) -> str:
    cls = tone if tone in ("ok", "warn", "err") else "ok"
    parts = [f"<div class=\"{cls}\">{_e(redact_secrets(message, max_chars=500))}</div>"]
    if details:
        parts.append("<dl>")
        for k, v in details:
            parts.append(f"<dt><strong>{_e(k)}</strong></dt><dd><code>{_e(redact_secrets(v, max_chars=400))}</code></dd>")
        parts.append("</dl>")
    if post_button_label:
        fields = dict(hidden_fields or {})
        fields.setdefault("confirm", "1")
        hidden_html = "".join(
            f"<input type=\"hidden\" name=\"{_e(k)}\" value=\"{_e(v)}\">"
            for k, v in fields.items()
        )
        parts.append(
            "<form method=\"POST\">"
            f"{hidden_html}"
            f"<button type=\"submit\">{_e(post_button_label)}</button>"
            "</form>"
        )
    return _page_shell(title=title, badge=badge, body_html="".join(parts))


def render_confirmation_page(
    *,
    action: dict[str, Any],
    radarr_summary: str,
    root_folders: list[dict[str, Any]],
    quality_profiles: list[dict[str, Any]],
    needs_add_options: bool,
    warning_note: Optional[str] = None,
) -> str:
    action_type = str(action["action_type"])
    title = str(action["bound_title"])
    year = action.get("bound_year")
    film_label = f"{title} ({year})" if year else title
    release_name = str(action["bound_release_name"])

    labels = {
        "add_grab": ("Confirm Add & Grab", "Confirm & Push Release to Radarr"),
        "alternative_grab": ("Confirm Alternative Grab (Grab Anyway)", "Confirm Alternative Grab"),
        "watched_good": ("Confirm Watched – Good", "Mark Watched (Unmonitored) & Save Good Rating"),
        "watched_bad": ("Confirm Watched – Bad", "Mark Watched (Unmonitored) & Save Bad Rating"),
        "watched_legacy_unrated": ("Confirm Watched (Unrated)", "Mark Watched (Unmonitored)"),
        "skip": ("Confirm Skip (Feedback Only)", "Record Skip Decision (No Radarr Changes)"),
    }
    heading, btn_label = labels.get(action_type, ("Confirm Action", "Confirm"))

    parts: list[str] = [
        f"<p><strong>Movie:</strong> {_e(film_label)}</p>",
        f"<p><strong>Release:</strong> <code>{_e(release_name)}</code></p>",
        f"<p><strong>Current Radarr Status:</strong> {_e(radarr_summary)}</p>",
    ]
    if action_type == "alternative_grab":
        parts.append(
            "<div class=\"warn\">"
            f"Quality-only exception bound to movie ID <code>{_e(action.get('bound_existing_movie_id'))}</code>, "
            f"existing file ID <code>{_e(action.get('bound_existing_file_id'))}</code>, "
            f"rejection class <code>{_e(action.get('bound_rejection_class'))}</code>."
            "</div>"
        )
    if warning_note:
        parts.append(f"<div class=\"warn\">{_e(redact_secrets(warning_note, max_chars=400))}</div>")

    form_fields: list[str] = ["<input type=\"hidden\" name=\"confirm\" value=\"1\">"]
    if needs_add_options:
        if root_folders:
            opts = "".join(
                f"<option value=\"{_e(rf['path'])}\">{_e(rf['path'])}</option>"
                for rf in root_folders
                if isinstance(rf.get("path"), str) and rf["path"].strip()
            )
            form_fields.append(
                "<label for=\"root_folder_path\">Radarr Root Folder</label>"
                f"<select id=\"root_folder_path\" name=\"root_folder_path\" required>{opts}</select>"
            )
        if quality_profiles:
            opts = "".join(
                f"<option value=\"{_e(qp['id'])}\">{_e(qp['name'])}</option>"
                for qp in quality_profiles
                if isinstance(qp.get("id"), int) and not isinstance(qp.get("id"), bool)
            )
            form_fields.append(
                "<label for=\"quality_profile_id\">Radarr Quality Profile</label>"
                f"<select id=\"quality_profile_id\" name=\"quality_profile_id\" required>{opts}</select>"
            )

    parts.append(
        "<form method=\"POST\">"
        f"{''.join(form_fields)}"
        f"<button type=\"submit\">{_e(btn_label)}</button>"
        "</form>"
    )
    return _page_shell(title=heading, badge=action_type, body_html="".join(parts))
