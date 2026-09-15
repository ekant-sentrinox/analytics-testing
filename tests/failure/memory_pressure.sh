#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# MEMORY PRESSURE — squeeze the collector until the kernel kills it.
#
#   tests/failure/memory_pressure.sh [--limit 1G] [--rps 3000] [--duration 420]
#
# Applies a systemd MemoryMax to bench-collector, drives load, and records what
# happens. Restores the original limit afterwards, including on Ctrl-C.
#
# What is being tested is not "does it die" — with a small enough limit it
# always dies. It is whether it dies CLEANLY: does the collector ack records it
# then loses? An OOM kill after an ack is data loss and a protocol bug; an OOM
# kill that only drops un-acked work is a capacity problem.
#
# SERVER 2 has 7.6 GiB and no swap, so memory pressure is always an OOM kill
# rather than a slowdown.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

LIMIT=1G; RPS=3000; DURATION=420
while [ $# -gt 0 ]; do
  case "$1" in
    --limit) LIMIT="$2"; shift ;;
    --rps) RPS="$2"; shift ;;
    --duration) DURATION="$2"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_env
PY="$(python_bin)"
HOST="$COLLECTOR_SSH_HOST"
UNIT=bench-collector
TEST_ID="$(new_test_id memory-pressure)"
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
mkdir -p "$RUN_DIR"

ORIGINAL="$(remote "$HOST" "systemctl show $UNIT -p MemoryMax --value" 2>/dev/null || echo infinity)"
info "current MemoryMax: $ORIGINAL"

restore() {
  echo
  info "restoring MemoryMax"
  remote "$HOST" "
    sudo mkdir -p /etc/systemd/system/$UNIT.d
    sudo rm -f /etc/systemd/system/$UNIT.d/99-memory-pressure.conf
    sudo systemctl daemon-reload
    sudo systemctl restart $UNIT" 2>/dev/null || warn "restore" "check MemoryMax on $HOST by hand"
  for _ in $(seq 1 30); do
    remote "$HOST" "curl -sf --max-time 3 http://127.0.0.1:$COLLECTOR_HEALTH_PORT/health" \
      >/dev/null 2>&1 && { pass "restore" "collector back with the original limit"; return; }
    sleep 2
  done
  fail "restore" "collector did not come back — check $HOST"
}
trap restore EXIT INT TERM

info "applying MemoryMax=$LIMIT to $UNIT on $HOST"
remote "$HOST" "
  sudo mkdir -p /etc/systemd/system/$UNIT.d
  printf '[Service]\nMemoryMax=$LIMIT\nMemorySwapMax=0\n' | \
    sudo tee /etc/systemd/system/$UNIT.d/99-memory-pressure.conf >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart $UNIT" || die "could not apply the limit"

for _ in $(seq 1 45); do
  remote "$HOST" "curl -sf --max-time 3 http://127.0.0.1:$COLLECTOR_HEALTH_PORT/health" \
    >/dev/null 2>&1 && break
  sleep 2
done
applied="$(remote "$HOST" "systemctl show $UNIT -p MemoryMax --value" 2>/dev/null)"
pass "limit applied" "MemoryMax=$applied"

before_restarts="$(remote "$HOST" "systemctl show $UNIT -p NRestarts --value" 2>/dev/null || echo 0)"
before_oom="$(remote "$HOST" "sudo dmesg -T 2>/dev/null | grep -ci 'out of memory: killed' || echo 0" 2>/dev/null || echo 0)"

echo
info "driving $RPS rec/s for ${DURATION}s against the constrained collector"
SETTLE_SECONDS=120 "$PROJECT_ROOT/scripts/run-test.sh" light \
  --test-id "$TEST_ID" --rps "$RPS" --duration "$DURATION" --no-preflight || true

after_restarts="$(remote "$HOST" "systemctl show $UNIT -p NRestarts --value" 2>/dev/null || echo 0)"
after_oom="$(remote "$HOST" "sudo dmesg -T 2>/dev/null | grep -ci 'out of memory: killed' || echo 0" 2>/dev/null || echo 0)"
peak_mem="$(remote "$HOST" "systemctl show $UNIT -p MemoryPeak --value" 2>/dev/null || echo '')"

echo
info "result"
echo
printf '  memory limit         %s\n' "$applied"
printf '  peak memory          %s\n' "${peak_mem:-not reported by this systemd}"
printf '  restarts             %s -> %s  (delta %s)\n' "$before_restarts" "$after_restarts" \
  "$((after_restarts - before_restarts))"
printf '  OOM kills in dmesg   %s -> %s  (delta %s)\n' "$before_oom" "$after_oom" \
  "$((after_oom - before_oom))"

if [ -f "$RUN_DIR/correctness.json" ]; then
  "$PY" - "$RUN_DIR/correctness.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
rc = c.get("checks", {}).get("row_count", {})
d = rc.get("delta")
print()
print(f"  offered              {c.get('offered', 0):,}")
print(f"  accepted (acked)     {c.get('accepted', 0):,}")
print(f"  landed               {c.get('landed', 0):,}")
print()
if d == 0:
    print("  DATA INTEGRITY       intact — every acked record survived the memory pressure.")
    print("                       The collector degraded by refusing or failing work, which")
    print("                       is the correct behaviour.")
elif isinstance(d, int) and d < 0:
    print(f"  DATA LOSS            {-d:,} acked records did not land.")
    print("                       This is a protocol bug, not a capacity limit: the collector")
    print("                       must not complete the export RPC before the batch is durable.")
print(f"  CORRECTNESS          {c.get('verdict')}")
PY
else
  warn "correctness" "not evaluated"
fi

echo
info "artifacts: $RUN_DIR"
