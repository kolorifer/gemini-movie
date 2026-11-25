# Movie Triage API (`movie-triage-api`)

Local-only Python FastAPI microservice for fast movie release triage using Google Search-grounded Gemini (`google-genai`), optional TMDB deterministic ID/runtime/original-title verification (`TMDB_API_KEY`), Pydantic v2 structured output, and SQLite caching/feedback persistence.

## Endpoints

- `GET /health` — Unauthenticated read-only status, configured model, bound port, active cache entry count, `tmdb_enabled`, and `auth_enabled`.
- `POST /evaluate` — Identifies a movie from a release string (with optional hints, `torrent_info` tracker metadata/description/MediaInfo NFO, and TMDB deterministic verification when `TMDB_API_KEY` is configured). Tracker-supplied IDs are cross-checked against release title/year and MediaInfo runtime so mistaken tracker links are rejected and flagged for Gemini disambiguation. Results are cached in SQLite (`data/triage.db`, `0600`) for `CACHE_TTL_HOURS` (default `72`), scoped by `(movie, quality_key, evidence_key)` so changed tracker/MediaInfo evidence never reuses a stale cached verdict.
- `POST /feedback` — Records user `watch` or `skip` decisions (case-insensitive, deduplicated per movie) and optional notes to guide future evaluations.
- `GET /feedback` — Lists recent stored feedback entries fed into Gemini prompts.
- `GET /recent` — Lists recent evaluations.

### Shared-Loopback Authentication
When `INTERNAL_API_SECRET` is set in `.env` (`chmod 600`), all non-health endpoints (`POST /evaluate`, `POST /feedback`, `GET /feedback`, `GET /recent`) require the `X-Internal-Token` request header (validated via constant-time `hmac.compare_digest`; missing/invalid tokens receive `401 Unauthorized`).

## Ultra.cc Non-Root Deployment

Run `deploy-ultra.sh` from a checkout of this repository on your Ultra.cc seedbox (never as root or with `sudo`):

```bash
bash deploy-ultra.sh
```

Or specify an explicit unassigned port from `app-ports free`:

```bash
PORT=<UNASSIGNED_PORT> bash deploy-ultra.sh
```

The installer:
1. Picks an unassigned port from `app-ports free` verified free against `ss -tlnH` and `~/.config/systemd/user/`, binding strictly to `127.0.0.1:<PORT>`.
2. Copies the microservice files into `~/apps/movie-triage-api/` with a `.venv` and private SQLite directory (`chmod 700 ~/apps/movie-triage-api/data`).
3. Prompts for `GEMINI_API_KEY` (hidden input), generates or preserves `INTERNAL_API_SECRET`, and writes `.env` atomically with `chmod 600`.
4. Installs and enables `~/.config/systemd/user/movie-triage-api.service` with `UMask=0077` and `NoNewPrivileges=true`.
5. Runs live smoke tests (passing `X-Internal-Token` via environment variable rather than command-line args).
