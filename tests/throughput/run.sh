#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# THROUGHPUT — find the maximum SUSTAINABLE rate with a staircase.
#
#   tests/throughput/run.sh [--steps "1000,5000,10000"] [--step-duration 600]
#
# Sustainability is not "did it keep up for a minute". A level counts only if
# ALL of these hold for the whole level:
#
#   accepted >= 0.99 * offered
#   the uncompacted file backlog (B2) is not trending up over the second half
#   no RESOURCE_EXHAUSTED
#   zero loss and zero duplication, validated after the fact
#   no restart, no OOM
#
# The backlog condition is the one people drop, and it is the one that matters:
# a pipeline can ack every record while quietly accumulating small files it will
# never catch up on. That is a system that fails tomorrow, reported as a pass.
#
# STEP DURATION. A step must be long enough to contain several compaction
# cycles, or the backlog slope is noise. With major_compaction_frequency at
# 10 minutes the floor is ~10 min per step; the spec's rule of thumb is
#   step >= max(10 * max_delay_ms, 5 * major_compaction_frequency, 10 min).
# Shortening the step alone invalidates the result — shorten the compaction
# frequencies in proportion and say so.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

STEPS=""
STEP_DURATION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --steps)         STEPS="$2"; shift ;;
    --step-duration) STEP_DURATION="$2"; shift ;;
    -h|--help)       sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_env
PY="$(python_bin)"
TEST_ID="$(new_test_id throughput)"

CONFIG="$PROJECT_ROOT/config/generator.yaml"
if [ -n "$STEPS" ] || [ -n "$STEP_DURATION" ]; then
  # Materialise an override config rather than mutating the checked-in one.
  CONFIG="$PROJECT_ROOT/results/$TEST_ID.generator.yaml"
  mkdir -p "$PROJECT_ROOT/results"
  STEPS="$STEPS" STEP_DURATION="$STEP_DURATION" SRC="$PROJECT_ROOT/config/generator.yaml" \
  OUT="$CONFIG" "$PY" - <<'PY'
import os, yaml
doc = yaml.safe_load(open(os.environ["SRC"]))
prof = doc["profiles"]["throughput"]
steps = os.environ.get("STEPS") or ""
dur = os.environ.get("STEP_DURATION") or ""
if steps:
    rates = [float(x) for x in steps.split(",") if x.strip()]
    base = float(dur) if dur else prof["steps"][0]["duration_seconds"]
    prof["steps"] = [{"rps": r, "duration_seconds": base} for r in rates]
elif dur:
    for s in prof["steps"]:
        s["duration_seconds"] = float(dur)
yaml.safe_dump(doc, open(os.environ["OUT"], "w"), sort_keys=False)
print("wrote", os.environ["OUT"])
PY
fi

n_steps="$("$PY" -c "
import yaml; print(len(yaml.safe_load(open('$CONFIG'))['profiles']['throughput']['steps']))")"
total="$("$PY" -c "
import yaml
s=yaml.safe_load(open('$CONFIG'))['profiles']['throughput']['steps']
print(int(sum(x['duration_seconds'] for x in s)))")"

info "THROUGHPUT staircase — $n_steps steps, ${total}s (~$((total / 60)) min) plus settle"
info "test id $TEST_ID"
"$PY" -c "
import yaml
for s in yaml.safe_load(open('$CONFIG'))['profiles']['throughput']['steps']:
    print('    %8.0f rec/s for %5.0fs' % (s['rps'], s['duration_seconds']))"
echo

if ! tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 5; then
  die "collector gRPC unreachable — see NETWORK.md"
fi

# Warn if a step cannot contain a major compaction cycle.
major="$("$PY" "$PROJECT_ROOT/scripts/lib/cfg.py" get "$PROJECT_ROOT/config/compactor.yaml" \
         compactor.schedule.major_compaction_frequency)"
shortest="$("$PY" -c "
import yaml
s=yaml.safe_load(open('$CONFIG'))['profiles']['throughput']['steps']
print(int(min(x['duration_seconds'] for x in s)))")"
info "shortest step ${shortest}s, major compaction every '$major'"
[ "$shortest" -lt 600 ] && warn "step duration" \
  "steps shorter than 10 min may not contain enough compaction cycles for the backlog slope to mean anything"

