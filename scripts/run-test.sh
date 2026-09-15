#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run one test end to end.
#
#   scripts/run-test.sh <profile> [--rps N] [--duration S] [--test-id ID]
#                                 [--no-preflight] [--no-collect] [--no-report]
#
#   scripts/run-test.sh smoke
#   scripts/run-test.sh throughput
#   scripts/run-test.sh soak --duration 21600
#
# Sequence: preflight -> mark catalog state -> generator -> settle -> collect ->
#           validate correctness -> report.
#
# The test id is <UTC yyyymmdd-HHMMSS>-<profile> and every artifact for the run
# lands in results/<test-id>/.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

[ $# -ge 1 ] || { sed -n '2,16p' "$0"; exit 1; }
PROFILE="$1"; shift

PREFLIGHT=1; COLLECT=1; REPORT=1; TEST_ID=""
CONFIG_FILE="$PROJECT_ROOT/config/generator.yaml"
GEN_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-preflight) PREFLIGHT=0 ;;
    --no-collect)   COLLECT=0 ;;
    --no-report)    REPORT=0 ;;
    --test-id)      TEST_ID="$2"; shift ;;
    --rps)          GEN_ARGS+=(--rps "$2"); shift ;;
    --duration)     GEN_ARGS+=(--duration "$2"); shift ;;
    --workers)      GEN_ARGS+=(--workers "$2"); shift ;;
    --batch-size)   GEN_ARGS+=(--batch-size "$2"); shift ;;
    --retry-mode)   GEN_ARGS+=(--retry-mode "$2"); shift ;;
    --config)       CONFIG_FILE="$2"; shift ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

load_env
PY="$(python_bin)"
[ -n "$TEST_ID" ] || TEST_ID="$(new_test_id "$PROFILE")"
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
mkdir -p "$RUN_DIR"

echo
info "test id  $TEST_ID"
info "profile  $PROFILE"
info "results  $RUN_DIR"
echo

# --- 1. preflight ------------------------------------------------------------
if [ "$PREFLIGHT" -eq 1 ]; then
  info "preflight"
  if "$PROJECT_ROOT/scripts/preflight.sh" > "$RUN_DIR/preflight.txt" 2>&1; then
    pass "preflight" "all mandatory checks passed (see preflight.txt)"
  else
    grep -E '^FAIL' "$RUN_DIR/preflight.txt" | sed 's/^/  /'
    die "preflight failed — fix the above or re-run with --no-preflight to override"
  fi
else
  skip "preflight" "skipped by request"
fi

# --- 2. mark the starting state ------------------------------------------------
# Everything the correctness validator needs to bound its queries to this run.
info "recording pre-run state"
"$PROJECT_ROOT/scripts/snapshot-state.sh" > "$RUN_DIR/state-before.json" 2>/dev/null \
  && pass "state before" "$(( $(wc -c < "$RUN_DIR/state-before.json") )) bytes" \
  || warn "state before" "could not snapshot catalog/S3 state"

# --- 3. make sure the monitoring is up ------------------------------------------
"$PROJECT_ROOT/scripts/start.sh" --agents --exporter >"$RUN_DIR/start.txt" 2>&1 || true
pass "monitoring" "host agents + pipeline exporter running"

# The exporter writes to a shared log; note the offset so collect-results can
# slice out exactly this run's samples instead of shipping the whole history.
PIPELINE_OFFSET=0
[ -f "$PROJECT_ROOT/logs/pipeline.jsonl" ] && \
  PIPELINE_OFFSET="$(wc -l < "$PROJECT_ROOT/logs/pipeline.jsonl")"
echo "$PIPELINE_OFFSET" > "$RUN_DIR/.pipeline-offset"
for f in host-generator; do
  [ -f "$PROJECT_ROOT/logs/$f.jsonl" ] && wc -l < "$PROJECT_ROOT/logs/$f.jsonl" > "$RUN_DIR/.$f-offset"
done
for spec in "collector:$COLLECTOR_SSH_HOST" "compactor:$COMPACTOR_SSH_HOST"; do
  IFS=: read -r role host <<< "$spec"
  remote "$host" "wc -l < /opt/analytics-bench/agent/logs/host-$role.jsonl 2>/dev/null || echo 0" \
    > "$RUN_DIR/.host-$role-offset" 2>/dev/null || echo 0 > "$RUN_DIR/.host-$role-offset"
done

# --- 4. the run ------------------------------------------------------------------
echo
info "starting generator"
START_EPOCH="$(date +%s)"
set +e
"$PY" -m generator.src.main \
  --config "$CONFIG_FILE" \
  --profile "$PROFILE" \
  --test-id "$TEST_ID" \
  --results-dir "$PROJECT_ROOT/results" \
  "${GEN_ARGS[@]}" 2>&1 | tee "$RUN_DIR/generator.log"
GEN_RC="${PIPESTATUS[0]}"
set -e
END_EPOCH="$(date +%s)"

case "$GEN_RC" in
  0) pass "generator" "completed" ;;
  5) warn "generator" "aborted early (consecutive failures) — the run is still a result" ;;
  *) fail "generator" "exit $GEN_RC" ;;
esac

# --- 5. settle -------------------------------------------------------------------
# The export RPC acks on durability, so accepted rows are already committed. But
# the watermark and the compactor's view lag by up to one flush + one compaction
# tick; sampling before that reports a backlog that is about to disappear.
SETTLE="${SETTLE_SECONDS:-90}"
info "settling ${SETTLE}s so the last flush and one compaction cycle complete"
sleep "$SETTLE"

"$PROJECT_ROOT/scripts/snapshot-state.sh" > "$RUN_DIR/state-after.json" 2>/dev/null || true

# --- 6. collect --------------------------------------------------------------------
if [ "$COLLECT" -eq 1 ]; then
  echo; info "collecting results"
  "$PROJECT_ROOT/scripts/collect-results.sh" "$TEST_ID" || warn "collect" "partial"
fi

# --- 7. correctness -----------------------------------------------------------------
echo; info "validating correctness"
if "$PROJECT_ROOT/scripts/validate-correctness.sh" "$TEST_ID" > "$RUN_DIR/correctness.txt" 2>&1; then
  pass "correctness" "$(grep -m1 VERDICT "$RUN_DIR/correctness.txt" || echo PASS)"
else
  fail "correctness" "$(grep -m1 VERDICT "$RUN_DIR/correctness.txt" || echo FAIL) — see correctness.txt"
fi

# --- 8. report ------------------------------------------------------------------------
if [ "$REPORT" -eq 1 ]; then
  echo; info "generating report"
  "$PROJECT_ROOT/scripts/generate-report.sh" "$TEST_ID" || warn "report" "generation failed"
fi

cat > "$RUN_DIR/run.json" <<JSON
{
  "test_id": "$TEST_ID",
  "profile": "$PROFILE",
  "generator_exit_code": $GEN_RC,
  "started_at_epoch": $START_EPOCH,
  "ended_at_epoch": $END_EPOCH,
  "settle_seconds": $SETTLE,
  "wall_seconds": $((END_EPOCH - START_EPOCH))
}
JSON

echo
info "done — $RUN_DIR"
[ -f "$RUN_DIR/report.md" ] && info "report: $RUN_DIR/report.md"
summary
