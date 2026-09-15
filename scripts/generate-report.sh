#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build results/<test-id>/report.md and charts/ from collected data.
#
#   scripts/generate-report.sh <test-id> [--no-charts]
#   scripts/generate-report.sh --all          rebuild every run's report
#
# The report is generated, never hand-edited. If a section says NOT MEASURED,
# the fix is to collect the missing file and re-run this — not to type a number
# into the markdown.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

[ $# -ge 1 ] || die "usage: generate-report.sh <test-id> [--no-charts] | --all"

load_env
PY="$(python_bin)"
EXTRA=()

if [ "$1" = "--all" ]; then
  shift
  [ "${1:-}" = "--no-charts" ] && EXTRA+=(--no-charts)
  n=0
  for d in "$PROJECT_ROOT"/results/*/; do
    [ -d "$d" ] || continue
    if "$PY" "$PROJECT_ROOT/bench/report.py" "$d" "${EXTRA[@]}"; then n=$((n+1)); fi
  done
  pass "reports" "$n regenerated"
  summary
  exit $?
fi

TEST_ID="$1"; shift
[ "${1:-}" = "--no-charts" ] && EXTRA+=(--no-charts)
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
[ -d "$RUN_DIR" ] || die "no such run: $RUN_DIR"

# The compaction timeline comes out of the compactor's log, since its
# LoggingMeterRegistry has no scrape endpoint. Re-parse in case the log was
# collected after the last report.
if [ -f "$RUN_DIR/logs/compactor.log" ]; then
  "$PY" "$PROJECT_ROOT/bench/parse_compactor_log.py" \
    --input "$RUN_DIR/logs/compactor.log" \
    --output "$RUN_DIR/raw/compaction.jsonl" 2>/dev/null || true
fi

if "$PY" "$PROJECT_ROOT/bench/report.py" "$RUN_DIR" "${EXTRA[@]}"; then
  pass "report" "$RUN_DIR/report.md"
  n="$(ls "$RUN_DIR/charts" 2>/dev/null | wc -l)"
  [ "$n" -gt 0 ] && pass "charts" "$n PNG(s) in $RUN_DIR/charts" \
                 || warn "charts" "none generated (matplotlib missing, or no samples)"
  # Anything the report could not measure is worth surfacing at the terminal too.
  miss="$(grep -c 'NOT MEASURED' "$RUN_DIR/report.md" || true)"
  [ "${miss:-0}" -gt 0 ] && warn "gaps" "$miss section(s) marked NOT MEASURED — see report.md"
else
  fail "report" "generation failed"
fi

summary
