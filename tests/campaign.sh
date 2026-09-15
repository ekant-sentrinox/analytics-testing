#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CAMPAIGN — run every test in sequence, unattended.
#
#   scripts/run-detached.sh tests/campaign.sh            # the intended way
#   tests/campaign.sh --skip soak,stress                 # skip stages by name
#
# Order is deliberate:
#   1. functional        contracts first — nothing else means anything if they fail
#   2. smoke             the cheapest end-to-end proof
#   3. throughput        the staircase, while the pipeline is undisturbed
#   4. soak              shortened to 1 h (the spec's 6 h first pass is a
#                        separate scheduled run; a campaign that takes a day
#                        would never be run at all). Labelled as shortened.
#   5. drain             immediately after the soak, per TEST 19
#   6. recovery-sigterm  faults LAST, so restarts cannot pollute the
#   7. recovery-compactor  measurement runs above
#   8. memory-pressure
#   9. disk-pressure
#  10. reports           regenerate everything, write the campaign summary
#
# Each stage's exit code is recorded and the campaign continues on failure —
# a failed stage is a result, not a reason to lose the remaining stages.
# Everything lands in results/<campaign-id>/campaign.jsonl plus the usual
# per-run directories.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/.." && pwd)/scripts/lib/common.sh"

SKIP=""
[ "${1:-}" = "--skip" ] && SKIP=",${2:-},"

load_env
CAMPAIGN_ID="$(new_test_id campaign)"
CAMPAIGN_DIR="$PROJECT_ROOT/results/$CAMPAIGN_ID"
mkdir -p "$CAMPAIGN_DIR"
LOG="$CAMPAIGN_DIR/campaign.jsonl"

note() {                        # note <stage> <status> <rc> <seconds>
  printf '{"stage":"%s","status":"%s","exit_code":%s,"seconds":%s,"at":"%s"}\n' \
    "$1" "$2" "$3" "$4" "$(utc_now)" >> "$LOG"
}

# Never start while another generator is mid-run: two generators sharing the
# tunnel and the collector would corrupt both runs' accounting.
info "waiting for any in-flight test to finish"
while pgrep -f "generator.src.main" >/dev/null 2>&1; do sleep 30; done
pass "clear" "no generator running"

run_stage() {                   # run_stage <name> <cmd...>
  local name="$1"; shift
  if [ -n "$SKIP" ] && [ "${SKIP#*,"$name",}" != "$SKIP" ]; then
    warn "$name" "skipped by request"
    note "$name" skipped 0 0
    return
  fi
  echo; echo "############################################################"
  info "STAGE $name — $(utc_now)"
  echo "############################################################"
  local started rc
  started=$(date +%s)
  if "$@" > "$CAMPAIGN_DIR/$name.log" 2>&1; then rc=0; else rc=$?; fi
  local secs=$(( $(date +%s) - started ))
  tail -25 "$CAMPAIGN_DIR/$name.log" | sed 's/^/  /'
  if [ "$rc" -eq 0 ]; then
    pass "$name" "completed in ${secs}s"
    note "$name" completed "$rc" "$secs"
  else
    fail "$name" "exit $rc after ${secs}s — campaign continues; see $name.log"
    note "$name" failed "$rc" "$secs"
  fi
  # Let queues drain and the compactor settle between stages so one stage's
  # tail cannot leak into the next stage's baseline.
  sleep 60
}

info "campaign $CAMPAIGN_ID"
[ "${BENCH_TRANSPORT:-direct}" != "direct" ] && \
  warn "transport" "$BENCH_TRANSPORT — throughput/latency stages will be labelled NOT VALID"

run_stage functional  "$PROJECT_ROOT/tests/functional/run.sh"
run_stage smoke       "$PROJECT_ROOT/tests/smoke/run.sh"
# Shortened staircase: 4 levels x 10 min. The full 7-level spec staircase is a
# separate scheduled run once the 4317 rule exists and rates are valid.
run_stage throughput  "$PROJECT_ROOT/tests/throughput/run.sh" \
                        --steps "1000,2000,5000,10000" --step-duration 600
# Soak shortened to 1 h at 75% of the light-verified rate band; the spec's
# 6 h/12 h/24 h ladder is for after the security-group fix.
run_stage soak        "$PROJECT_ROOT/tests/soak/run.sh" --hours 1 --rps 1500
run_stage drain       "$PROJECT_ROOT/tests/drain/run.sh"
run_stage recovery-sigterm   "$PROJECT_ROOT/tests/recovery/run.sh" collector-sigterm \
                        --rps 2000 --duration 600 --at 240
run_stage recovery-compactor "$PROJECT_ROOT/tests/recovery/run.sh" compactor-sigkill \
                        --rps 2000 --duration 600 --at 240
run_stage memory-pressure "$PROJECT_ROOT/tests/failure/memory_pressure.sh" \
                        --limit 1G --rps 2000 --duration 300
run_stage disk-pressure   "$PROJECT_ROOT/tests/failure/disk_pressure.sh" \
                        --target-percent 93 --rps 1500 --duration 240

run_stage reports     "$PROJECT_ROOT/scripts/generate-report.sh" --all

# --- campaign summary ---------------------------------------------------------
echo
info "campaign summary"
"$(python_bin)" - "$CAMPAIGN_DIR" "$PROJECT_ROOT/results" <<'PY'
import glob, json, os, sys
cdir, results = sys.argv[1], sys.argv[2]

stages = [json.loads(l) for l in open(os.path.join(cdir, "campaign.jsonl")) if l.strip()]
lines = ["# Campaign summary — " + os.path.basename(cdir), ""]
lines += ["| Stage | Status | Exit | Duration |", "|---|---|---|---|"]
for s in stages:
    lines.append("| %s | %s | %s | %ds |" % (s["stage"], s["status"], s["exit_code"], s["seconds"]))
lines.append("")

lines += ["## Runs produced", "",
          "| Run | Offered | Accepted | Rejected | Rate | Correctness |", "|---|---|---|---|---|---|"]
for d in sorted(glob.glob(os.path.join(results, "*/"))):
    mf = os.path.join(d, "manifest.json")
    if not os.path.exists(mf):
        continue
    m = json.load(open(mf))
    try:
        v = json.load(open(os.path.join(d, "correctness.json")))["verdict"]
    except Exception:
        v = "-"
    try:
        rate = json.load(open(os.path.join(d, "generator.json")))["totals"]["accepted_rps_mean"]
    except Exception:
        rate = 0
    lines.append("| %s | %s | %s | %s | %.0f/s | %s |" % (
        os.path.basename(d.rstrip("/")), format(m["total_offered"], ","),
        format(m["total_accepted"], ","), m["total_rejected"], rate, v))
lines.append("")
lines.append("Transport during this campaign: **%s** — see NETWORK.md for validity."
             % os.environ.get("BENCH_TRANSPORT", "direct"))

open(os.path.join(cdir, "CAMPAIGN_SUMMARY.md"), "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
PY

info "done — $CAMPAIGN_DIR/CAMPAIGN_SUMMARY.md"
summary
