#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install the host-local monitoring agent on all three servers.
#
#   scripts/deploy-agent.sh [--restart-only] [--remove]
#
# Each agent runs as a systemd service beside the thing it observes:
#
#   SERVER 1  generator   system metrics + S3/RDS reachability from that host
#   SERVER 2  collector   ... plus  curl http://127.0.0.1:8081/health
#   SERVER 3  compactor   ... plus  curl http://127.0.0.1:8080/health
#
# The health check runs on the host, over the loopback. That is the whole
# reason 8080 and 8081 can stay CLOSED in the security group: nothing needs to
# reach them across the network. The central exporter reads each agent's state
# file over SSH (port 22, already open), so monitoring adds no attack surface.
#
# systemd rather than nohup: restart on failure, resource caps, and a run that
# survives your SSH session closing.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

MODE=install
for a in "$@"; do
  case "$a" in
    --restart-only) MODE=restart ;;
    --remove)       MODE=remove ;;
    -h|--help)      sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $a" ;;
  esac
done

load_env

export AGENT_DIR=/opt/analytics-bench/agent
export AGENT_INTERVAL="${AGENT_INTERVAL:-1}"
export AGENT_CHECK_INTERVAL="${AGENT_CHECK_INTERVAL:-15}"
UNIT=bench-agent.service

# role : ssh-host : extra args for that role
ROLES="
generator:${GENERATOR_HOST}:
collector:${COLLECTOR_SSH_HOST}:--health-url http://127.0.0.1:${COLLECTOR_HEALTH_PORT}/health --systemd-unit bench-collector --process-pattern otel.collector.Main --jvm
compactor:${COMPACTOR_SSH_HOST}:--health-url http://127.0.0.1:${COMPACTOR_HEALTH_PORT}/health --systemd-unit bench-compactor --process-pattern sql.compaction.Main --jvm
"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

is_local() { [ "$1" = "$GENERATOR_HOST" ] || [ "$1" = "127.0.0.1" ] || [ "$1" = "localhost" ]; }

run_on() {                      # run_on <host> <script...>
  if is_local "$1"; then shift; bash -c "$*"; else local h="$1"; shift; remote "$h" "$*"; fi
}

put_on() {                      # put_on <host> <src> <dst>
  if is_local "$1"; then install -m "${4:-0644}" "$2" "$3"; else copy_to "$1" "$2" "$3"; fi
}

while IFS= read -r line; do
  [ -z "$line" ] && continue
  role="${line%%:*}"; rest="${line#*:}"
  host="${rest%%:*}"; extra="${rest#*:}"
  [ -n "$host" ] || { warn "$role" "no host configured, skipping"; continue; }

  echo
  info "$role  ($host)"

  if [ "$MODE" = remove ]; then
    run_on "$host" "sudo systemctl disable --now $UNIT 2>/dev/null || true
                    sudo rm -f /etc/systemd/system/$UNIT
                    sudo systemctl daemon-reload" >/dev/null 2>&1 \
      && pass "$role" "agent removed" || fail "$role" "removal failed"
    continue
  fi

  if [ "$MODE" = restart ]; then
    run_on "$host" "sudo systemctl restart $UNIT" >/dev/null 2>&1 \
      && pass "$role" "restarted" || fail "$role" "restart failed"
    continue
  fi

  export ROLE="$role"
  export AGENT_EXTRA_ARGS="$extra"
  render_template "$PROJECT_ROOT/agent/bench-agent.service.tmpl" "$STAGE/$UNIT.$role"

  run_on "$host" "sudo mkdir -p $AGENT_DIR/{bin,logs,state} &&
                  sudo chown -R $SSH_USER:$SSH_USER $AGENT_DIR" >/dev/null 2>&1

  put_on "$host" "$PROJECT_ROOT/bench/host_agent.py" "$AGENT_DIR/bin/host_agent.py"
  put_on "$host" "$STAGE/$UNIT.$role" "/tmp/$UNIT"

  # `enable --now` starts a stopped unit but does NOT restart a running one, so
  # on a redeploy the old process would keep serving stale code. Always restart.
  if run_on "$host" "chmod 0755 $AGENT_DIR/bin/host_agent.py
       sudo install -m 0644 -o root -g root /tmp/$UNIT /etc/systemd/system/$UNIT
       rm -f /tmp/$UNIT
       # Clear the previous snapshot, or the wait below is satisfied instantly
       # by a stale file and the summary reports the OLD agent's results.
       rm -f $AGENT_DIR/state/health.json
       sudo systemctl daemon-reload
       sudo systemctl enable $UNIT
       sudo systemctl restart $UNIT" >/dev/null 2>&1
  then
    pass "$role" "$UNIT installed and (re)started"
  else
    fail "$role" "install failed"
    run_on "$host" "sudo systemctl status $UNIT --no-pager -l | head -20" || true
    continue
  fi

  # The agent discards its first sample (deltas need two) and the dependency
  # checks run on their own slower clock, so wait for a real snapshot rather
  # than reading a file that has not been written yet.
  for _ in $(seq 1 15); do
    run_on "$host" "test -s $AGENT_DIR/state/health.json" >/dev/null 2>&1 && break
    sleep 1
  done
  state="$(run_on "$host" "cat $AGENT_DIR/state/health.json 2>/dev/null" || true)"
  if [ -n "$state" ]; then
    echo "$state" | "$(python_bin)" -c '
