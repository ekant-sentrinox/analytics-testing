#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# SOAK — hold a sub-ceiling rate for hours and watch for drift.
#
#   tests/soak/run.sh [--hours 6] [--rps 3000]
#
# Rate should be 70-80% of the measured sustainable figure from the throughput
# staircase. Running a soak at the ceiling tests the ceiling, not stability.
#
# The failure this test exists to catch is SLOW: backlog, RSS, GC time, file
# count and catalog size trending up over hours while every instantaneous
# metric looks fine. Hourly checkpoints make the trend visible; the final
# comparison between the first and last hour is the actual result.
#
# On these hosts there is one more thing to watch: t3.large is burstable. A run
# long enough to exhaust CPU credits will slow down for reasons that have
# nothing to do with the software. Check the credit balance before blaming code.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

HOURS=6
RPS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --hours) HOURS="$2"; shift ;;
    --rps)   RPS="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_env
PY="$(python_bin)"
DURATION=$(awk "BEGIN{print int($HOURS*3600)}")
TEST_ID="$(new_test_id "soak${HOURS}h")"
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"

info "SOAK — ${HOURS}h${RPS:+ at $RPS rec/s}, test id $TEST_ID"
info "checkpoints hourly into $RUN_DIR/checkpoints.jsonl"
echo

tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 5 || die "collector gRPC unreachable"

free_gb="$(df -BG --output=avail "$PROJECT_ROOT" | tail -1 | tr -dc '0-9')"
[ "${free_gb:-0}" -lt 3 ] && warn "disk" \
  "only ${free_gb} GiB free on SERVER 1; a ${HOURS}h run at 1 s sampling writes a lot of JSONL"

mkdir -p "$RUN_DIR"

# Hourly checkpoints, taken alongside the run. The soak's own per-second data is
# already being captured; these are the coarse snapshots that make drift legible
# without reading a million samples.
(
  n=0
  while [ "$n" -lt "$HOURS" ]; do
    sleep 3600
    n=$((n + 1))
    {
      printf '{"checkpoint":%d,"hour":%d,"at":"%s","state":' "$n" "$n" "$(utc_now)"
      "$PROJECT_ROOT/scripts/snapshot-state.sh" 2>/dev/null | tr -d '\n'
      printf '}\n'
    } >> "$RUN_DIR/checkpoints.jsonl"
  done
) &
CHECKPOINTER=$!
trap 'kill $CHECKPOINTER 2>/dev/null || true' EXIT

ARGS=(soak --test-id "$TEST_ID" --duration "$DURATION")
[ -n "$RPS" ] && ARGS+=(--rps "$RPS")
SETTLE_SECONDS=300 "$PROJECT_ROOT/scripts/run-test.sh" "${ARGS[@]}" || true

kill $CHECKPOINTER 2>/dev/null || true

echo
info "drift analysis — first hour versus last"
"$PY" - "$RUN_DIR" <<'PY'
import json, os, statistics, sys

run = sys.argv[1]

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

def dig(d, *ks):
    for k in ks:
        d = (d or {}).get(k) if isinstance(d, dict) else None
    return d

gen = jsonl("generator.jsonl")
pipe = jsonl("raw/pipeline.jsonl")
hosts = {r: jsonl(f"raw/host-{r}.jsonl") for r in ("generator", "collector", "compactor")}

def window(rows, first=True, seconds=3600):
    rows = [r for r in rows if isinstance(r.get("ts"), (int, float))]
    if not rows:
        return []
    if first:
        t0 = rows[0]["ts"]
        return [r for r in rows if r["ts"] <= t0 + seconds]
    t1 = rows[-1]["ts"]
    return [r for r in rows if r["ts"] >= t1 - seconds]

def mean_of(rows, fn):
    vals = [fn(r) for r in rows]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return statistics.fmean(vals) if vals else None

def line(label, a, b, unit="", better_lower=True, tol=0.05):
    if a is None or b is None:
        print(f"  {label:<34} {'n/a':>14} {'n/a':>14}   NOT MEASURED")
        return
    if a == 0:
        drift = 0.0 if b == 0 else float("inf")
    else:
        drift = (b - a) / abs(a)
    if drift == float("inf"):
        flag = "GREW FROM ZERO"
    elif abs(drift) <= tol:
        flag = "stable"
    elif (drift > 0) == better_lower:
        flag = f"DRIFT {drift:+.1%}"
    else:
        flag = f"improved {drift:+.1%}"
    print(f"  {label:<34} {a:>14,.2f} {b:>14,.2f}   {flag}{unit}")

print()
print(f"  {'metric':<34} {'first hour':>14} {'last hour':>14}   drift")
print("  " + "-" * 82)

line("accepted rec/s", mean_of(window(gen), lambda r: r.get("accepted_rps")),
     mean_of(window(gen, False), lambda r: r.get("accepted_rps")), better_lower=False)
line("latency p99 (ms)", mean_of(window(gen), lambda r: r.get("latency_p99_ms")),
     mean_of(window(gen, False), lambda r: r.get("latency_p99_ms")))
line("backlog files (B2)", mean_of(window(pipe), lambda r: dig(r, "catalog", "backlog_files")),
     mean_of(window(pipe, False), lambda r: dig(r, "catalog", "backlog_files")))
line("small files", mean_of(window(pipe), lambda r: dig(r, "catalog", "small_files")),
     mean_of(window(pipe, False), lambda r: dig(r, "catalog", "small_files")))
line("visibility lag (s)", mean_of(window(pipe), lambda r: dig(r, "watermark", "lag_seconds")),
     mean_of(window(pipe, False), lambda r: dig(r, "watermark", "lag_seconds")))
line("catalog db (MiB)",
     (mean_of(window(pipe), lambda r: dig(r, "catalog", "pg_database_size_bytes")) or 0) / 1048576,
     (mean_of(window(pipe, False), lambda r: dig(r, "catalog", "pg_database_size_bytes")) or 0) / 1048576)

for role, rows in hosts.items():
    if not rows:
        continue
    line(f"{role} RSS (MiB)",
         (mean_of(window(rows), lambda r: dig(r, "process", "rss_bytes")) or 0) / 1048576,
         (mean_of(window(rows, False), lambda r: dig(r, "process", "rss_bytes")) or 0) / 1048576)
    line(f"{role} CPU %", mean_of(window(rows), lambda r: r.get("cpu_percent")),
         mean_of(window(rows, False), lambda r: r.get("cpu_percent")))

restarts = {r: max((x.get("process_restarts") or 0) for x in rows)
            for r, rows in hosts.items() if rows}
ooms = {r: max((x.get("oom_kills") or 0) for x in rows)
        for r, rows in hosts.items() if rows if any(x.get("oom_kills") is not None for x in rows)}
print()
print(f"  process restarts: {restarts or 'not measured'}")
print(f"  OOM kills:        {ooms or 'none observed'}")

corr = None
try:
    corr = json.load(open(os.path.join(run, "correctness.json")))
except Exception:
    pass
print(f"  correctness:      {corr.get('verdict') if corr else 'NOT EVALUATED'}")
print()
print("  A stable soak has all rows 'stable'. Any monotonic DRIFT in backlog, RSS or")
print("  lag is the finding, even if throughput held — that is what breaks on day three.")
PY

echo
info "next: tests/drain/run.sh $TEST_ID  (TEST 19 — measure how fast the backlog drains)"
info "report: $RUN_DIR/report.md"