SETTLE_SECONDS=180 "$PROJECT_ROOT/scripts/run-test.sh" throughput --test-id "$TEST_ID" \
  ${CONFIG:+--config "$CONFIG"} || true
# run-test.sh reads config/generator.yaml; when an override was materialised,
# point the generator at it explicitly for the record.
[ "$CONFIG" != "$PROJECT_ROOT/config/generator.yaml" ] && \
  cp "$CONFIG" "$PROJECT_ROOT/results/$TEST_ID/generator.override.yaml" 2>/dev/null || true

RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
echo
info "per-level sustainability"
echo

"$PY" - "$RUN_DIR" <<'PY'
import json, os, sys, statistics

run = sys.argv[1]

def load(p, default=None):
    try:
        return json.load(open(os.path.join(run, p)))
    except Exception:
        return default

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

gen = load("generator.json", {}) or {}
steps = gen.get("steps") or []
pipeline = jsonl("raw/pipeline.jsonl")
corr = load("correctness.json", {}) or {}

if not steps:
    print("  no step results in generator.json")
    raise SystemExit(1)

def backlog_slope(t0, t1):
    """files/min over the second half of [t0, t1] — the window the criterion
    is defined on, because the first half is warm-up by construction."""
    pts = [(r["ts"], (r.get("catalog") or {}).get("backlog_files"))
           for r in pipeline
           if isinstance(r.get("ts"), (int, float)) and t0 <= r["ts"] <= t1
           and (r.get("catalog") or {}).get("backlog_files") is not None]
    if len(pts) < 4:
        return None
    pts = pts[len(pts) // 2:]
    if len(pts) < 3:
        return None
    xs = [p[0] - pts[0][0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den) * 60

print("  step  target rps   accepted/s   ratio    rejected   backlog slope   verdict")
print("  " + "-" * 76)

best = None
for s in steps:
    dur = max(1e-9, s["duration_seconds"])
    ratio = s["accepted"] / s["offered"] if s["offered"] else 0.0
    acc_ps = s["accepted"] / dur
    slope = backlog_slope(s["started_at"], s["ended_at"])

    reasons = []
    if ratio < 0.99:
        reasons.append(f"accepted/offered {ratio:.4f} < 0.99")
    if s["rejected"] > 0:
        reasons.append(f"{s['rejected']:,} records rejected")
    if slope is None:
        reasons.append("backlog slope unknown")
    elif slope > 0.5:
        reasons.append(f"backlog +{slope:.2f} files/min")

    ok = not reasons
    verdict = "SUSTAINED" if ok else "FAILED"
    slope_s = "  n/a  " if slope is None else f"{slope:+7.2f}"
    print(f"  {s['index']:>4}  {s['target_rps']:>10,.0f}  {acc_ps:>11,.0f}  "
          f"{ratio:>6.4f}  {s['rejected']:>10,}  {slope_s:>13}   {verdict}")
    if reasons and not ok:
        for r in reasons:
            print(f"        └─ {r}")
    if ok:
        best = s
    else:
        break

print()
if corr.get("verdict") and corr["verdict"] != "PASS":
    print(f"  CORRECTNESS {corr['verdict']} — no level below can be reported as passing.")
    print(f"  failed: {', '.join(corr.get('failed_checks', []))}")
elif best:
    print(f"  MAXIMUM SUSTAINABLE RATE: {best['target_rps']:,.0f} records/s "
          f"(step {best['index']}, {best['accepted'] / best['duration_seconds']:,.0f}/s accepted)")
    peak = max(s["accepted"] / max(1e-9, s["duration_seconds"]) for s in steps)
    print(f"  Highest instantaneous accepted rate: {peak:,.0f} records/s "
          "— a different claim, reported separately.")
else:
    print("  NO LEVEL WAS SUSTAINABLE. The lowest step already failed a criterion.")
PY

echo
info "full report: $RUN_DIR/report.md"