import json, sys
d = json.load(sys.stdin)
svc = d.get("service") or {}
dep = d.get("dependencies") or {}
h = svc.get("health") or {}
u = svc.get("unit") or {}
bits = []
if h.get("configured"):
    bits.append("health=" + ("OK" if h.get("ok") else "FAIL"))
if u.get("configured"):
    bits.append("unit=" + str(u.get("active_state")) + " restarts=" + str(u.get("restarts")))
for name in ("s3", "rds"):
    c = dep.get(name) or {}
    if c.get("configured"):
        bits.append(name + "=" + ("OK" if c.get("ok") else "FAIL"))
bits.append("cpu=%.1f%%" % (d.get("system", {}).get("cpu_percent") or 0))
print("      " + "  ".join(bits))'
  else
    warn "$role" "no state/health.json yet — check $AGENT_DIR/logs/agent.out"
  fi
done <<< "$ROLES"

# --- central exporter on SERVER 1 --------------------------------------------
# Also a systemd service rather than a nohup'd job: a 24-hour soak must not
# depend on an SSH session or a laptop staying awake.
echo
info "central exporter (SERVER 1)"

EXPORTER_UNIT=bench-exporter.service
if [ "$MODE" = remove ]; then
  sudo systemctl disable --now "$EXPORTER_UNIT" >/dev/null 2>&1 || true
  sudo rm -f "/etc/systemd/system/$EXPORTER_UNIT"
  sudo systemctl daemon-reload
  rm -f "$PROJECT_ROOT/run/exporter-run.sh"
  pass "exporter" "removed"
elif [ "$MODE" = restart ]; then
  sudo systemctl restart "$EXPORTER_UNIT" >/dev/null 2>&1 \
    && pass "exporter" "restarted" || fail "exporter" "restart failed"
else
  export PROJECT_DIR="$PROJECT_ROOT"
  export HOME_DIR="$HOME"
  export SSH_KEY="${SSH_KEY/#\~/$HOME}"
  render_template "$PROJECT_ROOT/monitoring/exporter-run.sh.tmpl" "$PROJECT_ROOT/run/exporter-run.sh"
  chmod 0755 "$PROJECT_ROOT/run/exporter-run.sh"
  render_template "$PROJECT_ROOT/monitoring/bench-exporter.service.tmpl" "/tmp/$EXPORTER_UNIT"

  # A previously nohup'd exporter would hold the port and confuse systemd.
  if [ -f "$PROJECT_ROOT/run/exporter.pid" ] && kill -0 "$(cat "$PROJECT_ROOT/run/exporter.pid")" 2>/dev/null; then
    kill "$(cat "$PROJECT_ROOT/run/exporter.pid")" 2>/dev/null || true
    rm -f "$PROJECT_ROOT/run/exporter.pid"
    warn "exporter" "stopped a previously backgrounded instance"
  fi

  if sudo install -m 0644 -o root -g root "/tmp/$EXPORTER_UNIT" "/etc/systemd/system/$EXPORTER_UNIT" \
     && rm -f "/tmp/$EXPORTER_UNIT" \
     && sudo systemctl daemon-reload \
     && sudo systemctl enable "$EXPORTER_UNIT" >/dev/null 2>&1 \
     && sudo systemctl restart "$EXPORTER_UNIT"
  then
    for _ in $(seq 1 20); do
      curl -sf --max-time 3 "http://127.0.0.1:${PIPELINE_EXPORTER_PORT:-9101}/metrics" >/dev/null 2>&1 && break
      sleep 1
    done
    if curl -sf --max-time 3 "http://127.0.0.1:${PIPELINE_EXPORTER_PORT:-9101}/metrics" >/dev/null 2>&1; then
      pass "exporter" "$EXPORTER_UNIT serving on :${PIPELINE_EXPORTER_PORT:-9101}"
    else
      fail "exporter" "started but not serving metrics"
      sudo systemctl status "$EXPORTER_UNIT" --no-pager -l | head -20 || true
    fi
  else
    fail "exporter" "install failed"
  fi
fi

echo
if [ "$MODE" = install ]; then
  info "health is checked on each host over its own loopback; the exporter pulls the"
  info "resulting state over SSH. No inbound rule is needed for 8080/8081."
  info "everything runs under systemd — safe to disconnect."
  info "verify:  ./scripts/health-check.sh"
fi
summary
