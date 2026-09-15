#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One command: run a throughput test, wait for it, build a plain-language
# report + charts, commit, and push to GitHub.
#
#   scripts/run-and-publish.sh                              (default 1k-25k)
#   scripts/run-and-publish.sh --steps "50000" --step-duration 600
#
# Any args are forwarded to tests/throughput/run.sh (--steps/--step-duration).
# ---------------------------------------------------------------------------
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

cd "$PROJECT_ROOT"
PY="$(python_bin)"

LAUNCH_OUT="$(./scripts/run-detached.sh tests/throughput/run.sh "$@" 2>&1)"
echo "$LAUNCH_OUT"
JOB="$(echo "$LAUNCH_OUT" | grep -oE 'bench-job-[0-9]{8}-[0-9]{6}-run' | head -1)"
[ -n "$JOB" ] || die "could not determine job name from launch output"

DONE_FILE="run/$JOB.done"
info "waiting for $JOB to finish"
i=0
while [ ! -f "$DONE_FILE" ] && [ "$i" -lt 720 ]; do
  sleep 30
  i=$((i + 1))
done
[ -f "$DONE_FILE" ] || die "timed out after 6h waiting for $DONE_FILE"

RC="$(python3 -c "import json; print(json.load(open('$DONE_FILE'))['exit_code'])")"
[ "$RC" = "0" ] || warn "job exit code" "$RC (continuing to publish whatever completed)"

# The test id is the newest results/ dir created since launch.
TEST_ID="$(ls -t results/ | grep -E '^[0-9]{8}-[0-9]{6}-' | head -1)"
[ -n "$TEST_ID" ] || die "could not determine test id from results/"
info "test id" "$TEST_ID"

[ -f "results/$TEST_ID/report.md" ] || die "results/$TEST_ID/report.md missing — did generate-report.sh run?"

"$PY" bench/publish_report.py "$TEST_ID" || die "publish_report.py failed"

git add "report/$TEST_ID/"
git commit -m "test: $TEST_ID results and report

$(python3 -c "
import json
g = json.load(open('results/$TEST_ID/generator.json'))
t = g.get('totals', {})
steps = g.get('steps', [])
maxr = max((s['target_rps'] for s in steps), default=0)
print(f\"max rate {maxr:,.0f} rec/s, offered {t.get('records_offered',0):,}, accepted {t.get('records_accepted',0):,}, rejected {t.get('records_rejected',0):,}\")
")"
git push
pass "published" "report/$TEST_ID pushed to origin"
