#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Shared helpers. Source this, don't execute it.
#   . "$(dirname "$0")/lib/common.sh"
# ---------------------------------------------------------------------------

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PROJECT_ROOT

# --- output -----------------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
  C_BLU=$'\033[34m'; C_DIM=$'\033[2m';  C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YEL=''; C_BLU=''; C_DIM=''; C_RST=''
fi

CHECKS_RUN=0
CHECKS_FAILED=0

pass() { CHECKS_RUN=$((CHECKS_RUN+1)); printf '%sPASS%s  %-28s %s\n' "$C_GRN" "$C_RST" "$1" "${2-}"; }
fail() { CHECKS_RUN=$((CHECKS_RUN+1)); CHECKS_FAILED=$((CHECKS_FAILED+1))
         printf '%sFAIL%s  %-28s %s\n' "$C_RED" "$C_RST" "$1" "${2-}"; }
warn() { printf '%sWARN%s  %-28s %s\n' "$C_YEL" "$C_RST" "$1" "${2-}"; }
skip() { printf '%sSKIP%s  %-28s %s\n' "$C_DIM" "$C_RST" "$1" "${2-}"; }
info() { printf '%s==>%s %s\n' "$C_BLU" "$C_RST" "$*"; }
die()  { printf '%serror:%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }

summary() {
  echo
  if [ "$CHECKS_FAILED" -eq 0 ]; then
    printf '%s%d/%d checks passed%s\n' "$C_GRN" "$CHECKS_RUN" "$CHECKS_RUN" "$C_RST"
    return 0
  fi
  printf '%s%d of %d checks FAILED%s\n' "$C_RED" "$CHECKS_FAILED" "$CHECKS_RUN" "$C_RST"
  return 1
}

# --- config -----------------------------------------------------------------
load_env() {
  local f="${1:-$PROJECT_ROOT/.env}"
  [ -f "$f" ] || die "missing $f — copy .env.example to .env and edit it"
  set -a
  # shellcheck disable=SC1090
  . "$f"

  # .env.tunnel is written by scripts/tunnel.sh and sourced AFTER .env so its
  # host overrides win. It also sets BENCH_TRANSPORT=ssh-tunnel, which is
  # recorded in every run's metadata and bannered by the report — a tunnelled
  # throughput number that loses its caveat is worse than no number.
  if [ -f "$PROJECT_ROOT/.env.tunnel" ]; then
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/.env.tunnel"
  else
    BENCH_TRANSPORT="${BENCH_TRANSPORT:-direct}"
  fi

  # Two different addresses for the same machine, and conflating them is what
  # breaks under tunnelling:
  #   *_HOST      the DATA plane — where the generator and exporter connect.
  #               Becomes 127.0.0.1 when a tunnel is up.
  #   *_SSH_HOST  the MANAGEMENT plane — where ssh/scp/systemctl go. Always the
  #               real address; port 22 was never blocked.
  COLLECTOR_SSH_HOST="${COLLECTOR_SSH_HOST:-$COLLECTOR_HOST}"
  COMPACTOR_SSH_HOST="${COMPACTOR_SSH_HOST:-$COMPACTOR_HOST}"
  set +a

  : "${PG_PASSWORD_FILE:?PG_PASSWORD_FILE not set in .env}"
  if [ -r "$PG_PASSWORD_FILE" ]; then
    PG_PASSWORD="$(tr -d '\r\n' < "$PG_PASSWORD_FILE")"
    export PG_PASSWORD PGPASSWORD="$PG_PASSWORD"
  else
    warn "password file" "unreadable: $PG_PASSWORD_FILE"
  fi
}

# Substitute @VAR@ placeholders from the current environment.
# render_template <src> <dst> [extra KEY=VAL ...]
render_template() {
  local src="$1" dst="$2"; shift 2
  [ -f "$src" ] || die "template not found: $src"
  local extra_keys=()
  local kv
  for kv in "$@"; do export "${kv?}"; extra_keys+=("${kv%%=*}"); done

  python3 - "$src" "$dst" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
missing = []
def sub(m):
    key = m.group(1)
    val = os.environ.get(key)
    if val is None:
        missing.append(key); return m.group(0)
    return val
out = re.sub(r'@([A-Z0-9_]+)@', sub, text)
if missing:
    sys.exit("unresolved placeholders in %s: %s" % (src, ", ".join(sorted(set(missing)))))
os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
open(dst, "w").write(out)
os.chmod(dst, 0o600)
PY
}

# --- ssh --------------------------------------------------------------------
ssh_key_opt() {
  local k="${SSH_KEY:-}"
  [ -n "$k" ] && printf -- '-i %s' "${k/#\~/$HOME}"
}

# remote <host> <command...>
remote() {
  local host="$1"; shift
  # shellcheck disable=SC2046
  ssh -n -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
      $(ssh_key_opt) "${SSH_USER:-ec2-user}@${host}" "$@"
}

# remote_stdin <host> <command>   — feeds this function's stdin to the remote
remote_stdin() {
  local host="$1"; shift
  # shellcheck disable=SC2046
  ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
      $(ssh_key_opt) "${SSH_USER:-ec2-user}@${host}" "$@"
}

# copy_to <host> <local> <remote>
copy_to() {
  local host="$1" src="$2" dst="$3"
  # shellcheck disable=SC2046
  scp -q -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes \
      $(ssh_key_opt) -r "$src" "${SSH_USER:-ec2-user}@${host}:${dst}"
}

# --- network ----------------------------------------------------------------
# tcp_open <host> <port> [timeout_s]
#
# Opens and closes the socket WITHOUT writing anything. `echo > /dev/tcp/...`
# would send a newline, and a stray byte into a gRPC port makes the server log
# "HTTP/2 client preface string missing or corrupt" — the probe would then be
# manufacturing errors in the service it is checking. Nine of those turned up
# in the collector's log from exactly this cause.
tcp_open() {
  timeout "${3:-4}" bash -c "exec 3<>/dev/tcp/$1/$2 && exec 3<&- && exec 3>&-" 2>/dev/null
}

# --- test ids ---------------------------------------------------------------
new_test_id() { printf '%s-%s' "$(date -u +%Y%m%d-%H%M%S)" "${1:-run}"; }
utc_now()     { date -u +%Y-%m-%dT%H:%M:%SZ; }

python_bin() {
  if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then echo "$PROJECT_ROOT/.venv/bin/python"
  elif command -v python3.11 >/dev/null 2>&1;   then echo python3.11
  else echo python3; fi
}
