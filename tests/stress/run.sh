#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# STRESS — push past the knee until something gives, then stop safely.
#
#   tests/stress/run.sh [--start 10000] [--step 10000] [--max 60000] [--hold 180]
#
# Different question from the throughput staircase. That one finds the highest
# rate the system can HOLD. This one finds where it BREAKS and how: does
# throughput plateau, does latency explode, does it start refusing, or does
# something die?
#
# Stops at the first of: throughput plateau (accepted stops rising while offered
# does), error rate above the profile's threshold, p99 above the threshold, or a
# component going down. The generator is stopped cleanly either way — the point
# is a measurement, not wreckage.
#
# NOTE ON THIS HARDWARE. SERVER 1 is a 2-vCPU t3.large running a Python
# generator. Past roughly 20-30k records/s it is plausible that the GENERATOR
# saturates first. That is a real result, but it is a result about the load
# generator. The check below watches generator CPU and stall time and says so
# explicitly rather than letting it be reported as a pipeline ceiling.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

START=10000; STEP=10000; MAXR=60000; HOLD=180
while [ $# -gt 0 ]; do
  case "$1" in
    --start) START="$2"; shift ;;
    --step)  STEP="$2"; shift ;;
    --max)   MAXR="$2"; shift ;;
    --hold)  HOLD="$2"; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_env
PY="$(python_bin)"
TEST_ID="$(new_test_id stress)"
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
mkdir -p "$RUN_DIR"

tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 5 || die "collector gRPC unreachable"

info "STRESS — $START to $MAXR rec/s in steps of $STEP, ${HOLD}s each"
info "test id $TEST_ID"
echo

"$PROJECT_ROOT/scripts/start.sh" --agents --exporter >/dev/null 2>&1 || true
"$PROJECT_ROOT/scripts/snapshot-state.sh" > "$RUN_DIR/state-before.json" 2>/dev/null || true

RESULTS="$RUN_DIR/levels.jsonl"
prev_accepted=0
rate="$START"
stop_reason="reached the configured maximum"

while [ "$rate" -le "$MAXR" ]; do
  echo
  info "level: $rate rec/s for ${HOLD}s"
  LEVEL_ID="${TEST_ID}-r${rate}"

  "$PY" -m generator.src.main \
    --config "$PROJECT_ROOT/config/generator.yaml" --profile stress \
    --rps "$rate" --duration "$HOLD" --test-id "$LEVEL_ID" \
    --results-dir "$PROJECT_ROOT/results" --log-level WARNING >/dev/null 2>&1 || true

  LEVEL_DIR="$PROJECT_ROOT/results/$LEVEL_ID"
  if [ ! -f "$LEVEL_DIR/generator.json" ]; then
    stop_reason="the generator produced no result at $rate rec/s"
    break
  fi

  eval "$("$PY" - "$LEVEL_DIR" <<'PY'
import json, sys
t = json.load(open(sys.argv[1] + "/generator.json"))["totals"]
dur = max(1e-9, t["duration_seconds"])
print(f'ACCEPTED={t["records_accepted"] / dur:.0f}')
print(f'OFFERED={t["records_offered"] / dur:.0f}')
print(f'REJECTED={t["records_rejected"]}')
print(f'P99={t["latency_p99_ms"]:.0f}')
print(f'FAILED={t["requests_failed"]}')
print(f'SENT={max(1, t["requests_sent"])}')
print(f'STALLED={t["stalled_ms"]:.0f}')
print(f'DURMS={dur * 1000:.0f}')
PY
)"

  err_pct=$(awk "BEGIN{printf \"%.2f\", 100*$FAILED/$SENT}")
  stall_pct=$(awk "BEGIN{printf \"%.1f\", 100*$STALLED/$DURMS}")
  gen_cpu="$(tail -30 "$PROJECT_ROOT/logs/host-generator.jsonl" 2>/dev/null | "$PY" -c '
