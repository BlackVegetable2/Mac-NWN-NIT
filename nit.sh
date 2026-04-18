#!/usr/bin/env bash
# =============================================================================
# nit.sh — NIT macOS Launcher
# Ensures Python 3 and tkinter are available, then launches nit_macos.py
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIT_PY="$SCRIPT_DIR/nit_macos.py"

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}[NIT]${NC} $1"; }
ok()    { echo -e "${GREEN}[NIT]${NC} $1"; }
warn()  { echo -e "${YELLOW}[NIT]${NC} $1"; }
die()   { echo -e "${RED}[NIT] ERROR:${NC} $1"; exit 1; }

# ── Check for nit_macos.py ────────────────────────────────────────────────────
[[ -f "$NIT_PY" ]] || die "nit_macos.py not found in $SCRIPT_DIR"

# ── Find Python 3 ─────────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3 python3.12 python3.11 python3.10 python3.9 python3.8; do
  if command -v "$candidate" &>/dev/null; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done

if [[ -z "$PYTHON" ]]; then
  warn "Python 3 not found."
  if command -v brew &>/dev/null; then
    info "Installing Python 3 via Homebrew..."
    brew install python3
    PYTHON="$(command -v python3)"
    ok "Python 3 installed: $($PYTHON --version)"
  else
    die "Python 3 is required. Install Homebrew first:\n  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  fi
else
  ok "Using $PYTHON ($($PYTHON --version))"
fi

# ── Check tkinter ─────────────────────────────────────────────────────────────
if ! "$PYTHON" -c "import tkinter" 2>/dev/null; then
  warn "tkinter not available for $PYTHON"
  if command -v brew &>/dev/null; then
    info "Installing python-tk via Homebrew..."
    PYVER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    brew install "python-tk@$PYVER" 2>/dev/null || brew install python-tk
    ok "python-tk installed"
  else
    die "tkinter is required for the GUI.\nInstall Homebrew then run: brew install python-tk"
  fi
else
  ok "tkinter available"
fi

# ── Launch NIT ────────────────────────────────────────────────────────────────
info "Launching NIT..."
exec "$PYTHON" "$NIT_PY" "$@"