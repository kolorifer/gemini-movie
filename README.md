# `gemini-movie` — Clean 3-Service Movie Intel Suite (Ultra.cc)

Lean, local-only, non-root Python FastAPI microservice suite replacing the legacy `movie-intel` monolith on Ultra.cc (userland `systemd`, strict `127.0.0.1` loopback binding, `0700`/`0600` owner-only private state, zero Docker, zero `sudo`).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/LEGACY_HANDOVER.md](docs/LEGACY_HANDOVER.md) for the complete architectural contract and production invariants.

## Service Architecture (3 Cohesive Services)

| Service | Code Location | Deploy Path & Unit | Responsibility |
|---|---|---|---|
| **1. `movie-triage-api`** | Repo root (`main.py`, `db.py`, `schemas.py`, `tmdb_client.py`, `gemini_client.py`) | `~/apps/movie-triage-api/`<br>`movie-triage-api.service` (`127.0.0.1:40716`) | Search-grounded Gemini evaluation (`/evaluate`), TMDB pre/post verification, `(movie, quality_key, evidence_key)` SQLite caching, user feedback store (`/feedback`), and `X-Internal-Token` loopback auth. |
| **2. `movie-intake-api`** | `services/movie_intake_api/` | `~/apps/movie-intake-api/`<br>`movie-intake-api.service` | Autobrr webhook receiver (`/webhook/{secret}`), archive-before-filter `0700`/`0600` raw storage with SHA-256 verification, SQLite WAL burst deduplication & work queue, serialized TorrentLeech listing verification, independent TMDB/MediaInfo identity verification gate, pre-delivery suppression, and on-demand ephemeral push-payload resolution (`/internal/releases/{release_id}/push-payload`). |
| **3. `movie-actions-api`** | `services/movie_actions_api/` | `~/apps/movie-actions-api/`<br>`movie-actions-api.service` | Sole holder of Radarr & Telegram credentials: read-only Radarr pre-delivery status (`/internal/radarr/status`), defense-in-depth Telegram card/reply delivery with UTF-16 budgeting, poster→text fallback & `.txt` attachments, expiring SHA-256-hashed action tokens (`/action/{token}`), write-ahead Radarr journaling, `Add & Grab`, `Alternative Grab` ("Grab Anyway"), `Watched – Good / Bad`, `Standalone Skip`, startup orphan recovery, reconciliation, and feedback failure isolation. |

## Verification & Automated Shadow Suite

Run the full unit, concurrency, integration, deployment, and shadow test suite locally:

```bash
.venv/bin/python -m unittest -v \
  test_triage_api.py \
  services/movie_intake_api/tests/test_intake_api.py \
  services/movie_actions_api/tests/test_actions_api.py \
  tests/test_deploy_and_shadow_suite.py
```

Run the standalone **5-Case Pre-Cutover Shadow Verification Harness** (zero external mutations, isolated temporary state, real `movie-intake-api` ↔ `movie-actions-api` ↔ `movie-triage-api` wiring):

```bash
.venv/bin/python scripts/run_shadow_verification.py
```

## Ultra.cc Non-Root Deployment & Cutover Runbook

> **Never run as root or with `sudo`.** Existing `movie-intel.service` is left untouched during deployment and shadow verification so rollback is instant.

### 1. Deploy / Upgrade `movie-triage-api`
```bash
bash deploy-ultra.sh
# Or specify an explicit port from `app-ports free`:
PORT=40716 bash deploy-ultra.sh
```

### 2. Optional Isolated Shadow Deployment on Ultra.cc (Zero Live Credentials)
```bash
bash deploy/deploy-suite-ultra.sh --shadow
```
This deploys `movie-intake-api-shadow.service` and `movie-actions-api-shadow.service` into isolated `~/.local/state/movie-intel-shadow/` (`0700`) with `SHADOW_REPLAY_ENABLED=true` and zero live Radarr/Telegram/TL credentials.

### 3. Production Deployment of `movie-intake-api` & `movie-actions-api`
```bash
bash deploy/deploy-suite-ultra.sh
```
The installer:
1. Selects two distinct unassigned loopback ports from `app-ports free` cross-checked against `ss -tlnH` and `~/.config/systemd/user/*.{service,socket}`.
2. Slices credentials strictly by least privilege from `~/.config/movie-intel/environment` (if present and `chmod 600`) and reuses `INTERNAL_API_SECRET` from `~/apps/movie-triage-api/.env` into locked-down `chmod 600` `.env` files (`deploy/env_slicer.py`).
3. Creates `0700` state/archive directories, isolated Python `.venv`s, and hardened userland systemd units (`UMask=0077`, `NoNewPrivileges=true`, `--host 127.0.0.1`, `--no-access-log`).
4. Runs loopback `GET /health` smoke checks against both services without stopping legacy `movie-intel.service`.

### 4. User Nginx Proxy Cutover & Rollback
1. Adapt [deploy/nginx-movie-intel.conf.example](deploy/nginx-movie-intel.conf.example) with the selected `<PORT_INTAKE>` and `<PORT_ACTIONS>`, preserving prefix stripping, `access_log off;` and `error_log /dev/null emerg;` on both `/webhook/` and `/action/` (so neither access logs nor upstream failure error logs ever record secrets/tokens), and `client_max_body_size 1m;` on `/webhook/`. **Never** expose `/internal/`, `/replay`, `/health`, `/evaluate`, or `/feedback`.
2. Reload user Nginx and verify autobrr webhook delivery and Telegram confirmation pages.
3. Only after live verification succeeds, stop/disable legacy `movie-intel.service`. To roll back at any point, revert the two Nginx `proxy_pass` ports to legacy `127.0.0.1:40714` and reload Nginx.
