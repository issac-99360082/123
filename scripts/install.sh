#!/usr/bin/env bash
#
# Watchpost installer: creates a virtualenv, installs dependencies, writes a
# .env with a generated SECRET_KEY, initialises the database, and runs the
# test suite so you know the install is good before you start it.
#
# Usage:  ./scripts/install.sh [--dev]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEV=0
[[ "${1:-}" == "--dev" ]] && DEV=1

info()  { printf '\033[0;34m==>\033[0m %s\n' "$1"; }
ok()    { printf '\033[0;32m  ok\033[0m %s\n' "$1"; }
warn()  { printf '\033[0;33m  !!\033[0m %s\n' "$1"; }
die()   { printf '\033[0;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

# --- 1. Python -------------------------------------------------------------
info "Checking Python"
command -v python3 >/dev/null || die "python3 not found. Install Python 3.11 or newer."
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
[[ "$PY_OK" == "1" ]] || die "Python 3.11+ required, found $(python3 -V)"
ok "$(python3 -V)"

# --- 2. Virtualenv ---------------------------------------------------------
info "Creating virtualenv at .venv"
[[ -d .venv ]] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
ok "virtualenv ready"

# --- 3. Dependencies -------------------------------------------------------
info "Installing dependencies"
if [[ $DEV -eq 1 ]]; then
  pip install --quiet -r requirements-dev.txt
else
  pip install --quiet -r requirements.txt
fi
ok "dependencies installed"

# --- 4. Configuration ------------------------------------------------------
info "Preparing .env"
if [[ -f .env ]]; then
  warn ".env already exists - leaving it untouched"
else
  cp .env.example .env
  SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')
  # BSD and GNU sed disagree about -i, so write through a temp file.
  sed "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env > .env.tmp && mv .env.tmp .env
  ok ".env created with a generated SECRET_KEY"
  warn "Stripe keys are empty: the app will run, but cannot charge yet."
  warn "See docs/SETUP.md for what to fill in."
fi

# --- 5. Database -----------------------------------------------------------
info "Initialising the database"
python -c 'from app.db import init_db; init_db(); print("  schema created or already current")'
ok "database ready"

# --- 6. Verify -------------------------------------------------------------
if [[ $DEV -eq 1 ]]; then
  info "Running the test suite"
  python -m pytest -q || die "tests failed - do not deploy this build"
  ok "tests passed"
fi

cat <<'DONE'

Watchpost is installed.

  Start it:      source .venv/bin/activate && uvicorn app.main:app --reload
  Open:          http://localhost:8000        (landing page)
                 http://localhost:8000/docs   (interactive API reference)
  Smoke test:    ./scripts/smoke_test.sh

Next steps to actually take money: docs/SETUP.md
DONE