import json,sys
vals=[json.loads(l).get("cpu_percent",0) for l in sys.stdin if l.strip()]
print(round(sum(vals)/len(vals),1) if vals else 0)' 2>/dev/null || echo 0)"

  printf '  offered %s/s  accepted %s/s  rejected %s  p99 %sms  errors %s%%  stalled %s%%  genCPU %s%%\n' \
    "$OFFERED" "$ACCEPTED" "$REJECTED" "$P99" "$err_pct" "$stall_pct" "$gen_cpu"

  echo "{\"rate\":$rate,\"offered\":$OFFERED,\"accepted\":$ACCEPTED,\"rejected\":$REJECTED,\"p99_ms\":$P99,\"error_pct\":$err_pct,\"stalled_pct\":$stall_pct,\"generator_cpu\":$gen_cpu}" >> "$RESULTS"

  # --- stop conditions ------------------------------------------------------
  if [ "$REJECTED" -gt 0 ]; then
    stop_reason="the collector started rejecting (RESOURCE_EXHAUSTED) at $rate rec/s"
    break
  fi
  if awk "BEGIN{exit !($err_pct > 5)}"; then
    stop_reason="error rate reached ${err_pct}% at $rate rec/s"
    break
  fi
  if [ "$P99" -gt 60000 ]; then
    stop_reason="p99 latency reached ${P99}ms at $rate rec/s"
    break
  fi
  if [ "$prev_accepted" -gt 0 ] && awk "BEGIN{exit !($ACCEPTED < $prev_accepted * 1.05)}"; then
    stop_reason="throughput plateaued: accepted rose from $prev_accepted to $ACCEPTED while offered rose by $STEP"
    break
  fi
  for spec in "collector:$COLLECTOR_SSH_HOST:$COLLECTOR_HEALTH_PORT" \
              "compactor:$COMPACTOR_SSH_HOST:$COMPACTOR_HEALTH_PORT"; do
    IFS=: read -r role host port <<< "$spec"
    if ! remote "$host" "curl -sf --max-time 4 http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      stop_reason="$role stopped responding at $rate rec/s"
      break 2
    fi
  done

  prev_accepted="$ACCEPTED"
  rate=$((rate + STEP))
done

echo
info "stopped: $stop_reason"
"$PROJECT_ROOT/scripts/snapshot-state.sh" > "$RUN_DIR/state-after.json" 2>/dev/null || true

echo
info "summary"
"$PY" - "$RESULTS" "$stop_reason" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
reason = sys.argv[2]
if not rows:
    print("  no levels completed")
    raise SystemExit(0)

print()
print("   offered/s   accepted/s   rejected     p99 ms   err%   stall%   genCPU%")
print("  " + "-" * 74)
for r in rows:
    print(f"  {r['offered']:>10,}  {r['accepted']:>11,}  {r['rejected']:>9,}  "
          f"{r['p99_ms']:>9,}  {r['error_pct']:>5}  {r['stalled_pct']:>6}  {r['generator_cpu']:>8}")

peak = max(rows, key=lambda r: r["accepted"])
print()
print(f"  peak accepted        {peak['accepted']:,}/s at an offered rate of {peak['offered']:,}/s")
print(f"  stopped because      {reason}")
print()

# Was the generator the limit, rather than the pipeline?
last = rows[-1]
if last["generator_cpu"] > 85 or last["stalled_pct"] > 20:
    print("  CAUTION: at the final level the generator host was at "
          f"{last['generator_cpu']}% CPU with {last['stalled_pct']}% pacer stall.")
    print("  This level measured the LOAD GENERATOR, not the pipeline. To push higher,")
    print("  run generators on more than one host, or raise batch_size so fewer, larger")
    print("  RPCs carry the same record rate.")
else:
    print("  The generator had headroom at the final level (CPU "
          f"{last['generator_cpu']}%, stall {last['stalled_pct']}%), so the limit found")
    print("  is a property of the pipeline.")
PY

echo
info "levels: $RESULTS"
