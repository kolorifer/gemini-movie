#!/usr/bin/env bash
set -euo pipefail

# Ultra.cc Non-Root Movie Triage FastAPI Installer & Live Smoke Test
# Deploys from this repository checkout into ~/apps/movie-triage-api with
# userland systemd bound strictly to 127.0.0.1 and owner-only permissions.

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Error: Do not run this script as root or with sudo on Ultra.cc." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/apps/movie-triage-api"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="movie-triage-api.service"

for required_file in main.py db.py schemas.py tmdb_client.py gemini_client.py requirements.txt taste_profile.txt; do
  if [[ ! -f "$SCRIPT_DIR/$required_file" ]]; then
    echo "Error: Missing $required_file next to deploy-ultra.sh. Run from the repository checkout." >&2
    exit 1
  fi
done

read_existing_env_key() {
  local env_file="$1"
  local key_name="$2"
  python3 - "$env_file" "$key_name" <<'PYEOF'
import os, stat, sys
path, key = sys.argv[1], sys.argv[2]
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
except OSError:
    sys.exit(0)
try:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or (st.st_mode & 0o077) != 0:
        sys.exit(0)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                val = v.strip().strip('"').strip("'")
                if val and "\n" not in val and "\r" not in val:
                    print(val)
                break
except OSError:
    pass
PYEOF
}

