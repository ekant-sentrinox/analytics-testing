#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# RECOVERY — disturb one component under load and measure what it costs.
#
#   tests/recovery/run.sh <fault> [--rps 2000] [--duration 900] [--at 300]
#
#   collector-sigterm   graceful stop and restart. The collector should enter
#                       MAINTENANCE, drain, and lose nothing.
#   collector-sigkill   hard kill mid-flush. Whatever was in the in-memory
#                       queue is gone; the point is to measure HOW MUCH, and to
#                       confirm nothing is silently duplicated on restart.
#   compactor-sigkill   kill mid-merge. Expect orphaned Parquet files on S3
#                       that housekeeping later reclaims — and no catalog
#                       corruption, because the merge commits atomically.
#
# Measured: failure time, detection time, recovery time, records lost,
# duplicates, backlog recovery, throughput after recovery.
#
# SAFETY. This only ever touches bench-collector / bench-compactor on the two
# configured hosts, via systemctl. It does not stop RDS, does not touch S3, and
# does not restart anything it did not start.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

[ $# -ge 1 ] || { sed -n '2,24p' "$0"; exit 1; }
FAULT="$1"; shift
RPS=2000; DURATION=900; AT=300
while [ $# -gt 0 ]; do
  case "$1" in
    --rps) RPS="$2"; shift ;;
    --duration) DURATION="$2"; shift ;;
    --at) AT="$2"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

case "$FAULT" in
  collector-sigterm) ROLE=collector; HOSTVAR=COLLECTOR_SSH_HOST; UNIT=bench-collector; SIG=SIGTERM ;;
  collector-sigkill) ROLE=collector; HOSTVAR=COLLECTOR_SSH_HOST; UNIT=bench-collector; SIG=SIGKILL ;;
  compactor-sigkill) ROLE=compactor; HOSTVAR=COMPACTOR_SSH_HOST; UNIT=bench-compactor; SIG=SIGKILL ;;
  *) die "unknown fault '$FAULT' — see --help" ;;
esac

load_env
PY="$(python_bin)"
HOST="${!HOSTVAR}"
PORT_VAR="$( [ "$ROLE" = collector ] && echo COLLECTOR_HEALTH_PORT || echo COMPACTOR_HEALTH_PORT )"
PORT="${!PORT_VAR}"
TEST_ID="$(new_test_id "recovery-$FAULT")"
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
mkdir -p "$RUN_DIR"

info "RECOVERY — $FAULT"
info "$RPS rec/s for ${DURATION}s; fault injected at t+${AT}s on $HOST"
warn "scope" "only systemctl $SIG on $UNIT. RDS and S3 are not touched."
echo

tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 5 || die "collector gRPC unreachable"

"$PROJECT_ROOT/scripts/start.sh" --agents --exporter >/dev/null 2>&1 || true
"$PROJECT_ROOT/scripts/snapshot-state.sh" > "$RUN_DIR/state-before.json" 2>/dev/null || true

# --- fault injector, running alongside the generator -------------------------
(
  sleep "$AT"
  FAULT_AT="$(date +%s.%N)"
  echo "{\"event\":\"fault_injected\",\"signal\":\"$SIG\",\"unit\":\"$UNIT\",\"host\":\"$HOST\",\"at\":$FAULT_AT}" \
    >> "$RUN_DIR/fault.jsonl"

  ssh -n -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new $(ssh_key_opt) \
    "${SSH_USER}@${HOST}" "sudo systemctl kill -s $SIG $UNIT" 2>/dev/null || true

  # Detection: first moment /health stops answering.
  DETECTED=""
  for _ in $(seq 1 300); do
    if ! ssh -n -o ConnectTimeout=3 -o BatchMode=yes $(ssh_key_opt) "${SSH_USER}@${HOST}" \
         "curl -sf --max-time 2 http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      DETECTED="$(date +%s.%N)"
      break
    fi
    sleep 0.5
  done
  echo "{\"event\":\"down_detected\",\"at\":${DETECTED:-null}}" >> "$RUN_DIR/fault.jsonl"

  # systemd Restart=on-failure should bring it back on its own; only intervene
  # if it does not, and record which happened.
  RECOVERED=""
  SELF_HEALED=true
  for i in $(seq 1 240); do
    if ssh -n -o ConnectTimeout=3 -o BatchMode=yes $(ssh_key_opt) "${SSH_USER}@${HOST}" \
         "curl -sf --max-time 2 http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      RECOVERED="$(date +%s.%N)"
      break
    fi
    if [ "$i" -eq 60 ]; then
      SELF_HEALED=false
      ssh -n -o ConnectTimeout=10 -o BatchMode=yes $(ssh_key_opt) "${SSH_USER}@${HOST}" \
        "sudo systemctl start $UNIT" 2>/dev/null || true
    fi
    sleep 0.5
  done
  echo "{\"event\":\"recovered\",\"at\":${RECOVERED:-null},\"self_healed\":$SELF_HEALED}" \
    >> "$RUN_DIR/fault.jsonl"
) &
INJECTOR=$!
trap 'kill $INJECTOR 2>/dev/null || true' EXIT

