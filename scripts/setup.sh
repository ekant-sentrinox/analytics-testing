#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-time setup on SERVER 1.
#
#   scripts/setup.sh
#
# Creates the venv, installs dependencies, creates .env from the example if it
# is missing, and generates the shared JWT secret. Idempotent.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

cd "$PROJECT_ROOT"

# --- python ------------------------------------------------------------------
PYBASE=""
for candidate in python3.11 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver="$("$candidate" -c 'import sys; print("%d%02d" % sys.version_info[:2])')"
    # config.py and friends use PEP 604 unions (`int | None`), 3.10+.
    if [ "$ver" -ge 310 ]; then PYBASE="$candidate"; break; fi
  fi
done
[ -n "$PYBASE" ] || die "no python >= 3.10 found. On Amazon Linux 2023: sudo dnf install -y python3.11 python3.11-pip"
info "using $PYBASE ($("$PYBASE" --version))"

if [ ! -d .venv ]; then
  info "creating .venv"
  "$PYBASE" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip wheel
info "installing generator/requirements.txt (this takes a minute on 2 vCPUs)"
./.venv/bin/python -m pip install --quiet -r generator/requirements.txt
pass "python deps" "$(./.venv/bin/python -c 'import grpc, jwt, yaml, duckdb; print("grpc %s, duckdb %s" % (grpc.__version__, duckdb.__version__))')"

# --- directories --------------------------------------------------------------
mkdir -p results logs run
touch results/.gitkeep logs/.gitkeep
pass "directories" "results/ logs/ run/"

# --- .env ---------------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 0600 .env
  pass ".env" "created from .env.example — review it"
else
  pass ".env" "already present, left alone"
fi

# --- shared secret --------------------------------------------------------------
if ! grep -q '^OTEL_JWT_SECRET_B64=.\+' .env; then
  info "generating the shared HMAC secret"
  "$PROJECT_ROOT/scripts/gen-secret.sh"
else
  pass "jwt secret" "already set in .env"
fi

# --- git ------------------------------------------------------------------------
if [ ! -d .git ]; then
  git init -q
  git add -A >/dev/null 2>&1 || true
  pass "git" "initialised (nothing committed, nothing pushed)"
else
  pass "git" "repository already present — remote and branch untouched"
fi

echo
info "next:  scripts/preflight.sh"
summary
