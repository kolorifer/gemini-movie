# Multi-Agent Collaboration Guide (`kolorifer/gemini-movie`)

This repository is the shared workspace for rebuilding the user's legacy `movie-intel` automation into a clean, minimal set of local-only Python FastAPI microservices on Ultra.cc (non-root userland systemd).

## What Is Already Live
- **`movie-triage-api`** (deployed at `~/apps/movie-triage-api/`, `127.0.0.1:40716`, `movie-triage-api.service`):
  - `GET /health` — status, configured Gemini model (`gemini-4-hydrogen`), bound port, active cache entries, `tmdb_enabled`.
  - `POST /evaluate` — accepts `raw_text`, optional `hint_title`, `hint_year`, `extra_context`, `force_refresh`, and optional `torrent_info` (`indexer`, `category`, `imdb_id`, `tmdb_id`, `runtime_minutes`, `audio_languages`, `resolution`, `source`, `dynamic_range`, `media_processing`, `group`, `size_gb`, `freeleech`, `description`, `nfo_text`).
    - Cross-checks tracker-supplied IDs against release title/year and MediaInfo duration before trusting them (rejects mistaken tracker links and flags conflicts to Gemini).
    - Parses release quality (`Remux-2160p` vs `Remux-1080p`, Dolby Vision Profile 7 FEL/MEL vs Profile 8 vs HDR/SDR, BDInfo 1080p EL bitrate heuristic, favorite group `CiNEPHiLES`) and scopes SQLite cache by `(movie, quality_key)`.
  - `POST /feedback` — records user `watch` or `skip` decisions (case-insensitive, deduplicated per movie) to guide future evaluations.
  - `GET /feedback` / `GET /recent` — inspect stored feedback and recent evaluations.

## Agent Roles & Handover Protocol
- **ChatGPT / Codex (Legacy Historian & Critical Reviewer):**
  - Owns institutional memory of the legacy `movie-intel` (`radarr-festival-list`) setup.
  - Authors `docs/LEGACY_HANDOVER.md` (or answers Issue #1) listing active flows to keep, experimental features to drop, non-negotiable production invariants, and external config/API contracts.
  - Reviews Antigravity's architecture and implementation PRs (`@codex review`) specifically for real-world edge-case regressions from `movie-intel` history.
- **Antigravity (Architect, Builder, Tester, Seedbox Operator):**
  - Designs the clean microservice architecture (`docs/ARCHITECTURE.md`) from Codex's handover brief without carrying over legacy bloat.
  - Implements services cleanly on feature branches via Pull Requests, writes unit/replay tests, deploys to Ultra.cc non-root systemd user units on `app-ports free` ports, and runs shadow/replay verification before cutover.

## Target Environment Constraints (Ultra.cc Seedbox)
- Shared **non-root** Linux environment: **NEVER** use `sudo`, root paths, or Docker.
- Bind internal APIs strictly to `127.0.0.1:<PORT>` using unassigned ports from `app-ports free` (only expose specific authenticated callback routes via existing user Nginx if required for Telegram actions).
- Keep the microservice footprint lean (**2–3 cohesive FastAPI services**, not over-fragmented) to respect seedbox RAM and port budgets.
- Every `.env` / cookie / secret file must stay outside git and be locked down with `chmod 600`.
- Never log, commit, or transmit tracker passkeys, cookies, `download_url` tokens, or API keys to external LLMs or GitHub.

## Code Review Rules
When reviewing pull requests in this repository (`@codex review`), treat the following as **P0/P1 findings**:
1. **Security & Secret Hygiene (P0):** Any leak of tracker cookies, torrent passkeys/`download_url` tokens, Radarr/Telegram/TMDB/Gemini API keys into logs, LLM prompts, SQLite, or git; missing `chmod 600` or unsafe cookie-file handling (symlink/world-readable reads).
2. **Legacy Production Invariants (P1):** Any regression of hard-won `movie-intel` invariants documented in Issue #1 / `docs/LEGACY_HANDOVER.md` (e.g., tracker listing verification, mistaken tracker IDs, duplicate webhook burst races, non-movie filtering, Radarr `watched` + unmonitored protection against re-notification/re-download, Telegram button auth/idempotency).
3. **Seedbox Constraints (P1):** Any use of `sudo`, Docker, hardcoded ports, public `0.0.0.0` binding without auth, or blocking synchronous I/O stalling FastAPI event loops.
4. **Scope Creep (P1):** Re-introducing legacy experimental code that was explicitly placed on the Drop List.