# --- the load ---------------------------------------------------------------
# retry-mode none: a fault must show up as failed RPCs, not be smoothed over.
SETTLE_SECONDS=180 "$PROJECT_ROOT/scripts/run-test.sh" recovery \
  --test-id "$TEST_ID" --rps "$RPS" --duration "$DURATION" --retry-mode none || true

wait $INJECTOR 2>/dev/null || true

# --- analysis ---------------------------------------------------------------
echo
info "recovery analysis"
"$PY" - "$RUN_DIR" "$FAULT" <<'PY'
import json, os, sys

run, fault = sys.argv[1], sys.argv[2]

def load(p, d=None):
    try:
        return json.load(open(os.path.join(run, p)))
    except Exception:
        return d

def jsonl(p):
    rows = []
    try:
        for line in open(os.path.join(run, p)):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except OSError:
        pass
    return rows

events = {e["event"]: e for e in jsonl("fault.jsonl")}
man = load("manifest.json", {}) or {}
corr = load("correctness.json", {}) or {}
samples = jsonl("generator.jsonl")

inj = (events.get("fault_injected") or {}).get("at")
det = (events.get("down_detected") or {}).get("at")
rec = (events.get("recovered") or {}).get("at")
healed = (events.get("recovered") or {}).get("self_healed")

print()
print(f"  fault                {fault}")
print(f"  signal               {(events.get('fault_injected') or {}).get('signal', '?')}")
if inj and det:
    print(f"  detection time       {det - inj:.1f} s  (health endpoint stopped answering)")
else:
    print("  detection time       NOT MEASURED")
if inj and rec:
    print(f"  total outage         {rec - inj:.1f} s")
    print(f"  restarted by         {'systemd (Restart=on-failure)' if healed else 'the test, after systemd did not'}")
else:
    print("  total outage         NOT MEASURED — the service may never have come back")

# Throughput before and after, from the generator's own samples.
if samples and inj:
    before = [s["accepted_rps"] for s in samples
              if isinstance(s.get("ts"), (int, float)) and s["ts"] < inj and s.get("accepted_rps")]
    after = [s["accepted_rps"] for s in samples
             if isinstance(s.get("ts"), (int, float)) and rec and s["ts"] > rec + 30
             and s.get("accepted_rps")]
    if before:
        print(f"  accepted before      {sum(before) / len(before):,.0f} rec/s")
    if after:
        avg_b = sum(before) / len(before) if before else 0
        avg_a = sum(after) / len(after)
        print(f"  accepted after       {avg_a:,.0f} rec/s"
              + (f"  ({(avg_a - avg_b) / avg_b:+.1%} vs before)" if avg_b else ""))
    else:
        print("  accepted after       NOT MEASURED — the run ended before recovery settled")

errs = man.get("errors_by_code") or {}
print(f"  failed RPCs          {man.get('requests_failed', 0):,}  {errs or ''}")
print(f"  offered              {man.get('total_offered', 0):,}")
print(f"  accepted (acked)     {man.get('total_accepted', 0):,}")
print(f"  landed in the lake   {corr.get('landed', 'NOT MEASURED'):,}"
      if isinstance(corr.get("landed"), int) else "  landed in the lake   NOT MEASURED")

rc = corr.get("checks", {}).get("row_count") or {}
dup = corr.get("checks", {}).get("duplicates") or {}
print()
if rc:
    delta = rc.get("delta")
    if delta == 0:
        print("  DATA LOSS            none — every acked record is in the lake")
    elif isinstance(delta, int) and delta < 0:
        print(f"  DATA LOSS            {-delta:,} acked records never landed")
    else:
        print(f"  EXTRA ROWS           {delta:,} more rows than acked")
else:
    print("  DATA LOSS            NOT EVALUATED")
if dup:
    print(f"  DUPLICATES           {dup.get('excess_rows', 0):,} excess rows "
          f"across {dup.get('duplicate_keys', 0):,} keys")
print(f"  CORRECTNESS          {corr.get('verdict', 'NOT EVALUATED')}")
print()
if fault == "collector-sigkill":
    print("  Expected: a SIGKILL skips the drain, so batches still in the in-memory queue")
    print("  are lost. Acked-but-missing is the number that matters — the collector must")
    print("  not ack a record it has not persisted. Any loss here is a protocol bug, not")
    print("  an artefact of the kill.")
elif fault == "collector-sigterm":
    print("  Expected: zero loss. SIGTERM enters MAINTENANCE, serves 503 on /health for")
    print("  the LB-drain window, then flushes in-flight batches before stopping.")
elif fault == "compactor-sigkill":
    print("  Expected: zero loss and zero duplication — a merge commits atomically, so an")
    print("  interrupted one leaves orphaned Parquet files on S3, not a broken catalog.")
    print("  Those orphans are reclaimed by ducklake_cleanup_old_files on the housekeeping")
    print("  timer. Compare S3 object count against live file count to see them.")
PY

echo
info "artifacts: $RUN_DIR (fault.jsonl, correctness.json, report.md)"
