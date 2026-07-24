#!/usr/bin/env bash
#
# dev.sh — Start the Kinetograph backend + desktop app together
#
# Usage:
#   ./dev.sh           Start both backend and frontend
#   ./dev.sh backend   Start only the Python backend
#   ./dev.sh desktop   Start only the Electron desktop app
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
DESKTOP_DIR="$ROOT_DIR/desktop"
VENV_DIR="$ROOT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python3"
ENV_FILE="$ROOT_DIR/.env"

# ── Colors ──────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# ── Logging ─────────────────────────────────────────────
info()  { echo -e "${BLUE}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[✓]${NC}     $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
fail()  { echo -e "${RED}[✗]${NC}     $*"; exit 1; }

# ── Cleanup on exit ─────────────────────────────────────
BACKEND_PID=""
DESKTOP_PID=""

cleanup() {
    echo ""
    info "Shutting down..."

    if [[ -n "$DESKTOP_PID" ]] && kill -0 "$DESKTOP_PID" 2>/dev/null; then
        info "Stopping desktop app (PID $DESKTOP_PID)..."
        kill "$DESKTOP_PID" 2>/dev/null || true
        wait "$DESKTOP_PID" 2>/dev/null || true
    fi

    if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        info "Stopping backend (PID $BACKEND_PID)..."
        kill "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
    fi

    # Kill anything still on port 8080
    lsof -ti:8080 2>/dev/null | xargs kill -9 2>/dev/null || true

    ok "All processes stopped."
}
trap cleanup EXIT INT TERM

# ── Preflight checks ───────────────────────────────────
preflight() {
    info "Running preflight checks..."

    # Python venv
    if [[ ! -f "$VENV_PYTHON" ]]; then
        fail "Python venv not found at $VENV_DIR. Run: python3 -m venv .venv && pip install -e backend/"
    fi
    ok "Python venv found"

    # Backend package
    if ! "$VENV_PYTHON" -c "import kinetograph" 2>/dev/null; then
        warn "Backend not installed. Installing..."
        "$VENV_PYTHON" -m pip install -e "$BACKEND_DIR" --quiet
        ok "Backend installed"
    else
        ok "Backend package importable"
    fi

    # Node modules
    if [[ ! -d "$DESKTOP_DIR/node_modules" ]]; then
        warn "Desktop dependencies not installed. Running npm install..."
        (cd "$DESKTOP_DIR" && npm install --silent)
        ok "Desktop dependencies installed"
    else
        ok "Desktop node_modules found"
    fi

    # FFmpeg
    if ! command -v ffmpeg &>/dev/null; then
        warn "FFmpeg not found on PATH — media processing will fail"
    else
        ok "FFmpeg available ($(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f3))"
    fi

    # .env file
    if [[ ! -f "$ENV_FILE" ]]; then
        warn "No .env file found. Copying .env.example..."
        cp "$ROOT_DIR/.env.example" "$ENV_FILE"
        warn "Please edit .env with your API keys before running the pipeline."
    else
        ok ".env file found"
    fi

    echo ""
}

# ── Start backend ───────────────────────────────────────
start_backend() {
    info "Starting Python backend on port 8080..."

    # Kill any existing process on 8080
    lsof -ti:8080 2>/dev/null | xargs kill -9 2>/dev/null || true
    sleep 0.5

    (
        cd "$ROOT_DIR"
        "$VENV_PYTHON" -m uvicorn kinetograph.server:app \
            --host 127.0.0.1 \
            --port 8080 \
            --reload \
            --reload-dir "$BACKEND_DIR/src" \
            2>&1 | while IFS= read -r line; do
                echo -e "${MAGENTA}[backend]${NC} $line"
            done
    ) &
    BACKEND_PID=$!

    # Wait for health check
    info "Waiting for backend to be ready..."
    for i in $(seq 1 60); do
        if curl -sf http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
            ok "Backend is ready! (took ${i}s)"
            return 0
        fi
        sleep 1
    done

    fail "Backend failed to start within 60 seconds"
}

# ── Start desktop ──────────────────────────────────────
start_desktop() {
    info "Starting Electron desktop app..."

    (
        cd "$DESKTOP_DIR"
        KINETOGRAPH_EXTERNAL_BACKEND=1 node ./node_modules/.bin/vite 2>&1 | while IFS= read -r line; do
            echo -e "${CYAN}[desktop]${NC} $line"
        done
    ) &
    DESKTOP_PID=$!

    ok "Desktop app starting (Vite + Electron)"
}

# ── Main ────────────────────────────────────────────────
echo ""
echo -e "${BOLD}🎬 Kinetograph — Development Server${NC}"
echo -e "   ${CYAN}AI-powered autonomous video editor${NC}"
echo ""

MODE="${1:-all}"

case "$MODE" in
    backend)
        preflight
        start_backend
        echo ""
        ok "Backend running at ${BOLD}http://127.0.0.1:8080${NC}"
        ok "API docs at ${BOLD}http://127.0.0.1:8080/docs${NC}"
        echo ""
        info "Press Ctrl+C to stop."
        wait
        ;;
    desktop|frontend)
        preflight
        start_desktop
        echo ""
        info "Press Ctrl+C to stop."
        wait
        ;;
    all|"")
        preflight
        start_backend
        echo ""
        start_desktop
        echo ""
        echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${GREEN}${BOLD}  🎬 Kinetograph is running!${NC}"
        echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo -e "  ${MAGENTA}Backend${NC}  → http://127.0.0.1:8080"
        echo -e "  ${MAGENTA}API docs${NC} → http://127.0.0.1:8080/docs"
        echo -e "  ${CYAN}Desktop${NC}  → Electron window (auto-launched)"
        echo ""
        info "Press Ctrl+C to stop all services."
        wait
        ;;
    *)
        echo "Usage: $0 [all|backend|desktop]"
        exit 1
        ;;
esac
