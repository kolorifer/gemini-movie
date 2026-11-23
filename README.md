# Movie Triage API (`movie-triage-api`)

Local-only Python FastAPI microservice for fast movie release triage using Google Search-grounded Gemini (`google-genai`), optional TMDB deterministic ID/runtime/original-title verification (`TMDB_API_KEY`), Pydantic v2 structured output, and SQLite caching/feedback persistence.

## Endpoints

- `GET /health` — Service status, configured model, bound port, active cache entry count, and whether TMDB lookup is enabled.
- `POST /evaluate` — Identifies a movie from a release string (with optional hints/context and TMDB metadata verification when `TMDB_API_KEY` is configured), fetches grounded ratings and critical reception, and returns a `WATCH` / `MAYBE` / `SKIP` verdict. Results are cached in SQLite (`data/triage.db`) for `CACHE_TTL_HOURS` (default `72`).
- `POST /feedback` — Records user `watch` or `skip` decisions (case-insensitive, deduplicated per movie) and optional notes to guide future evaluations.
- `GET /feedback` — Lists recent stored feedback entries fed into Gemini prompts.
- `GET /recent` — Lists recent evaluations.

## Ultra.cc Non-Root Deployment

Run `deploy-ultra.sh` on your Ultra.cc seedbox (never as root or with `sudo`):

```bash
bash deploy-ultra.sh
```

Or specify an explicit unassigned port from `app-ports free`:

```bash
PORT=<UNASSIGNED_PORT> bash deploy-ultra.sh
```

The installer:
1. Picks an unassigned port and binds strictly to `127.0.0.1:<PORT>`.
2. Writes the app into `~/apps/movie-triage-api/` with a `.venv` and SQLite at `~/apps/movie-triage-api/data/triage.db`.
3. Prompts for `GEMINI_API_KEY` (hidden input) and writes `.env` with `chmod 600`.
4. Installs and enables `~/.config/systemd/user/movie-triage-api.service`.
5. Runs live `curl` smoke tests.
