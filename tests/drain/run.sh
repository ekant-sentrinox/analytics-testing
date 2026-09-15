#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# DRAIN — how fast does the backlog clear once ingestion stops?
#
#   tests/drain/run.sh [<test-id-of-the-run-that-created-the-backlog>]
#
# Stop the generator, leave the collector and compactor running, and sample the
# uncompacted file backlog every 10 s until it reaches steady state. Gives the
# drain rate and the drain time, which together answer "how long after a spike
# before queries are fast again".
#
# Steady state here means the backlog has stopped falling — not that it reached
# zero. It never reaches zero: the collector keeps registering new files from
# any residual traffic, and a merge always leaves a file behind. Waiting for
# zero would hang forever.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

SOURCE_RUN="${1:-}"
load_env
PY="$(python_bin)"

TEST_ID="$(new_test_id drain)"
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
mkdir -p "$RUN_DIR"

INTERVAL=10
MAX_MINUTES="${DRAIN_MAX_MINUTES:-60}"
# Stop once the slope has been flat for this many consecutive samples.
FLAT_SAMPLES=12

info "DRAIN — sampling backlog every ${INTERVAL}s, up to ${MAX_MINUTES} min"
[ -n "$SOURCE_RUN" ] && info "backlog created by run $SOURCE_RUN"
info "test id $TEST_ID"
echo

# Make sure nothing is still generating.
if pgrep -f "generator.src.main" >/dev/null 2>&1; then
  warn "generator" "still running — killing it, the drain must start from zero ingestion"
  pkill -f "generator.src.main" || true
  sleep 5
fi
pass "generator" "stopped"

for spec in "collector:$COLLECTOR_SSH_HOST:bench-collector" "compactor:$COMPACTOR_SSH_HOST:bench-compactor"; do
  IFS=: read -r role host unit <<< "$spec"
  state="$(remote "$host" "systemctl is-active $unit" 2>/dev/null || echo unreachable)"
  [ "$state" = active ] && pass "$role" "left running (required)" \
                        || fail "$role" "$unit is $state — the drain cannot be measured"
done

echo
info "sampling"
printf '  %-10s %10s %12s %14s %12s\n' "elapsed" "files" "small" "bytes" "lag s"

SAMPLES="$RUN_DIR/drain.jsonl"
START="$(date +%s)"
flat=0
prev_files=""
initial_files=""
initial_bytes=""

while true; do
  now="$(date +%s)"
  elapsed=$((now - START))
  [ "$elapsed" -gt $((MAX_MINUTES * 60)) ] && { warn "timeout" "stopping after ${MAX_MINUTES} min"; break; }

  snap="$("$PROJECT_ROOT/scripts/snapshot-state.sh" 2>/dev/null || echo '{}')"
  read -r files small nbytes lag <<< "$(echo "$snap" | "$PY" -c '
import json,sys
d=json.load(sys.stdin) or {}
c=d.get("catalog") or {}
print(c.get("live_files",0), c.get("small_files",0), c.get("live_bytes",0), "null")')"

  echo "{\"elapsed\":$elapsed,\"at\":\"$(utc_now)\",\"files\":$files,\"small_files\":$small,\"bytes\":$nbytes}" \
    >> "$SAMPLES"
  printf '  %-10s %10s %12s %14s %12s\n' "${elapsed}s" "$files" "$small" "$nbytes" "$lag"

  [ -z "$initial_files" ] && { initial_files="$files"; initial_bytes="$nbytes"; }

  if [ -n "$prev_files" ]; then
    if [ "$files" -ge "$prev_files" ]; then
      flat=$((flat + 1))
    else
      flat=0
    fi
  fi
  prev_files="$files"

  if [ "$flat" -ge "$FLAT_SAMPLES" ]; then
    pass "steady state" "backlog stopped falling for $((FLAT_SAMPLES * INTERVAL))s"
    break
  fi
  sleep "$INTERVAL"
done

echo
info "drain result"
"$PY" - "$SAMPLES" "$RUN_DIR" <<'PY'
import json, os, sys

path, run = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(path) if l.strip()]
if len(rows) < 2:
    print("  NOT MEASURED — fewer than two samples")
    raise SystemExit(0)

first, last = rows[0], rows[-1]
lowest = min(rows, key=lambda r: r["files"])
drained_files = first["files"] - lowest["files"]
drained_bytes = first["bytes"] - lowest["bytes"]
secs = lowest["elapsed"] - first["elapsed"]

print()
print(f"  initial backlog      {first['files']:,} files, {first['bytes'] / 1048576:,.1f} MiB")
print(f"  minimum reached      {lowest['files']:,} files, {lowest['bytes'] / 1048576:,.1f} MiB"
      f"  at t+{lowest['elapsed']}s")
print(f"  final                {last['files']:,} files, {last['bytes'] / 1048576:,.1f} MiB"
      f"  at t+{last['elapsed']}s")
print()
if drained_files > 0 and secs > 0:
    print(f"  drain time           {secs}s ({secs / 60:.1f} min)")
    print(f"  drain rate           {drained_files / (secs / 60):,.1f} files/min, "
          f"{drained_bytes / 1048576 / (secs / 60):,.2f} MiB/min")
elif drained_files <= 0:
    print("  drain rate           NOT MEASURED — the backlog never fell.")
    print("                       Either there was nothing to compact (files already above")
    print("                       minor_compaction_max_size), or the compactor is not")
    print("                       merging. Check the compactor's totalFilesCompacted.")
print()
print("  Note: the backlog does not reach zero, and should not be expected to. A merge")
print("  produces a file, and any residual ingestion registers more. Steady state is the")
print("  measurement.")

json.dump({
    "initial_files": first["files"], "initial_bytes": first["bytes"],
    "min_files": lowest["files"], "min_bytes": lowest["bytes"],
    "drain_seconds": secs, "drained_files": drained_files, "drained_bytes": drained_bytes,
    "files_per_min": (drained_files / (secs / 60)) if secs > 0 and drained_files > 0 else None,
    "samples": len(rows),
}, open(os.path.join(run, "drain-summary.json"), "w"), indent=2)
PY

echo
info "samples: $SAMPLES"
