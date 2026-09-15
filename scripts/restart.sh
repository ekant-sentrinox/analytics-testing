#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Restart pipeline components.
#
#   scripts/restart.sh [collector|compactor|all] [--kill]
#
# Default is a graceful SIGTERM restart. --kill sends SIGKILL instead, which is
# TEST 20's collector-kill fault: it skips the drain, so whatever was in the
# in-memory queue is lost. Use it deliberately, and expect the correctness
# validator to report the loss.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

TARGET="${1:-all}"
KILL=0
[ "${2:-}" = "--kill" ] && KILL=1
case "$TARGET" in collector|compactor|all) : ;; *) die "usage: restart.sh [collector|compactor|all] [--kill]" ;; esac

load_env

restart_one() {                 # restart_one <role> <host> <unit> <port>
  local role="$1" host="$2" unit="$3" port="$4"
  local before after
  before="$(remote "$host" "systemctl show $unit -p NRestarts --value" 2>/dev/null || echo '?')"

  if [ "$KILL" -eq 1 ]; then
    warn "$role" "SIGKILL — no graceful drain; in-flight batches will be lost"
    remote "$host" "sudo systemctl kill -s SIGKILL $unit" 2>/dev/null || \
      fail "$role" "kill failed"
    # systemd Restart=on-failure brings it back by itself; do not race it.
    sleep 3
    remote "$host" "sudo systemctl start $unit" 2>/dev/null || true
  else
    info "$role: SIGTERM restart (collector drains for shutdown_grace_period_ms first)"
    remote "$host" "sudo systemctl restart $unit" 2>/dev/null || fail "$role" "restart failed"
  fi

  local body=""
  for _ in $(seq 1 45); do
    body="$(remote "$host" "curl -sf --max-time 3 http://127.0.0.1:$port/health" 2>/dev/null || true)"
    [ -n "$body" ] && break
    sleep 2
  done
  after="$(remote "$host" "systemctl show $unit -p NRestarts --value" 2>/dev/null || echo '?')"
  if [ -n "$body" ]; then
    pass "$role" "back up (NRestarts $before -> $after)"
  else
    fail "$role" "did not come back within 90s"
    remote "$host" "sudo systemctl status $unit --no-pager -l | head -20" || true
  fi
}

[ "$TARGET" = "collector" ] || [ "$TARGET" = "all" ] && \
  restart_one collector "$COLLECTOR_SSH_HOST" bench-collector "$COLLECTOR_HEALTH_PORT"
[ "$TARGET" = "compactor" ] || [ "$TARGET" = "all" ] && \
  restart_one compactor "$COMPACTOR_SSH_HOST" bench-compactor "$COMPACTOR_HEALTH_PORT"

summary