port_in_use() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    if ss -tlnH 2>/dev/null | grep -Eq "[:.]${p}[[:space:]]"; then
      return 0
    fi
  fi
  if [[ -d "$SYSTEMD_DIR" ]]; then
    if grep -RqsE "(--port[[:space:]=]+|127\.0\.0\.1:|ListenStream=.*:)${p}([^0-9]|$)" "$SYSTEMD_DIR"/*.service "$SYSTEMD_DIR"/*.socket 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

if [[ -z "${PORT:-}" && ! -L "$APP_DIR/.env" && -f "$APP_DIR/.env" ]]; then
  EXISTING_PORT="$(read_existing_env_key "$APP_DIR/.env" "PORT")"
  if [[ "$EXISTING_PORT" =~ ^[0-9]{4,5}$ ]]; then
    PORT="$EXISTING_PORT"
  fi
fi

echo "[1/6] Finding an unassigned Ultra.cc port via app-ports free..."
CHOSEN_PORT=""
if [[ -n "${PORT:-}" ]]; then
  if [[ ! "$PORT" =~ ^[0-9]{4,5}$ ]]; then
    echo "Error: PORT must be a 4-5 digit TCP port number." >&2
    exit 1
  fi
  CHOSEN_PORT="$PORT"
else
  if ! command -v app-ports >/dev/null 2>&1; then
    echo "Error: 'app-ports' command not found. Run this on your Ultra.cc seedbox or pass PORT=<port>." >&2
    exit 1
  fi
  while read -r candidate; do
    if [[ -n "$candidate" ]] && ! port_in_use "$candidate"; then
      CHOSEN_PORT="$candidate"
      break
    fi
  done < <(app-ports free | grep -Eo '[0-9]{4,5}' || true)
fi

if [[ -z "$CHOSEN_PORT" ]]; then
  echo "Error: Could not find an unused port from 'app-ports free'." >&2
  exit 1
fi
echo "Selected local port: $CHOSEN_PORT"

if [[ -z "${GEMINI_API_KEY:-}" && ! -L "$APP_DIR/.env" && -f "$APP_DIR/.env" ]]; then
  GEMINI_API_KEY="$(read_existing_env_key "$APP_DIR/.env" "GEMINI_API_KEY")"
fi
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  read -r -s -p "Enter your GEMINI_API_KEY (input hidden): " GEMINI_API_KEY
  echo ""
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "Error: GEMINI_API_KEY is required." >&2
  exit 1
fi

if [[ -z "${INTERNAL_API_SECRET:-}" && ! -L "$APP_DIR/.env" && -f "$APP_DIR/.env" ]]; then
  INTERNAL_API_SECRET="$(read_existing_env_key "$APP_DIR/.env" "INTERNAL_API_SECRET")"
fi
if [[ -z "${INTERNAL_API_SECRET:-}" ]]; then
  INTERNAL_API_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
fi

if [[ -z "${TMDB_API_KEY:-}" && ! -L "$APP_DIR/.env" && -f "$APP_DIR/.env" ]]; then
  TMDB_API_KEY="$(read_existing_env_key "$APP_DIR/.env" "TMDB_API_KEY")"
fi

echo "[2/6] Copying microservice files into $APP_DIR..."
mkdir -p "$APP_DIR" "$SYSTEMD_DIR"
mkdir -m 700 -p "$APP_DIR/data"
chmod 700 "$APP_DIR/data"

install -m 600 "$SCRIPT_DIR/main.py" "$APP_DIR/main.py"
install -m 600 "$SCRIPT_DIR/db.py" "$APP_DIR/db.py"
install -m 600 "$SCRIPT_DIR/schemas.py" "$APP_DIR/schemas.py"
install -m 600 "$SCRIPT_DIR/tmdb_client.py" "$APP_DIR/tmdb_client.py"
install -m 600 "$SCRIPT_DIR/gemini_client.py" "$APP_DIR/gemini_client.py"
install -m 600 "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
if [[ ! -f "$APP_DIR/taste_profile.txt" ]]; then
  install -m 600 "$SCRIPT_DIR/taste_profile.txt" "$APP_DIR/taste_profile.txt"
fi

echo "[3/6] Writing locked-down .env (chmod 600)..."
if [[ -L "$APP_DIR/.env" ]]; then
  echo "Error: Refusing to overwrite symlinked $APP_DIR/.env" >&2
  exit 1
fi
(
  umask 077
  ENV_TMP="$(mktemp "$APP_DIR/.env.tmp.XXXXXX")"
  cat <<EOF_ENV > "$ENV_TMP"
GEMINI_API_KEY=${GEMINI_API_KEY}
GEMINI_MODEL=${GEMINI_MODEL:-gemini-4-hydrogen}
HOST=127.0.0.1
PORT=${CHOSEN_PORT}
CACHE_TTL_HOURS=${CACHE_TTL_HOURS:-72}
TMDB_API_KEY=${TMDB_API_KEY:-}
MDBLIST_API_KEY=
INTERNAL_API_SECRET=${INTERNAL_API_SECRET}
EOF_ENV
  chmod 600 "$ENV_TMP"
  mv -f "$ENV_TMP" "$APP_DIR/.env"
)

echo "[4/6] Creating Python virtual environment and installing dependencies..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[5/6] Configuring and starting userland systemd unit..."
cat <<EOF_UNIT > "$SYSTEMD_DIR/$SERVICE_NAME"
[Unit]
Description=Local-only Movie Triage FastAPI Microservice
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/apps/movie-triage-api
EnvironmentFile=%h/apps/movie-triage-api/.env
UMask=0077
NoNewPrivileges=true
ExecStart=%h/apps/movie-triage-api/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${CHOSEN_PORT}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF_UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"
sleep 2
systemctl --user status "$SERVICE_NAME" --no-pager

smoke_post_evaluate() {
  local payload="$1"
  # Read X-Internal-Token from environment so the secret never appears in argv/ps.
  SMOKE_TOKEN="$INTERNAL_API_SECRET" SMOKE_URL="http://127.0.0.1:${CHOSEN_PORT}/evaluate" \
    python3 -c '
import json, os, sys, urllib.request
body = sys.stdin.read().encode("utf-8")
req = urllib.request.Request(
    os.environ["SMOKE_URL"],
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Internal-Token": os.environ["SMOKE_TOKEN"],
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=90) as resp:
    print(json.dumps(json.loads(resp.read().decode("utf-8")), indent=2))
' <<< "$payload"
}

echo "[6/6] Running live smoke tests against http://127.0.0.1:${CHOSEN_PORT}..."
echo ""
echo "--- Smoke Test 1: GET /health ---"
curl -sS "http://127.0.0.1:${CHOSEN_PORT}/health" | python3 -m json.tool

echo ""
echo "--- Smoke Test 2: POST /evaluate (La Chimera, expect cached: false) ---"
smoke_post_evaluate '{"raw_text": "La.Chimera.2023.1080p.BluRay.x264-GROUP"}'

echo ""
echo "--- Smoke Test 3: Repeat POST /evaluate (La Chimera, expect cached: true) ---"
smoke_post_evaluate '{"raw_text": "La.Chimera.2023.1080p.BluRay.x264-GROUP"}'

echo ""
echo "--- Smoke Test 4: Disambiguation check (foreign/remake release string) ---"
smoke_post_evaluate '{"raw_text": "Anatomie.d.une.chute.2023.FRENCH.2160p.UHD.BluRay.HEVC-GROUP"}'

echo ""
echo "Deployment complete. Movie Triage API is listening on http://127.0.0.1:${CHOSEN_PORT}"
