#!/usr/bin/env bash
# One-click launcher for Mac and Linux.
#
#   ./setup.sh            Docker if available (PostgreSQL + dashboard + worker), else local mode
#   ./setup.sh --local    force local mode: Python virtualenv + SQLite, no Docker needed
#   ./setup.sh --stop     stop everything this script started
#   ./setup.sh --status   show what is running
#
# What it does: checks Docker / Python, creates .env from .env.example, fills in
# APP_SECRET_KEY (token encryption), JWT_SECRET (logins) and POSTGRES_PASSWORD
# with secure random values, starts the platform, waits until the dashboard
# answers, and prints the address.

set -euo pipefail
cd "$(dirname "$0")"

URL="http://127.0.0.1:8000"
RUN_DIR=".run"
MODE="auto"
case "${1:-}" in
  --local) MODE="local" ;;
  --docker) MODE="docker" ;;
  --stop) MODE="stop" ;;
  --status) MODE="status" ;;
  -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
esac

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

find_python() {
  for p in python3.13 python3.12 python3 python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  return 1
}

random_key() {   # 32 random bytes, base64 (what APP_SECRET_KEY expects)
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 32
  elif [ -n "${PYTHON:-}" ]; then "$PYTHON" -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
  else head -c 32 /dev/urandom | base64; fi
}

random_hex() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  elif [ -n "${PYTHON:-}" ]; then "$PYTHON" -c 'import secrets; print(secrets.token_hex(24))'
  else head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

compose() {   # docker compose v2 or the old docker-compose
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  else docker-compose "$@"; fi
}

docker_ready() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

set_env() {   # set_env NAME VALUE: replace the line (even if commented or a placeholder) or append
  local name="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -Eq "^#? *${name}=" .env; then
    awk -v n="$name" -v v="$value" 'BEGIN{done=0} { if (!done && $0 ~ "^#? *" n "=") { print n "=" v; done=1 } else print }' .env > "$tmp"
    mv "$tmp" .env
  else
    rm -f "${tmp:?}"; printf '\n%s=%s\n' "$name" "$value" >> .env
  fi
}

env_is_placeholder() {   # true when NAME is missing, empty, commented out or still a placeholder
  local v
  v="$(grep -E "^${1}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
  [ -z "$v" ] || [[ "$v" == your_* ]] || [[ "$v" == *_here ]]
}

prepare_env() {
  bold "Configuration (.env)"
  if [ ! -f .env ]; then cp .env.example .env; ok "created .env from .env.example"; else ok ".env already exists, keeping your values"; fi
  for name in APP_SECRET_KEY JWT_SECRET POSTGRES_PASSWORD; do
    if env_is_placeholder "$name"; then
      case "$name" in
        APP_SECRET_KEY) set_env "$name" "$(random_key)" ;;
        *) set_env "$name" "$(random_hex)" ;;
      esac
      ok "generated $name"
    else
      ok "$name already set"
    fi
  done
  chmod 600 .env 2>/dev/null || true
}

wait_for_dashboard() {
  local tries="${1:-60}"
  printf '  waiting for the dashboard'
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$URL/api/health" >/dev/null 2>&1 || wget -qO- "$URL/api/health" >/dev/null 2>&1; then
      printf '\n'; return 0
    fi
    printf '.'; sleep 2
  done
  printf '\n'; return 1
}

success() {
  printf '\n'
  bold "🚀 SUCCESS! Open $URL to access your SaaS Dashboard."
  printf '   First time? Click "Create one" to make your account, then open API settings for the key wizard.\n'
  printf '   Mode: %s. Stop with: ./setup.sh --stop\n\n' "$1"
}

# ---------------------------------------------------------------------------
# Local mode (virtualenv + SQLite)
# ---------------------------------------------------------------------------

start_local() {
  bold "Local mode: Python + SQLite"
  [ -n "${PYTHON:-}" ] || die "Python 3.12+ is required for local mode. Install it from https://www.python.org/downloads/ (tick 'Add to PATH' on Windows)."
  ok "using $("$PYTHON" --version 2>&1) at $(command -v "$PYTHON")"
  if command -v ffmpeg >/dev/null 2>&1; then ok "ffmpeg found (video posts enabled)"
  else warn "ffmpeg not found: video posts will not render. Mac: brew install ffmpeg  Linux: sudo apt install ffmpeg"; fi

  if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; ok "created virtualenv .venv"; fi
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  ok "dependencies installed"
  .venv/bin/python -m config.migrate upgrade >/dev/null
  ok "database ready (data/scheduler.db)"

  mkdir -p "$RUN_DIR" data/logs
  stop_local quiet
  nohup .venv/bin/python -m uvicorn src.web.app:app --host 127.0.0.1 --port 8000 > data/logs/web.log 2>&1 &
  echo $! > "$RUN_DIR/web.pid"
  nohup .venv/bin/python -m src.main loop > data/logs/worker.log 2>&1 &
  echo $! > "$RUN_DIR/worker.pid"
  ok "started dashboard (pid $(cat "$RUN_DIR/web.pid")) and worker (pid $(cat "$RUN_DIR/worker.pid")); logs in data/logs/"
  wait_for_dashboard 30 || die "the dashboard did not start. See data/logs/web.log"
  success "local"
}

stop_local() {
  local quiet="${1:-}" pidfile
  for svc in web worker; do
    pidfile="${RUN_DIR:?}/${svc:?}.pid"
    if [ -f "$pidfile" ]; then
      if kill "$(cat "$pidfile")" 2>/dev/null && [ -z "$quiet" ]; then ok "stopped $svc"; fi
      rm -f "$pidfile"
    fi
  done
}

# ---------------------------------------------------------------------------
# Docker mode
# ---------------------------------------------------------------------------

start_docker() {
  bold "Docker mode: PostgreSQL + dashboard + worker"
  ok "docker found: $(docker --version)"
  compose up --build -d
  ok "containers started"
  wait_for_dashboard 90 || die "the dashboard did not start. Check: docker compose logs web"
  success "docker"
}

# ---------------------------------------------------------------------------

bold "Social Automation Platform: setup"
PYTHON="$(find_python || true)"
if [ -n "$PYTHON" ]; then ok "Python: $("$PYTHON" --version 2>&1)"; else warn "Python 3.12+ not found (only needed for local mode)"; fi
if docker_ready; then ok "Docker: running"; else warn "Docker: not running or not installed (install Docker Desktop from https://www.docker.com/products/docker-desktop/ for the full stack)"; fi

case "$MODE" in
  stop)
    stop_local
    if docker_ready && [ -f docker-compose.yml ]; then compose down >/dev/null 2>&1 && ok "stopped docker containers" || true; fi
    exit 0 ;;
  status)
    if docker_ready; then compose ps 2>/dev/null || true; fi
    for svc in web worker; do
      if [ -f "$RUN_DIR/$svc.pid" ] && kill -0 "$(cat "$RUN_DIR/$svc.pid")" 2>/dev/null; then ok "local $svc running (pid $(cat "$RUN_DIR/$svc.pid"))"
      else warn "local $svc not running"; fi
    done
    exit 0 ;;
  docker) prepare_env; docker_ready || die "Docker is not running."; start_docker ;;
  local)  prepare_env; start_local ;;
  auto)   prepare_env; if docker_ready; then start_docker; else warn "falling back to local mode (no Docker)"; start_local; fi ;;
esac
