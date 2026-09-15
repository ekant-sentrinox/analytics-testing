#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# FUNCTIONAL — check the pipeline's contracts, not its speed.
#
#   tests/functional/run.sh
#
# These are the behaviours a throughput test silently assumes. If auth is not
# actually enforced, or the queue claim is not actually required, then the
# performance numbers describe a system nobody is going to run.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

load_env
PY="$(python_bin)"

tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 5 || die "collector gRPC unreachable"

info "FUNCTIONAL — collector contracts"
echo

PG_PASSWORD="$PG_PASSWORD" "$PY" "$PROJECT_ROOT/tests/functional/test_contracts.py"
rc=$?

echo
[ "$rc" -eq 0 ] && info "all contracts hold" || info "one or more contracts are not enforced"
exit "$rc"
