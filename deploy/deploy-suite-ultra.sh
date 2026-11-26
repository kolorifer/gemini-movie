#!/usr/bin/env bash
set -euo pipefail

# Ultra.cc Non-Root Installer for movie-intake-api & movie-actions-api
# - Strictly non-root (NO sudo, NO Docker, NO root paths).
# - NEVER modifies or disables legacy movie-intel.service.
# - Picks two distinct unassigned ports via `app-ports free` verified against
#   `ss -tlnH` and `~/.config/systemd/user/*.{service,socket}`.
# - Slices secrets strictly by least privilege into chmod 600 per-service .env files.
# - Runs uvicorn strictly on 127.0.0.1 with `--no-access-log`, `UMask=0077`, `NoNewPrivileges=true`.
#
# Usage:
#   bash deploy/deploy-suite-ultra.sh [--shadow] [--dry-run]
# Optional env overrides:
#   PORT_INTAKE=<port> PORT_ACTIONS=<port> PORT_TRIAGE=<port>

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Error: Do not run this script as root or with sudo on Ultra.cc." >&2
  exit 1
fi

SHADOW_MODE=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --shadow) SHADOW_MODE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Usage: bash deploy/deploy-suite-ultra.sh [--shadow] [--dry-run]"
      exit 0
      ;;
    *)
      echo "Error: Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMD_DIR="$HOME/.config/systemd/user"

if [[ "$SHADOW_MODE" -eq 1 ]]; then
  INTAKE_APP_DIR="$HOME/apps/movie-intake-api-shadow"
  ACTIONS_APP_DIR="$HOME/apps/movie-actions-api-shadow"
  INTAKE_SERVICE="movie-intake-api-shadow.service"
  ACTIONS_SERVICE="movie-actions-api-shadow.service"
  STATE_DIR="$HOME/.local/state/movie-intel-shadow"
else
  INTAKE_APP_DIR="$HOME/apps/movie-intake-api"
  ACTIONS_APP_DIR="$HOME/apps/movie-actions-api"
  INTAKE_SERVICE="movie-intake-api.service"
  ACTIONS_SERVICE="movie-actions-api.service"
  STATE_DIR="$HOME/.local/state/movie-intel"
fi

TRIAGE_ENV="$HOME/apps/movie-triage-api/.env"
LEGACY_ENV="$HOME/.config/movie-intel/environment"

for req in \
  "services/__init__.py" \
  "services/movie_intake_api/app.py" \
  "services/movie_intake_api/requirements.txt" \
  "services/movie_actions_api/app.py" \
  "services/movie_actions_api/requirements.txt" \
  "deploy/env_slicer.py" \
  "deploy/port_check.py"; do
  if [[ ! -f "$REPO_ROOT/$req" ]]; then
    echo "Error: Missing required repository file: $req" >&2
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
  python3 "$REPO_ROOT/deploy/port_check.py" is-listening "$p"
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
  python3 "$REPO_ROOT/deploy/port_check.py" owned-by-pid "$p" "$main_pid"
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

PORT_TRIAGE="${PORT_TRIAGE:-}"
if [[ -z "$PORT_TRIAGE" && ! -L "$TRIAGE_ENV" && -f "$TRIAGE_ENV" ]]; then
  PORT_TRIAGE="$(read_existing_env_key "$TRIAGE_ENV" "PORT")"
fi
PORT_TRIAGE="${PORT_TRIAGE:-40716}"

if [[ -z "${PORT_INTAKE:-}" && ! -L "$INTAKE_APP_DIR/.env" && -f "$INTAKE_APP_DIR/.env" ]]; then
  EXISTING_INTAKE_PORT="$(read_existing_env_key "$INTAKE_APP_DIR/.env" "MOVIE_INTEL_PORT")"
  if [[ "$EXISTING_INTAKE_PORT" =~ ^[0-9]{4,5}$ ]]; then
    PORT_INTAKE="$EXISTING_INTAKE_PORT"
  fi
fi

