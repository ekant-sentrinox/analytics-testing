#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verify every network path this architecture actually needs — and confirm that
# the ones it does not need are still closed.
#
#   scripts/connectivity-check.sh
#
# Three planes, checked separately, because they have different requirements:
#
#   DATA        Generator -> Collector :4317          MUST work
#               Collector -> S3, RDS                  MUST work, from SERVER 2
#               Compactor -> S3, RDS                  MUST work, from SERVER 3
#
#   MANAGEMENT  SERVER 1 -> 2, 3 on :22               MUST work
#
#   MONITORING  no cross-VM ports at all              MUST stay closed
#               Health is checked by each host's own agent over its loopback,
#               and the central exporter reads the result over :22. A closed
#               8080/8081 is the DESIRED state and is reported as such — not as
#               a failure.
#
# Ports are not invented: each is annotated with where the number came from.
# A timeout and a refusal are reported differently, because they mean different
# things — see the legend at the end.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

load_env
PY="$(python_bin)"

# probe host port [timeout] -> "<state> <elapsed_ms>"
# Both values come back on stdout: this runs in a command substitution, so a
# variable set inside the subshell would never reach the caller.
probe() {
  local host="$1" port="$2" started ended rc state
  started=$(date +%s%N)
  # Connect and close without sending a byte. Writing into a gRPC port makes
  # the collector log an HTTP/2 preface error, i.e. the check would create the
  # very errors the run is being audited for.
  timeout "${3:-6}" bash -c "exec 3<>/dev/tcp/$host/$port && exec 3<&- && exec 3>&-" 2>/dev/null
  rc=$?
  ended=$(date +%s%N)
  case "$rc" in
    0)   state=open ;;
    124) state=timeout ;;
    *)   if getent hosts "$host" >/dev/null 2>&1; then state=refused; else state=dns; fi ;;
  esac
  printf '%s %s\n' "$state" $(( (ended - started) / 1000000 ))
}

# agent_dep <ssh-host> <s3|rds>  -> reads the dependency result the host's own
# agent measured. Reachability from THERE is the only meaningful answer: S3
# being reachable from the control node says nothing about whether the collector
# can write to it.
agent_dep() {
  remote "$1" "cat /opt/analytics-bench/agent/state/health.json 2>/dev/null" 2>/dev/null \
    | "$PY" -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('noagent'); raise SystemExit
c=(d.get('dependencies') or {}).get('$2') or {}
if not c.get('configured'): print('unconfigured')
else: print(('ok ' if c.get('ok') else 'fail ') + str(c.get('latency_ms','?')))
" 2>/dev/null || echo noagent
}

echo "connectivity — from $(hostname -s) ($(hostname -I 2>/dev/null | awk '{print $1}')), $(utc_now)"
[ "${BENCH_TRANSPORT:-direct}" != "direct" ] && \
  warn "transport" "$BENCH_TRANSPORT is active — the data path is not direct"
echo

# =============================================================================
info "DATA PLANE — must work"
echo

# Generator -> Collector OTLP gRPC. This is the pipeline. Source of the number:
# otel_collector.grpc_port in the collector's reference.conf.
read -r state ms <<< "$(probe "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT")"
case "$state" in
  open)    pass "generator -> collector" "$COLLECTOR_HOST:$COLLECTOR_GRPC_PORT OTLP gRPC — ${ms}ms" ;;
  refused) fail "generator -> collector" "$COLLECTOR_HOST:$COLLECTOR_GRPC_PORT refused — route is fine, the collector is not listening" ;;
  timeout) fail "generator -> collector" "$COLLECTOR_HOST:$COLLECTOR_GRPC_PORT dropped after ${ms}ms — security group. This is the ONE data-plane rule that must exist; see NETWORK.md" ;;
  dns)     fail "generator -> collector" "$COLLECTOR_HOST does not resolve" ;;
esac

