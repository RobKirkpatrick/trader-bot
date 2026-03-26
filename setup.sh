#!/usr/bin/env bash
# setup.sh — Sentinel trader-bot first-time setup
# Works on macOS and Linux. Safe to re-run.
set -e

PYTHON_MIN=3.10
VENV_DIR=".venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
die()  { echo -e "${RED}✗${NC} $1"; exit 1; }

echo ""; echo "  Sentinel — trader-bot setup"; echo "  ─────────────────────────────"; echo ""

OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM="macos" ;;
  Linux)  PLATFORM="linux" ;;
  *)      die "Unsupported OS: $OS (Windows users: run in WSL2)" ;;
esac
ok "Platform: $OS"

find_python() {
  for cmd in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" &>/dev/null; then
      ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
      major=$(echo "$ver" | cut -d. -f1)
      minor=$(echo "$ver" | cut -d. -f2)
      if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
        echo "$cmd"; return 0
      fi
    fi
  done
  return 1
}

if PYTHON=$(find_python); then
  ok "Python $($PYTHON --version 2>&1 | awk '{print $2}') found at $(which $PYTHON)"
else
  warn "Python $PYTHON_MIN+ not found — attempting install..."
  if [ "$PLATFORM" = "macos" ]; then
    if ! command -v brew &>/dev/null; then
      echo "  Homebrew required: https://brew.sh — install it then re-run ./setup.sh"
      die "Homebrew not found"
    fi
    brew install python@3.12
    PYTHON=$(find_python) || die "Python install failed"
  elif [ "$PLATFORM" = "linux" ]; then
    if command -v apt-get &>/dev/null; then
      sudo apt-get update -q && sudo apt-get install -y python3.12 python3.12-venv python3-pip
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y python3.12
    else
      die "No supported package manager — install Python $PYTHON_MIN+ manually and re-run"
    fi
    PYTHON=$(find_python) || die "Python install failed"
  fi
  ok "Python installed: $($PYTHON --version 2>&1)"
fi

if [ -d "$VENV_DIR" ]; then
  ok "Virtual environment already exists ($VENV_DIR)"
else
  echo "  Creating virtual environment..."
  $PYTHON -m venv "$VENV_DIR"
  ok "Virtual environment created ($VENV_DIR)"
fi

echo "  Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt
ok "Dependencies installed"

if [ -f ".env" ]; then
  ok ".env already exists — skipping copy"
else
  cp .env.example .env
  ok ".env created from .env.example"
  warn "Fill in your API keys in .env before running the bot"
fi

echo ""; echo "  Setup complete!"; echo ""
echo "  Next steps:"
echo "  1. Fill in your API keys in .env"
echo "     (open docs/first-time-setup.html in a browser for a guided wizard)"
echo ""
echo "  2. Test locally (no real orders placed):"
echo "     source .venv/bin/activate && python3 test_scan.py"
echo ""
echo "  3. Deploy to AWS Lambda:"
echo "     ./deploy.sh"
echo ""