if [[ -z "${PORT_ACTIONS:-}" && ! -L "$ACTIONS_APP_DIR/.env" && -f "$ACTIONS_APP_DIR/.env" ]]; then
  EXISTING_ACTIONS_PORT="$(read_existing_env_key "$ACTIONS_APP_DIR/.env" "ACTIONS_PORT")"
  if [[ "$EXISTING_ACTIONS_PORT" =~ ^[0-9]{4,5}$ ]]; then
    PORT_ACTIONS="$EXISTING_ACTIONS_PORT"
  fi
fi

echo "[1/6] Selecting two distinct unassigned loopback ports..."
FREE_PORTS=()
if [[ -z "${PORT_INTAKE:-}" || -z "${PORT_ACTIONS:-}" ]]; then
  if command -v app-ports >/dev/null 2>&1; then
    APP_PORTS_RAW="$(app-ports free 2>/dev/null || true)"
    while read -r candidate; do
      if [[ -n "$candidate" ]] && [[ "$candidate" != "$PORT_TRIAGE" ]] && ! port_in_use "$candidate"; then
        FREE_PORTS+=("$candidate")
      fi
    done < <(python3 "$REPO_ROOT/deploy/port_check.py" parse-app-ports <<< "$APP_PORTS_RAW")
  elif [[ "$DRY_RUN" -eq 1 ]]; then
    FREE_PORTS=("40720" "40721")
  else
    echo "Error: 'app-ports' not found. Run on Ultra.cc or pass PORT_INTAKE=<p1> PORT_ACTIONS=<p2>." >&2
    exit 1
  fi
fi

if [[ -z "${PORT_INTAKE:-}" ]]; then
  for p in "${FREE_PORTS[@]}"; do
    if [[ "$p" != "${PORT_ACTIONS:-}" && "$p" != "$PORT_TRIAGE" ]]; then
      PORT_INTAKE="$p"
      break
    fi
  done
fi
if [[ -z "${PORT_ACTIONS:-}" ]]; then
  for p in "${FREE_PORTS[@]}"; do
    if [[ "$p" != "${PORT_INTAKE:-}" && "$p" != "$PORT_TRIAGE" ]]; then
      PORT_ACTIONS="$p"
      break
    fi
  done
fi

for name_val in "PORT_INTAKE:${PORT_INTAKE:-}" "PORT_ACTIONS:${PORT_ACTIONS:-}" "PORT_TRIAGE:${PORT_TRIAGE:-}"; do
  p_name="${name_val%%:*}"
  p_val="${name_val#*:}"
  if [[ ! "$p_val" =~ ^[0-9]{4,5}$ ]] || (( p_val < 1024 || p_val > 65535 )); then
    echo "Error: Invalid $p_name='$p_val' (must be 1024-65535)." >&2
    exit 1
  fi
done
if [[ "$PORT_INTAKE" == "$PORT_ACTIONS" || "$PORT_INTAKE" == "$PORT_TRIAGE" || "$PORT_ACTIONS" == "$PORT_TRIAGE" ]]; then
  echo "Error: PORT_INTAKE ($PORT_INTAKE), PORT_ACTIONS ($PORT_ACTIONS), and PORT_TRIAGE ($PORT_TRIAGE) must be distinct." >&2
  exit 1
fi
if port_claimed_by_other_unit "$PORT_INTAKE" "$INTAKE_SERVICE"; then
  echo "Error: PORT_INTAKE ($PORT_INTAKE) is already configured in another user systemd unit." >&2
  exit 1
fi
if port_claimed_by_other_unit "$PORT_ACTIONS" "$ACTIONS_SERVICE"; then
  echo "Error: PORT_ACTIONS ($PORT_ACTIONS) is already configured in another user systemd unit." >&2
  exit 1
fi
if port_listening "$PORT_INTAKE" && ! port_owned_by_service "$PORT_INTAKE" "$INTAKE_SERVICE"; then
  echo "Error: PORT_INTAKE ($PORT_INTAKE) is already listening under another process on this host." >&2
  exit 1
