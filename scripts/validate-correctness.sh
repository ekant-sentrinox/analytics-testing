#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Reconcile a run's landed rows against its generator manifest.
#
#   scripts/validate-correctness.sh <test-id> [--baseline <result.json>]
#
# Exit 0 only if there is no loss, no duplication, and no unexplained sequence
# gap. Pass --baseline with an earlier run's correctness.json to also prove that
# a compaction changed no column checksum.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

[ $# -ge 1 ] || die "usage: validate-correctness.sh <test-id> [--baseline FILE]"
TEST_ID="$1"; shift
BASELINE=""
[ "${1:-}" = "--baseline" ] && { BASELINE="$2"; shift 2; }

load_env
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
MANIFEST="$RUN_DIR/manifest.json"
[ -f "$MANIFEST" ] || die "no manifest for $TEST_ID — did the generator finish? ($MANIFEST)"

ARGS=(--manifest "$MANIFEST" --output "$RUN_DIR/correctness.json")
[ -n "$BASELINE" ] && ARGS+=(--baseline "$BASELINE")

PG_PASSWORD="$PG_PASSWORD" "$(python_bin)" "$PROJECT_ROOT/bench/validate_correctness.py" "${ARGS[@]}"
