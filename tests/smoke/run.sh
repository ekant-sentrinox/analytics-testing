#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# SMOKE — prove the pipeline works end to end. Not a performance measurement.
#
#   tests/smoke/run.sh
#
# 10 records/s for 60 s, then verify every acked record is readable out of the
# lake. Cheap enough to run before anything else and after every config change.
#
# Pass criteria:
#   * generator exits 0
#   * accepted == offered (at 10 rps nothing should ever be refused)
#   * correctness verdict PASS: zero loss, zero duplication
#   * the compactor's file counters move, or it explains why they did not
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

load_env
TEST_ID="$(new_test_id smoke)"

info "SMOKE — 10 rec/s for 60s, test id $TEST_ID"
echo

if ! tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 5; then
  fail "precondition" "collector gRPC $COLLECTOR_HOST:$COLLECTOR_GRPC_PORT unreachable"
  echo "  The service may be healthy on its own loopback while the security group"
  echo "  drops the port. Check scripts/connectivity-check.sh and NETWORK.md."
  exit 1
fi
pass "precondition" "collector gRPC reachable"

"$PROJECT_ROOT/scripts/run-test.sh" smoke --test-id "$TEST_ID"
rc=$?

RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
echo
info "verdict"

[ "$rc" -eq 0 ] && pass "generator" "exited 0" || fail "generator" "exit $rc"

if [ -f "$RUN_DIR/manifest.json" ]; then
  read -r offered accepted rejected <<< "$("$(python_bin)" -c "
import json; m=json.load(open('$RUN_DIR/manifest.json'))
print(m['total_offered'], m['total_accepted'], m['total_rejected'])")"
  [ "$offered" = "$accepted" ] \
    && pass "delivery" "$accepted/$offered records accepted, none refused" \
    || fail "delivery" "accepted $accepted of $offered offered ($rejected rejected)"
else
  fail "delivery" "no manifest.json"
fi

if [ -f "$RUN_DIR/correctness.json" ]; then
  v="$("$(python_bin)" -c "import json;print(json.load(open('$RUN_DIR/correctness.json'))['verdict'])")"
  [ "$v" = "PASS" ] && pass "correctness" "no loss, no duplication" \
                    || fail "correctness" "$v — see $RUN_DIR/correctness.txt"
else
  fail "correctness" "not evaluated"
fi

[ -f "$RUN_DIR/report.md" ] && pass "report" "$RUN_DIR/report.md" \
                            || warn "report" "not generated"

echo
summary