fi
if port_listening "$PORT_ACTIONS" && ! port_owned_by_service "$PORT_ACTIONS" "$ACTIONS_SERVICE"; then
  echo "Error: PORT_ACTIONS ($PORT_ACTIONS) is already listening under another process on this host." >&2
  exit 1
fi
echo "Selected loopback ports: intake=127.0.0.1:${PORT_INTAKE}, actions=127.0.0.1:${PORT_ACTIONS}, triage=127.0.0.1:${PORT_TRIAGE}"

SLICER_ARGS=(
  --home "$HOME"
  --port-intake "$PORT_INTAKE"
  --port-actions "$PORT_ACTIONS"
  --port-triage "$PORT_TRIAGE"
  --intake-env-out "$INTAKE_APP_DIR/.env"
  --actions-env-out "$ACTIONS_APP_DIR/.env"
)
if [[ "$SHADOW_MODE" -eq 0 && ! -L "$TRIAGE_ENV" && -f "$TRIAGE_ENV" ]]; then
  SLICER_ARGS+=(--triage-env "$TRIAGE_ENV")
fi
if [[ "$SHADOW_MODE" -eq 0 && ! -L "$LEGACY_ENV" && -f "$LEGACY_ENV" ]]; then
  SLICER_ARGS+=(--legacy-env "$LEGACY_ENV")
fi
if [[ "$SHADOW_MODE" -eq 1 ]]; then
  SLICER_ARGS+=(--shadow)
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DRY-RUN] Validating least-privilege env slicing (zero files written, zero systemd changes)..."
  python3 "$REPO_ROOT/deploy/env_slicer.py" "${SLICER_ARGS[@]}" --dry-run
  echo "[DRY-RUN] Complete."
  exit 0
fi

refuse_symlink() {
  local target_path="$1"
  if [[ -L "$target_path" ]]; then
    echo "Error: Refusing symlinked path: $target_path" >&2
    exit 1
  fi
}

safe_install_file() {
  local src="$1"
  local dst="$2"
  refuse_symlink "$dst"
  install -m 600 "$src" "$dst"
}

echo "[2/6] Creating owner-private directories (0700) and copying service packages..."
umask 077
for dir_path in \
  "$HOME/apps" \
  "$INTAKE_APP_DIR" \
  "$INTAKE_APP_DIR/services" \
  "$INTAKE_APP_DIR/services/movie_intake_api" \
  "$ACTIONS_APP_DIR" \
  "$ACTIONS_APP_DIR/services" \
  "$ACTIONS_APP_DIR/services/movie_actions_api" \
  "$STATE_DIR" \
  "$STATE_DIR/webhook-archive" \
  "$SYSTEMD_DIR"; do
  refuse_symlink "$dir_path"
done

mkdir -m 700 -p "$INTAKE_APP_DIR/services/movie_intake_api" "$ACTIONS_APP_DIR/services/movie_actions_api"
mkdir -m 700 -p "$STATE_DIR" "$STATE_DIR/webhook-archive" "$SYSTEMD_DIR"
chmod 700 \
  "$STATE_DIR" \
  "$STATE_DIR/webhook-archive" \
  "$INTAKE_APP_DIR" \
  "$INTAKE_APP_DIR/services" \
  "$INTAKE_APP_DIR/services/movie_intake_api" \
  "$ACTIONS_APP_DIR" \
  "$ACTIONS_APP_DIR/services" \
  "$ACTIONS_APP_DIR/services/movie_actions_api"