# Collector and compactor dependencies, as measured on those hosts.
for spec in "collector:$COLLECTOR_SSH_HOST" "compactor:$COMPACTOR_SSH_HOST"; do
  IFS=: read -r role host <<< "$spec"
  for dep in s3 rds; do
    read -r st lat <<< "$(agent_dep "$host" "$dep")"
    label="$role -> $(echo "$dep" | tr '[:lower:]' '[:upper:]')"
    case "$st" in
      ok)           pass "$label" "reachable from $host — ${lat}ms (measured by that host's agent)" ;;
      fail)         fail "$label" "NOT reachable from $host — the data path is broken there" ;;
      unconfigured) skip "$label" "not configured in that host's agent" ;;
      *)            warn "$label" "no agent state on $host — run scripts/deploy-agent.sh" ;;
    esac
  done
done

# =============================================================================
echo
info "MANAGEMENT PLANE — must work"
echo
for spec in "collector:$COLLECTOR_SSH_HOST" "compactor:$COMPACTOR_SSH_HOST"; do
  IFS=: read -r role host <<< "$spec"
  read -r state ms <<< "$(probe "$host" 22)"
  if [ "$state" = open ] && remote "$host" true 2>/dev/null; then
    pass "ssh -> $role" "$host:22 — ${ms}ms, key auth working"
  elif [ "$state" = open ]; then
    fail "ssh -> $role" "$host:22 is open but key auth failed — run scripts/setup-ssh.sh"
  else
    fail "ssh -> $role" "$host:22 $state — deploy, result collection and monitoring all need this"
  fi
done

# =============================================================================
echo
info "MONITORING PLANE — must stay closed"
echo
closed_ok=1
for spec in "collector health:$COLLECTOR_SSH_HOST:$COLLECTOR_HEALTH_PORT:otel_collector.health.port" \
            "compactor health:$COMPACTOR_SSH_HOST:$COMPACTOR_HEALTH_PORT:dazzleduck_sql_compaction.health_port"; do
  IFS=: read -r label host port source <<< "$spec"
  read -r state ms <<< "$(probe "$host" "$port" 4)"
  case "$state" in
    timeout|refused)
      pass "$label closed" "$host:$port not reachable from here — correct by design ($source)" ;;
    open)
      closed_ok=0
      warn "$label OPEN" "$host:$port IS reachable from here. Not required: health is checked on the host over its loopback. Consider removing the rule." ;;
    dns)
      fail "$label" "$host does not resolve" ;;
  esac
done

# The thing that replaces those ports: prove health is genuinely being observed.
for spec in "collector:$COLLECTOR_SSH_HOST:$COLLECTOR_HEALTH_PORT" \
            "compactor:$COMPACTOR_SSH_HOST:$COMPACTOR_HEALTH_PORT"; do
  IFS=: read -r role host port <<< "$spec"
  body="$(remote "$host" "curl -sf --max-time 4 http://127.0.0.1:$port/health" 2>/dev/null || true)"
  if [ -n "$body" ]; then
    pass "$role health (on-host)" "127.0.0.1:$port answers on $host — no inbound rule needed"
  else
    fail "$role health (on-host)" "the service does not answer on its own loopback on $host"
  fi
done

# =============================================================================
echo
info "SHARED SERVICES — from here (SERVER 1)"
echo
read -r state ms <<< "$(probe "$PG_HOST" "$PG_PORT")"
[ "$state" = open ] \
  && pass "control -> RDS" "$PG_HOST:$PG_PORT — ${ms}ms (backlog polling, correctness validation)" \
  || fail "control -> RDS" "$PG_HOST:$PG_PORT $state"

if aws s3api head-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION" >/dev/null 2>&1; then
  pass "control -> S3" "s3://$S3_BUCKET reachable (object stats, correctness validation)"
else
  fail "control -> S3" "head-bucket failed for s3://$S3_BUCKET"
fi

# =============================================================================
echo
if [ "$closed_ok" -eq 1 ]; then
  info "monitoring requires no cross-VM ports: 8080/8081 are closed and health is local"
fi
cat <<'LEGEND'

  reading a failure:
    refused, fast   the route is fine and nothing is listening -> a SERVICE problem
    timeout, ~4-6s  packets are being dropped                  -> a SECURITY GROUP problem
    dns             the hostname does not resolve

LEGEND
summary
