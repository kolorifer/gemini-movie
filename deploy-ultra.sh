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

for required_file in main.py db.py schemas.py tmdb_client.py gemini_client.py requirements.txt taste_profile.txt deploy/port_check.py; do
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

port_listening() {
  local p="$1"
  python3 "$SCRIPT_DIR/deploy/port_check.py" is-listening "$p"
}

port_owned_by_service() {
  local p="$1"
  local svc_name="$2"
  if ! command -v systemctl >/dev/null 2>&1; then
    return 1
  fi
  if ! systemctl --user is-active --quiet "$svc_name" 2>/dev/null; then
    return 1
  fi
  local main_pid
  main_pid="$(systemctl --user show -p MainPID --value "$svc_name" 2>/dev/null || true)"
  if [[ ! "$main_pid" =~ ^[0-9]+$ ]] || (( main_pid <= 0 )); then
    return 1
  fi
  python3 "$SCRIPT_DIR/deploy/port_check.py" owned-by-pid "$p" "$main_pid"
}

port_claimed_by_other_unit() {
  local p="$1"
  local ignore_unit="${2:-}"
  if [[ -d "$SYSTEMD_DIR" ]]; then
    local unit_file
    for unit_file in "$SYSTEMD_DIR"/*.service "$SYSTEMD_DIR"/*.socket; do
      [[ -f "$unit_file" ]] || continue
      if [[ -n "$ignore_unit" && "$(basename "$unit_file")" == "$ignore_unit" ]]; then
        continue
      fi
      if grep -qsE "(--port[[:space:]=]+|127\.0\.0\.1:|ListenStream=.*:)${p}([^0-9]|$)" "$unit_file"; then
        return 0
      fi
    done
  fi
  return 1
}

port_in_use() {
  local p="$1"
  local ignore_unit="${2:-}"
  if port_listening "$p" || port_claimed_by_other_unit "$p" "$ignore_unit"; then
    return 0
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
  if [[ ! "$PORT" =~ ^[0-9]{4,5}$ ]] || (( PORT < 1024 || PORT > 65535 )); then
    echo "Error: PORT must be a TCP port number between 1024 and 65535." >&2
    exit 1
  fi
  CHOSEN_PORT="$PORT"
else
  if ! command -v app-ports >/dev/null 2>&1; then
    echo "Error: 'app-ports' command not found. Run this on your Ultra.cc seedbox or pass PORT=<port>." >&2
    exit 1
  fi
  APP_PORTS_RAW="$(app-ports free 2>/dev/null || true)"
  while read -r candidate; do
    if [[ -n "$candidate" ]] && ! port_in_use "$candidate" "$SERVICE_NAME"; then
      CHOSEN_PORT="$candidate"
      break
    fi
  done < <(python3 "$SCRIPT_DIR/deploy/port_check.py" parse-app-ports <<< "$APP_PORTS_RAW")
fi

if [[ -z "$CHOSEN_PORT" ]]; then
  echo "Error: Could not find an unused port from 'app-ports free'." >&2
  exit 1
fi
if port_claimed_by_other_unit "$CHOSEN_PORT" "$SERVICE_NAME"; then
  echo "Error: PORT ($CHOSEN_PORT) is already configured in another user systemd unit." >&2
  exit 1
fi
if port_listening "$CHOSEN_PORT" && ! port_owned_by_service "$CHOSEN_PORT" "$SERVICE_NAME"; then
  echo "Error: PORT ($CHOSEN_PORT) is already listening under another process on this host." >&2
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

if [[ -z "${GEMINI_MODEL:-}" && ! -L "$APP_DIR/.env" && -f "$APP_DIR/.env" ]]; then
  GEMINI_MODEL="$(read_existing_env_key "$APP_DIR/.env" "GEMINI_MODEL")"
fi
if [[ -z "${CACHE_TTL_HOURS:-}" && ! -L "$APP_DIR/.env" && -f "$APP_DIR/.env" ]]; then
  CACHE_TTL_HOURS="$(read_existing_env_key "$APP_DIR/.env" "CACHE_TTL_HOURS")"
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

refuse_symlink() {
  local target_path="$1"
  if [[ -L "$target_path" ]]; then
    echo "Error: Refusing symlinked path: $target_path" >&2
    exit 1
  fi
}

echo "[2/6] Copying microservice files into $APP_DIR..."
umask 077
for dir_path in "$HOME/apps" "$APP_DIR" "$APP_DIR/data" "$SYSTEMD_DIR"; do
  refuse_symlink "$dir_path"
done
mkdir -m 700 -p "$APP_DIR" "$APP_DIR/data" "$SYSTEMD_DIR"
chmod 700 "$APP_DIR" "$APP_DIR/data"

for f in main.py db.py schemas.py tmdb_client.py gemini_client.py requirements.txt; do
  refuse_symlink "$APP_DIR/$f"
  install -m 600 "$SCRIPT_DIR/$f" "$APP_DIR/$f"
done
if [[ ! -e "$APP_DIR/taste_profile.txt" ]]; then
  refuse_symlink "$APP_DIR/taste_profile.txt"
  install -m 600 "$SCRIPT_DIR/taste_profile.txt" "$APP_DIR/taste_profile.txt"
fi

echo "[3/6] Writing locked-down .env (chmod 600)..."
refuse_symlink "$APP_DIR/.env"
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

echo "[5/6] Configuring and restarting userland systemd unit..."
write_unit_atomic() {
  local unit_path="$1"
  refuse_symlink "$SYSTEMD_DIR"
  refuse_symlink "$unit_path"
  local tmp_unit
  tmp_unit="$(mktemp "${unit_path}.tmp.XXXXXX")"
  if ! cat > "$tmp_unit" || ! chmod 600 "$tmp_unit" || ! mv -f "$tmp_unit" "$unit_path"; then
    rm -f "$tmp_unit"
    return 1
  fi
}

write_unit_atomic "$SYSTEMD_DIR/$SERVICE_NAME" <<EOF_UNIT
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
ExecStart=%h/apps/movie-triage-api/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${CHOSEN_PORT} --no-access-log
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF_UNIT

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

wait_for_health() {
  local svc_name="$1"
  local port="$2"
  local attempt body
  for attempt in $(seq 1 15); do
    if port_owned_by_service "$port" "$svc_name"; then
      if body="$(curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:${port}/health" 2>/dev/null)"; then
        if python3 "$SCRIPT_DIR/deploy/port_check.py" verify-health --role triage --port "$port" <<< "$body"; then
          return 0
        fi
      fi
    fi
    sleep 1
  done
  echo "Error: ${svc_name} did not pass PID-verified loopback /health on 127.0.0.1:${port}. Inspect: journalctl --user -u ${svc_name} -n 50" >&2
  return 1
}

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

echo "[6/6] Running PID-verified health and live smoke tests against http://127.0.0.1:${CHOSEN_PORT}..."
echo ""
echo "--- Smoke Test 1: PID-verified GET /health ---"
wait_for_health "$SERVICE_NAME" "$CHOSEN_PORT"

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