safe_install_file "$REPO_ROOT/services/__init__.py" "$INTAKE_APP_DIR/services/__init__.py"
for f in "$REPO_ROOT"/services/movie_intake_api/*.py; do
  safe_install_file "$f" "$INTAKE_APP_DIR/services/movie_intake_api/$(basename "$f")"
done
safe_install_file "$REPO_ROOT/services/movie_intake_api/requirements.txt" "$INTAKE_APP_DIR/requirements.txt"

safe_install_file "$REPO_ROOT/services/__init__.py" "$ACTIONS_APP_DIR/services/__init__.py"
for f in "$REPO_ROOT"/services/movie_actions_api/*.py; do
  safe_install_file "$f" "$ACTIONS_APP_DIR/services/movie_actions_api/$(basename "$f")"
done
safe_install_file "$REPO_ROOT/services/movie_actions_api/requirements.txt" "$ACTIONS_APP_DIR/requirements.txt"

echo "[3/6] Writing least-privilege locked-down .env files (chmod 600)..."
python3 "$REPO_ROOT/deploy/env_slicer.py" "${SLICER_ARGS[@]}"

echo "[4/6] Creating isolated Python virtual environments..."
python3 -m venv "$INTAKE_APP_DIR/.venv"
"$INTAKE_APP_DIR/.venv/bin/pip" install --upgrade pip
"$INTAKE_APP_DIR/.venv/bin/pip" install -r "$INTAKE_APP_DIR/requirements.txt"

python3 -m venv "$ACTIONS_APP_DIR/.venv"
"$ACTIONS_APP_DIR/.venv/bin/pip" install --upgrade pip
"$ACTIONS_APP_DIR/.venv/bin/pip" install -r "$ACTIONS_APP_DIR/requirements.txt"

echo "[5/6] Writing hardened non-root userland systemd units..."
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

write_unit_atomic "$SYSTEMD_DIR/$INTAKE_SERVICE" <<EOF_INTAKE
[Unit]
Description=Local-only Movie Intake FastAPI Microservice
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INTAKE_APP_DIR}
EnvironmentFile=${INTAKE_APP_DIR}/.env
UMask=0077
NoNewPrivileges=true
ExecStart=${INTAKE_APP_DIR}/.venv/bin/uvicorn services.movie_intake_api.app:build_default_app --factory --host 127.0.0.1 --port ${PORT_INTAKE} --no-access-log
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF_INTAKE

write_unit_atomic "$SYSTEMD_DIR/$ACTIONS_SERVICE" <<EOF_ACTIONS
[Unit]
Description=Local-only Movie Actions FastAPI Microservice
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ACTIONS_APP_DIR}
EnvironmentFile=${ACTIONS_APP_DIR}/.env
UMask=0077
NoNewPrivileges=true
ExecStart=${ACTIONS_APP_DIR}/.venv/bin/uvicorn services.movie_actions_api.app:build_default_app --factory --host 127.0.0.1 --port ${PORT_ACTIONS} --no-access-log
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF_ACTIONS

systemctl --user daemon-reload
systemctl --user enable "$ACTIONS_SERVICE" "$INTAKE_SERVICE"
systemctl --user restart "$ACTIONS_SERVICE" "$INTAKE_SERVICE"

wait_for_health() {
  local svc_name="$1"
  local port="$2"
  local role="$3"
  local attempt body
  for attempt in $(seq 1 15); do
    if port_owned_by_service "$port" "$svc_name"; then
      if body="$(curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:${port}/health" 2>/dev/null)"; then
        if python3 "$REPO_ROOT/deploy/port_check.py" verify-health --role "$role" --port "$port" --shadow "$SHADOW_MODE" <<< "$body"; then
          return 0
        fi
      fi
    fi
    sleep 1
  done
  echo "Error: ${svc_name} did not pass PID-verified loopback /health on 127.0.0.1:${port} (role=${role}, shadow=${SHADOW_MODE}). Inspect: journalctl --user -u ${svc_name} -n 50" >&2
  return 1
}

echo "[6/6] Running PID-verified loopback health checks..."
wait_for_health "$ACTIONS_SERVICE" "$PORT_ACTIONS" "actions"
wait_for_health "$INTAKE_SERVICE" "$PORT_INTAKE" "intake"

echo ""
echo "Deployment complete (legacy movie-intel.service left untouched)."
echo "  movie-intake-api : http://127.0.0.1:${PORT_INTAKE}"
echo "  movie-actions-api: http://127.0.0.1:${PORT_ACTIONS}"
