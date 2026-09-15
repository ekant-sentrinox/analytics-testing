#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# DISK PRESSURE — fill SERVER 2's root filesystem and see how ingestion fails.
#
#   tests/failure/disk_pressure.sh [--target-percent 95] [--rps 2000] [--duration 300]
#
# The collector stages Arrow IPC and Parquet on local disk before the file goes
# to S3, so a full root filesystem breaks ingestion even though the lake lives
# in object storage. The question is whether it fails CLEANLY — a typed error
# and a refused RPC — or writes a truncated Parquet file and registers it.
#
# SAFETY. The ballast is one file in /var/tmp/bench-ballast, removed on exit
# including on Ctrl-C or a crash (systemd-tmpfiles would also clear it). It
# never fills past --target-percent, and it never touches
# /opt/analytics-bench or the source checkout. Even so: run this on a machine
# you are willing to disturb, not during a soak.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

TARGET=95; RPS=2000; DURATION=300
while [ $# -gt 0 ]; do
  case "$1" in
    --target-percent) TARGET="$2"; shift ;;
    --rps) RPS="$2"; shift ;;
    --duration) DURATION="$2"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done
[ "$TARGET" -ge 99 ] && die "refusing a target of ${TARGET}% — that can wedge the host, not just the service"

load_env
PY="$(python_bin)"
HOST="$COLLECTOR_SSH_HOST"
BALLAST=/var/tmp/bench-ballast
TEST_ID="$(new_test_id disk-pressure)"
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
mkdir -p "$RUN_DIR"

cleanup() {
  echo
  info "removing ballast"
  remote "$HOST" "rm -f $BALLAST; sync; df -h / | tail -1" 2>/dev/null \
    && pass "cleanup" "ballast removed" \
    || fail "cleanup" "REMOVE $BALLAST ON $HOST BY HAND"
  remote "$HOST" "sudo systemctl restart bench-collector" 2>/dev/null || true
  for _ in $(seq 1 30); do
    remote "$HOST" "curl -sf --max-time 3 http://127.0.0.1:$COLLECTOR_HEALTH_PORT/health" \
      >/dev/null 2>&1 && { pass "cleanup" "collector healthy again"; return; }
    sleep 2
  done
  warn "cleanup" "collector did not come back cleanly — check $HOST"
}
trap cleanup EXIT INT TERM

before="$(remote "$HOST" "df -h / | tail -1" 2>/dev/null)"
info "before: $before"

info "filling / on $HOST to ${TARGET}%"
remote "$HOST" "
  set -e
  total=\$(df --output=size -k / | tail -1 | tr -d ' ')
  used=\$(df --output=used -k / | tail -1 | tr -d ' ')
  want=\$(( total * $TARGET / 100 ))
  fill=\$(( want - used ))
  if [ \$fill -le 0 ]; then echo 'already at or above target'; exit 0; fi
  # fallocate is instant and does not thrash the disk on the way in.
  fallocate -l \${fill}K $BALLAST 2>/dev/null || dd if=/dev/zero of=$BALLAST bs=1M count=\$(( fill / 1024 )) status=none
  df -h / | tail -1" || die "could not create ballast"

after_fill="$(remote "$HOST" "df -h / | tail -1" 2>/dev/null)"
pass "ballast" "$after_fill"

before_restarts="$(remote "$HOST" "systemctl show bench-collector -p NRestarts --value" 2>/dev/null || echo 0)"

echo
info "driving $RPS rec/s for ${DURATION}s against a nearly-full disk"
SETTLE_SECONDS=90 "$PROJECT_ROOT/scripts/run-test.sh" light \
  --test-id "$TEST_ID" --rps "$RPS" --duration "$DURATION" --no-preflight || true

after_restarts="$(remote "$HOST" "systemctl show bench-collector -p NRestarts --value" 2>/dev/null || echo 0)"

echo
info "result"
echo
printf '  disk before          %s\n' "$before"
printf '  disk during          %s\n' "$after_fill"
printf '  collector restarts   %s -> %s\n' "$before_restarts" "$after_restarts"

remote "$HOST" "grep -icE 'no space|ENOSPC|IOException|disk' /opt/analytics-bench/collector/logs/collector.log 2>/dev/null || echo 0" \
  | { read -r n; printf '  disk errors in log   %s\n' "$n"; }

if [ -f "$RUN_DIR/manifest.json" ]; then
  "$PY" - "$RUN_DIR" <<'PY'
import json, os, sys
run = sys.argv[1]
m = json.load(open(os.path.join(run, "manifest.json")))
print()
print(f"  offered              {m['total_offered']:,}")
print(f"  accepted             {m['total_accepted']:,}")
print(f"  failed RPCs          {m['requests_failed']:,}  {m.get('errors_by_code') or ''}")
try:
    c = json.load(open(os.path.join(run, "correctness.json")))
    d = (c.get("checks", {}).get("row_count") or {}).get("delta")
    print(f"  landed               {c.get('landed', 0):,}")
    print()
    if d == 0:
        print("  CLEAN FAILURE        every acked record landed. The collector refused work")
        print("                       rather than acking something it could not persist.")
    elif isinstance(d, int) and d < 0:
        print(f"  DIRTY FAILURE        {-d:,} acked records are missing. Under disk exhaustion")
        print("                       the collector acked writes it did not complete.")
    print(f"  CORRECTNESS          {c.get('verdict')}")
except Exception:
    print("  CORRECTNESS          NOT EVALUATED")
PY
fi

echo
info "artifacts: $RUN_DIR"
