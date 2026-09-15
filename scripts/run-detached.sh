#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run a test on SERVER 1 detached from your SSH session.
#
#   scripts/run-detached.sh <command...>
#   scripts/run-detached.sh --list
#   scripts/run-detached.sh --status <job>
#   scripts/run-detached.sh --follow <job>
#   scripts/run-detached.sh --stop   <job>
#
#   scripts/run-detached.sh tests/soak/run.sh --hours 24 --rps 3000
#   scripts/run-detached.sh tests/throughput/run.sh
#
# Close your laptop, lose the VPN, reboot your workstation — the run continues.
#
# WHY systemd-run AND NOT nohup
#
# nohup survives a hangup but nothing else: it leaves an orphan with no
# supervision, no resource accounting, no exit status anywhere, and no way to
# stop it cleanly other than hunting the pid. A 24-hour soak needs better.
#
# Each job becomes a transient systemd user unit, which gives:
#   * survival across logout, thanks to lingering (enabled by scripts/setup.sh)
#   * `systemctl --user status` with the real exit code, long after it finished
#   * journald capture of everything on stdout and stderr
#   * a clean `systemctl --user stop`, which propagates SIGTERM so the
#     generator writes its manifest instead of being killed mid-run
#   * a hard runtime cap, so a wedged job cannot occupy the box indefinitely
#
# The job also writes results/<test-id>/ exactly as an attached run does, plus a
# DONE marker with the exit code so completion can be detected without systemd.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

MAX_RUNTIME="${DETACHED_MAX_RUNTIME:-30h}"

jobs_list() {
  systemctl --user list-units 'bench-job-*' --all --no-legend --no-pager 2>/dev/null \
    | awk '{print $1}' | sed 's/\.service$//'
}

case "${1:-}" in
  --list)
    load_env
    info "detached jobs on $(hostname -s)"
    echo
    found=0
    for unit in $(jobs_list); do
      found=1
      state="$(systemctl --user show "$unit" -p ActiveState --value 2>/dev/null)"
      sub="$(systemctl --user show "$unit" -p SubState --value 2>/dev/null)"
      rc="$(systemctl --user show "$unit" -p ExecMainStatus --value 2>/dev/null)"
      since="$(systemctl --user show "$unit" -p ExecMainStartTimestamp --value 2>/dev/null)"
      case "$state" in
        active)  printf '  %-40s %s/%s  since %s\n' "$unit" "$state" "$sub" "$since" ;;
        failed)  printf '  %-40s %sFAILED%s (exit %s)  started %s\n' "$unit" "$C_RED" "$C_RST" "$rc" "$since" ;;
        *)       printf '  %-40s %s (exit %s)  started %s\n' "$unit" "$state" "$rc" "$since" ;;
      esac
    done
    [ "$found" -eq 0 ] && echo "  (none)"
    echo
    info "results:  ls -t results/ | head"
    exit 0
    ;;

  --status)
    [ -n "${2:-}" ] || die "usage: run-detached.sh --status <job>"
    systemctl --user status "$2" --no-pager -l | head -40
    exit 0
    ;;

  --follow)
    [ -n "${2:-}" ] || die "usage: run-detached.sh --follow <job>"
    exec journalctl --user -u "$2" -f
    ;;

  --stop)
    [ -n "${2:-}" ] || die "usage: run-detached.sh --stop <job>"
    info "sending SIGTERM to $2 — the generator will drain and write its manifest"
    systemctl --user stop "$2"
    pass "stopped" "$2"
    exit 0
    ;;

  ""|-h|--help)
    sed -n '2,32p' "$0"
    exit 0
    ;;
esac

# --- launch ------------------------------------------------------------------
load_env

# Lingering is what makes a user unit survive logout. Without it systemd tears
# down the user manager when the last session closes, taking the job with it.
if ! loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null | grep -q yes; then
  warn "lingering" "not enabled — enabling it now so jobs survive logout"
  sudo loginctl enable-linger "$(id -un)" \
    && pass "lingering" "enabled for $(id -un)" \
    || die "could not enable lingering; run: sudo loginctl enable-linger $(id -un)"
fi

CMD_NAME="$(basename "$1" .sh)"
JOB="bench-job-$(date -u +%Y%m%d-%H%M%S)-${CMD_NAME}"

# Resolve to an absolute path so the unit does not depend on a working directory.
TARGET="$1"; shift
[ -x "$TARGET" ] || TARGET="$PROJECT_ROOT/$TARGET"
[ -x "$TARGET" ] || die "not an executable command: $TARGET"

info "launching $JOB"
info "  command   $TARGET $*"
info "  cwd       $PROJECT_ROOT"
info "  max time  $MAX_RUNTIME"
echo

# PG_PASSWORD is passed through the environment rather than re-read inside the
# unit: systemd --user has no access to the interactive shell that read it, and
# putting it in the unit file would write the secret to disk.
systemd-run --user \
  --unit="$JOB" \
  --description="analytics-distributed-test: $TARGET $*" \
  --working-directory="$PROJECT_ROOT" \
  --property=RuntimeMaxSec="$MAX_RUNTIME" \
  --property=KillSignal=SIGTERM \
  --property=TimeoutStopSec=300 \
  --setenv=PG_PASSWORD="${PG_PASSWORD:-}" \
  --setenv=HOME="$HOME" \
  --setenv=PATH="$PATH" \
  -- /bin/bash -lc "
      set -o pipefail
      '$TARGET' $(printf '%q ' "$@") 2>&1
      rc=\$?
      # A marker so completion is detectable without talking to systemd, e.g.
      # from a cron poller or another machine.
      printf '{\"job\":\"$JOB\",\"exit_code\":%d,\"finished_at\":\"%s\"}\n' \
        \"\$rc\" \"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\" > '$PROJECT_ROOT/run/$JOB.done'
      exit \$rc
  " >/dev/null

sleep 2
if systemctl --user is-active "$JOB" >/dev/null 2>&1; then
  pass "launched" "$JOB is running"
else
  state="$(systemctl --user show "$JOB" -p ActiveState --value 2>/dev/null)"
  if [ "$state" = "failed" ]; then
    fail "launched" "$JOB failed immediately"
    systemctl --user status "$JOB" --no-pager -l | head -20
    exit 1
  fi
  warn "launched" "$JOB state is '$state' — check with --status"
fi

cat <<EOF

  You can disconnect now. The job keeps running on $(hostname -s).

    check       ./scripts/run-detached.sh --list
    follow      ./scripts/run-detached.sh --follow $JOB
    stop        ./scripts/run-detached.sh --stop $JOB
    results     ls -t results/ | head
    finished?   ls run/$JOB.done 2>/dev/null && cat run/$JOB.done

EOF
