#!/usr/bin/env bash
# Coordinator startup script.
#
# Usage:
#   scripts/run_coordinator.sh              # foreground
#   scripts/run_coordinator.sh --background # background with health check
#   scripts/run_coordinator.sh --port 8080  # custom port
#   scripts/run_coordinator.sh stop         # stop background process
#   scripts/run_coordinator.sh status       # show status

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Activate venv ───────────────────────────────────────────────────────────
VENV=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv"; do
  if [ -d "$candidate" ]; then
    VENV="$candidate"
    break
  fi
done

if [ -n "$VENV" ]; then
  # shellcheck disable=SC1091
  if [ -f "$VENV/bin/activate" ]; then
    source "$VENV/bin/activate"
  elif [ -f "$VENV/Scripts/activate" ]; then
    source "$VENV/Scripts/activate"
  fi
fi

# ── Defaults ────────────────────────────────────────────────────────────────
PORT=9100
MODE="foreground"
PID_FILE="${HERMES_HOME:-$HOME/.hermes}/coordinator.pid"
LOG_DIR="${HERMES_HOME:-$HOME/.hermes}/logs"
LOG_FILE="$LOG_DIR/coordinator.log"

# ── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    --background|-b)
      MODE="background"
      shift
      ;;
    stop)
      MODE="stop"
      shift
      ;;
    status)
      MODE="status"
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 [--port PORT] [--background] [stop|status]"
      exit 1
      ;;
  esac
done

export COORDINATOR_PORT="$PORT"

# ── Functions ───────────────────────────────────────────────────────────────

start_foreground() {
  echo "Starting coordinator on port $PORT (foreground)..."
  exec python -m uvicorn coordinator.server:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info
}

start_background() {
  if is_running; then
    echo "Coordinator already running (PID: $(cat "$PID_FILE"))"
    return 0
  fi

  mkdir -p "$LOG_DIR"
  mkdir -p "$(dirname "$PID_FILE")"

  echo "Starting coordinator on port $PORT (background)..."

  nohup python -m uvicorn coordinator.server:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info \
    >> "$LOG_FILE" 2>&1 &

  local pid=$!
  echo "$pid" > "$PID_FILE"

  echo "PID: $pid"
  echo "Log:  $LOG_FILE"
  echo -n "Waiting for health check..."

  for i in $(seq 1 30); do
    if health_check; then
      echo " OK"
      return 0
    fi
    echo -n "."
    sleep 1
  done

  echo " TIMEOUT"
  echo "Coordinator may have failed to start. Check $LOG_FILE"
  return 1
}

stop_coordinator() {
  if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found. Coordinator may not be running."
    return 1
  fi

  local pid
  pid=$(cat "$PID_FILE")

  # Windows: use python psutil or tasklist; POSIX: kill -0
  pid_alive() {
    if [ "$(uname -s 2>/dev/null)" = "Linux" ] || [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
      kill -0 "$1" 2>/dev/null
    else
      python -c "import psutil; exit(0 if psutil.pid_exists($1) else 1)" 2>/dev/null
    fi
  }

  if pid_alive "$pid"; then
    echo "Stopping coordinator (PID: $pid)..."
    kill "$pid"
    # Wait up to 10s for graceful shutdown
    for i in $(seq 1 10); do
      if ! pid_alive "$pid"; then
        echo "Coordinator stopped."
        rm -f "$PID_FILE"
        return 0
      fi
      sleep 1
    done
    echo "Force killing..."
    if [ "$(uname -s 2>/dev/null)" = "Linux" ] || [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
      kill -9 "$pid" 2>/dev/null || true
    else
      taskkill //PID "$pid" //F 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "Coordinator killed."
  else
    echo "Process $pid not running. Cleaning up PID file."
    rm -f "$PID_FILE"
  fi
}

show_status() {
  if is_running; then
    local pid
    pid=$(cat "$PID_FILE" 2>/dev/null || echo "unknown")
    echo "Coordinator is running (PID: $pid, port: $PORT)"
    if health_check; then
      echo "Health: OK"
    else
      echo "Health: FAIL"
    fi
  else
    echo "Coordinator is not running."
  fi
}

is_running() {
  if [ -f "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE")
    # Windows: use python psutil or tasklist; POSIX: kill -0
    if [ "$(uname -s 2>/dev/null)" = "Linux" ] || [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
      if kill -0 "$pid" 2>/dev/null; then
        return 0
      fi
    else
      if python -c "import psutil; exit(0 if psutil.pid_exists($pid) else 1)" 2>/dev/null; then
        return 0
      fi
    fi
  fi
  return 1
}

health_check() {
  if command -v curl >/dev/null 2>&1; then
    curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1
    return $?
  elif command -v python >/dev/null 2>&1; then
    python -c "
import urllib.request
try:
    urllib.request.urlopen('http://localhost:${PORT}/health')
    exit(0)
except:
    exit(1)
" 2>/dev/null
    return $?
  fi
  return 1
}

# ── Main ────────────────────────────────────────────────────────────────────

cd "$REPO_ROOT"

case "$MODE" in
  foreground)
    start_foreground
    ;;
  background)
    start_background
    ;;
  stop)
    stop_coordinator
    ;;
  status)
    show_status
    ;;
esac
