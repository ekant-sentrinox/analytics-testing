#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Bring the environment up.
#
#   scripts/start.sh [--services] [--agents] [--exporter] [--monitoring] [--all]
#
# With no flags: --services --agents --exporter (everything needed to run a test).
# --monitoring additionally starts Prometheus + Grafana in Docker on SERVER 1.
#
# The generator is NOT started here — it is started per test by run-test.sh.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

DO_SERVICES=0; DO_AGENTS=0; DO_EXPORTER=0; DO_MON=0
if [ $# -eq 0 ]; then
  DO_SERVICES=1; DO_AGENTS=1; DO_EXPORTER=1
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
mkdir -p "$PROJECT_ROOT/run" "$PROJECT_ROOT/logs"

# --- remote services ------------------------------------------------------------
if [ "$DO_SERVICES" -eq 1 ]; then
  info "starting pipeline services"
  for spec in "collector:$COLLECTOR_SSH_HOST:bench-collector:$COLLECTOR_HEALTH_PORT" \
              "compactor:$COMPACTOR_SSH_HOST:bench-compactor:$COMPACTOR_HEALTH_PORT"; do
    IFS=: read -r name host unit port <<< "$spec"
    if ! remote "$host" "sudo systemctl start $unit" 2>/dev/null; then
      fail "$name" "systemctl start $unit failed on $host"
      continue
    fi
    body=""
    for _ in $(seq 1 30); do
      body="$(remote "$host" "curl -sf --max-time 3 http://127.0.0.1:$port/health" 2>/dev/null || true)"
      [ -n "$body" ] && break
      sleep 2
    done
    [ -n "$body" ] && pass "$name" "$unit up on $host" \
                   || fail "$name" "$unit started but /health silent after 60s"
  done
fi

# --- host metric agents ----------------------------------------------------------
if [ "$DO_AGENTS" -eq 1 ]; then
  info "starting host metric agents"
  AGENT_DIR=/opt/analytics-bench/agent
  for spec in "generator:127.0.0.1::" \
              "collector:$COLLECTOR_SSH_HOST:otel.collector.Main:--jvm" \
              "compactor:$COMPACTOR_SSH_HOST:sql.compaction.Main:--jvm"; do
    IFS=: read -r role host pattern extra <<< "$spec"

    if [ "$role" = "generator" ]; then
      # Local: run inside the project so results stay with the run.
      if [ -f "$PROJECT_ROOT/run/agent.pid" ] && kill -0 "$(cat "$PROJECT_ROOT/run/agent.pid")" 2>/dev/null; then
        pass "agent generator" "already running (pid $(cat "$PROJECT_ROOT/run/agent.pid"))"
      else
        nohup "$(python_bin)" "$PROJECT_ROOT/bench/host_agent.py" \
          --output "$PROJECT_ROOT/logs/host-generator.jsonl" --role generator \
          --interval 1 --watch-path / --watch-path "$PROJECT_ROOT" \
          >"$PROJECT_ROOT/logs/agent-generator.out" 2>&1 &
        echo $! > "$PROJECT_ROOT/run/agent.pid"
        sleep 1
        kill -0 "$(cat "$PROJECT_ROOT/run/agent.pid")" 2>/dev/null \
          && pass "agent generator" "pid $(cat "$PROJECT_ROOT/run/agent.pid")" \
          || fail "agent generator" "died immediately — see logs/agent-generator.out"
      fi
      continue
    fi

    # Remote: the agent reads /proc only, so stock python3 is enough — no venv
    # needs to exist on servers 2 and 3.
    copy_to "$host" "$PROJECT_ROOT/bench/host_agent.py" "/tmp/host_agent.py" 2>/dev/null || true
    if remote "$host" "
        sudo mkdir -p $AGENT_DIR/{bin,logs} && sudo chown -R $SSH_USER:$SSH_USER $AGENT_DIR
        install -m 0755 /tmp/host_agent.py $AGENT_DIR/bin/host_agent.py
        if [ -f $AGENT_DIR/agent.pid ] && kill -0 \$(cat $AGENT_DIR/agent.pid) 2>/dev/null; then
          echo already
        else
          nohup python3 $AGENT_DIR/bin/host_agent.py \
            --output $AGENT_DIR/logs/host-$role.jsonl --role $role --interval 1 \
            --process-pattern '$pattern' $extra \
            --watch-path / --watch-path /var/tmp \
            >$AGENT_DIR/logs/agent.out 2>&1 &
          echo \$! > $AGENT_DIR/agent.pid
          sleep 1
          kill -0 \$(cat $AGENT_DIR/agent.pid) 2>/dev/null && echo started || echo died
        fi" >/tmp/agent.$$ 2>&1
    then
      case "$(tail -1 /tmp/agent.$$)" in
        already) pass "agent $role" "already running on $host" ;;
        started) pass "agent $role" "started on $host ($AGENT_DIR/logs)" ;;
        *)       fail "agent $role" "failed on $host: $(tail -2 /tmp/agent.$$ | tr '\n' ' ')" ;;
      esac
    else
      fail "agent $role" "could not reach $host"
    fi
    rm -f /tmp/agent.$$
  done
fi

# --- pipeline exporter --------------------------------------------------------------
if [ "$DO_EXPORTER" -eq 1 ]; then
  info "starting pipeline exporter"
  PIDF="$PROJECT_ROOT/run/exporter.pid"
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    pass "pipeline exporter" "already running (pid $(cat "$PIDF"))"
  else
    PG_PASSWORD="$PG_PASSWORD" nohup "$(python_bin)" \
      "$PROJECT_ROOT/monitoring/exporters/pipeline_exporter.py" \
      --jsonl "$PROJECT_ROOT/logs/pipeline.jsonl" \
      >"$PROJECT_ROOT/logs/exporter.out" 2>&1 &
    echo $! > "$PIDF"
    sleep 2
    if kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      pass "pipeline exporter" "pid $(cat "$PIDF") on :${PIPELINE_EXPORTER_PORT:-9101}"
    else
      fail "pipeline exporter" "died immediately — see logs/exporter.out"
    fi
  fi
fi

# --- monitoring stack -----------------------------------------------------------------
if [ "$DO_MON" -eq 1 ]; then
  info "starting the monitoring stack"
  if ! command -v docker >/dev/null 2>&1; then
    fail "monitoring" "docker not installed on this host"
  elif ! docker info >/dev/null 2>&1; then
    fail "monitoring" "docker daemon not reachable as $(id -un) — try 'newgrp docker' or re-login"
  else
    (cd "$PROJECT_ROOT" && docker compose -f docker-compose.monitoring.yml up -d) \
      && pass "monitoring" "prometheus :9090, grafana :3000 (admin/admin)" \
      || fail "monitoring" "docker compose up failed"
  fi
fi

echo
summary
