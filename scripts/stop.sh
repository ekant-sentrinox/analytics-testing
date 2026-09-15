#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Bring the environment down.
#
#   scripts/stop.sh [--services] [--agents] [--exporter] [--monitoring] [--all]
#
# With no flags: --agents --exporter. The pipeline services are deliberately
# LEFT RUNNING by default — stopping the collector mid-drain is a data-loss
# event, and after a soak you usually want the compactor to keep draining the
# backlog (that is TEST 19). Ask for --services explicitly.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

DO_SERVICES=0; DO_AGENTS=0; DO_EXPORTER=0; DO_MON=0
if [ $# -eq 0 ]; then
  DO_AGENTS=1; DO_EXPORTER=1
else
  for a in "$@"; do
    case "$a" in
      --services)   DO_SERVICES=1 ;;
      --agents)     DO_AGENTS=1 ;;
      --exporter)   DO_EXPORTER=1 ;;
      --monitoring) DO_MON=1 ;;
      --all)        DO_SERVICES=1; DO_AGENTS=1; DO_EXPORTER=1; DO_MON=1 ;;
      -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
      *) die "unknown argument: $a" ;;
    esac
  done
fi

load_env

stop_pidfile() {                # stop_pidfile <label> <pidfile>
  local label="$1" pidf="$2" pid
  if [ ! -f "$pidf" ]; then skip "$label" "no pid file"; return; fi
  pid="$(cat "$pidf")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    pass "$label" "stopped (pid $pid)"
  else
    skip "$label" "pid $pid not running"
  fi
  rm -f "$pidf"
}

if [ "$DO_EXPORTER" -eq 1 ]; then
  stop_pidfile "pipeline exporter" "$PROJECT_ROOT/run/exporter.pid"
fi

if [ "$DO_AGENTS" -eq 1 ]; then
  stop_pidfile "agent generator" "$PROJECT_ROOT/run/agent.pid"
  for spec in "collector:$COLLECTOR_SSH_HOST" "compactor:$COMPACTOR_SSH_HOST"; do
    IFS=: read -r role host <<< "$spec"
    out="$(remote "$host" '
      P=/opt/analytics-bench/agent/agent.pid
      if [ -f $P ] && kill -0 $(cat $P) 2>/dev/null; then
        kill -TERM $(cat $P); rm -f $P; echo stopped
      else
        rm -f $P 2>/dev/null; echo "not running"
      fi' 2>/dev/null || echo unreachable)"
    case "$out" in
      stopped) pass "agent $role" "stopped on $host" ;;
      *)       skip "agent $role" "$out on $host" ;;
    esac
  done
fi

if [ "$DO_SERVICES" -eq 1 ]; then
  warn "services" "stopping the pipeline — SIGTERM starts the collector's graceful drain"
  for spec in "collector:$COLLECTOR_SSH_HOST:bench-collector" \
              "compactor:$COMPACTOR_SSH_HOST:bench-compactor"; do
    IFS=: read -r role host unit <<< "$spec"
    if remote "$host" "sudo systemctl stop $unit" 2>/dev/null; then
      pass "$role" "$unit stopped on $host"
    else
      fail "$role" "could not stop $unit on $host"
    fi
  done
fi

if [ "$DO_MON" -eq 1 ]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    (cd "$PROJECT_ROOT" && docker compose -f docker-compose.monitoring.yml down) \
      && pass "monitoring" "stack down" || fail "monitoring" "compose down failed"
  else
    skip "monitoring" "docker unavailable"
  fi
fi

echo
summary
