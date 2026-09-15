#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Return the environment to a known-good state between test campaigns.
#
#   scripts/reset.sh [--with-data] [--yes]
#
# By default: stop the agents and exporter, restart both services, re-deploy
# current config, and verify health. Local results are KEPT — a benchmark that
# discards previous runs cannot show a regression.
#
# --with-data additionally wipes the lake (delegates to cleanup.sh --data, with
# all of its guard rails).
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

WITH_DATA=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --with-data) WITH_DATA=1 ;;
    --yes)       ASSUME_YES=1 ;;
    -h|--help)   sed -n '2,14p' "$0"; exit 0 ;;
    *) die "unknown argument: $a" ;;
  esac
done

load_env

info "stopping agents and exporter"
"$PROJECT_ROOT/scripts/stop.sh" --agents --exporter >/dev/null 2>&1 || true
pass "stopped" "host agents, pipeline exporter"

if [ "$WITH_DATA" -eq 1 ]; then
  info "wiping lake data"
  if [ "$ASSUME_YES" -eq 1 ]; then
    "$PROJECT_ROOT/scripts/cleanup.sh" --data --yes
  else
    "$PROJECT_ROOT/scripts/cleanup.sh" --data
    warn "data" "dry run only — add --yes to actually wipe"
  fi
fi

info "re-deploying current configuration"
"$PROJECT_ROOT/scripts/deploy-collector.sh" --no-build >/dev/null 2>&1 \
  && pass "collector" "redeployed and restarted" || fail "collector" "redeploy failed"
"$PROJECT_ROOT/scripts/deploy-compactor.sh" --no-build >/dev/null 2>&1 \
  && pass "compactor" "redeployed and restarted" || fail "compactor" "redeploy failed"

info "restarting monitoring"
"$PROJECT_ROOT/scripts/start.sh" --agents --exporter >/dev/null 2>&1 || true
pass "started" "host agents, pipeline exporter"

echo
info "verifying"
"$PROJECT_ROOT/scripts/health-check.sh" || true

echo
info "results/ was left untouched — use cleanup.sh --results to clear it"
summary
