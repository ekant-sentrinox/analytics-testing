#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# SSH port-forward workaround for the closed security group.
#
#   scripts/tunnel.sh start
#   scripts/tunnel.sh status
#   scripts/tunnel.sh stop
#
# Forwards, on SERVER 1's loopback:
#
#   127.0.0.1:4317 --ssh--> 10.16.24.204:4317   collector OTLP gRPC
#   127.0.0.1:8081 --ssh--> 10.16.24.204:8081   collector health
#   127.0.0.1:8080 --ssh--> 10.16.25.10:8080    compactor health
#
# While a tunnel is up, this writes .env.tunnel, which load_env sources AFTER
# .env so every script — generator, exporter, health checks — automatically
# talks to 127.0.0.1 instead of the real host.
#
# ---------------------------------------------------------------------------
# WHAT THIS IS AND IS NOT VALID FOR
#
#   VALID     functional testing, smoke, correctness, compaction behaviour,
#             fault injection, catalog and S3 verification — anything about
#             whether the pipeline WORKS.
#
#   NOT VALID throughput and latency. The path now carries SSH encryption,
#             an extra userspace hop, and TCP-over-TCP with its own window and
#             retransmit behaviour. A single ssh process is also single-
#             threaded, so it becomes a bottleneck of its own well before the
#             pipeline does.
#
# .env.tunnel sets BENCH_TRANSPORT=ssh-tunnel. That lands in every run's
# metadata.json and manifest.json, and the report prints a banner on top of any
# run recorded through it. The labelling is automatic on purpose — a tunnelled
# throughput number that loses its caveat is worse than no number.
#
# The real fix is three security group rules; see NETWORK.md.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

ACTION="${1:-status}"
load_env

RUN_DIR="$PROJECT_ROOT/run"
TUNNEL_ENV="$PROJECT_ROOT/.env.tunnel"
mkdir -p "$RUN_DIR"

# name:local_port:remote_host_var:remote_port
#
# ONLY 4317. The health ports are deliberately absent: health is checked by the
# host-local agent over each machine's own loopback (scripts/deploy-agent.sh),
# and the central exporter reads the resulting state file over SSH. Nothing
# needs to reach 8080 or 8081 across the network, so forwarding them would be
# pointless plumbing.
#
# 4317 is different — it is the DATA PLANE. The generator on SERVER 1 has to
# reach the collector's OTLP receiver on SERVER 2 or there is no benchmark at
# all. This forward is a stopgap until that one rule exists; see NETWORK.md.
FORWARDS="
collector-grpc:4317:COLLECTOR_HOST:4317
"

# The real hosts, before .env.tunnel rewrote them. Needed so `stop` and a
# second `start` still know where to connect.
real_host() {
  local var="$1"
  local from_base
  from_base="$(grep -E "^${var}=" "$PROJECT_ROOT/.env" | tail -1 | cut -d= -f2-)"
  echo "${from_base:-${!var}}"
}

start_one() {                   # start_one <name> <lport> <rhost> <rport>
  local name="$1" lport="$2" rhost="$3" rport="$4"
  local pidf="$RUN_DIR/tunnel-$name.pid"

  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    pass "$name" "already up (pid $(cat "$pidf"), :$lport)"
    return
  fi

  if ss -tlnH "sport = :$lport" 2>/dev/null | grep -q .; then
    fail "$name" "local port $lport is already in use by something else"
    return
  fi

  # -f background, -N no command, ExitOnForwardFailure so a bind failure is an
  # error rather than a silently useless tunnel. ServerAlive* so a dropped
  # tunnel dies instead of hanging a long soak on a dead socket.
  # shellcheck disable=SC2046
  ssh -f -N \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=3 \
      -o StrictHostKeyChecking=accept-new \
      -o BatchMode=yes \
      $(ssh_key_opt) \
      -L "127.0.0.1:${lport}:${rhost}:${rport}" \
      "${SSH_USER}@${rhost}" 2>/tmp/tunnel-$name.err

  # -f forks, so find the child by its forward spec rather than by $!.
  sleep 1
  local pid
  pid="$(pgrep -f -- "-L 127.0.0.1:${lport}:${rhost}:${rport}" | head -1)"
  if [ -n "$pid" ]; then
    echo "$pid" > "$pidf"
    if tcp_open 127.0.0.1 "$lport" 3; then
      pass "$name" "127.0.0.1:$lport -> $rhost:$rport (pid $pid)"
    else
      fail "$name" "tunnel process started but 127.0.0.1:$lport does not accept"
    fi
  else
    fail "$name" "$(head -1 /tmp/tunnel-$name.err 2>/dev/null || echo 'ssh did not start')"
  fi
  rm -f "/tmp/tunnel-$name.err"
}

stop_one() {
  local name="$1"
  local pidf="$RUN_DIR/tunnel-$name.pid"
  if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    kill "$(cat "$pidf")" 2>/dev/null || true
    pass "$name" "stopped"
  else
    skip "$name" "not running"
  fi
  rm -f "$pidf"
}

case "$ACTION" in

  start)
    info "opening SSH forwards from SERVER 1"
    echo
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      IFS=: read -r name lport hostvar rport <<< "$line"
      start_one "$name" "$lport" "$(real_host "$hostvar")" "$rport"
    done <<< "$FORWARDS"

    cat > "$TUNNEL_ENV" <<EOF
# Generated by scripts/tunnel.sh — do not edit. Removed by 'tunnel.sh stop'.
#
# Sourced by load_env AFTER .env, so these win. Every script now reaches the
# collector and compactor over the loopback forwards instead of the real hosts.
#
# THROUGHPUT AND LATENCY MEASURED THROUGH THIS ARE NOT VALID. See NETWORK.md.
# Data plane only, and only the collector: the generator connects to
# 127.0.0.1:4317 and the forward carries it to SERVER 2. The compactor is not
# in the data path from SERVER 1 at all.
#
# SSH keeps going to the real addresses — port 22 was never blocked, and
# deploy, result collection, systemctl and the agent state pull all need it.
COLLECTOR_HOST=127.0.0.1
COLLECTOR_SSH_HOST=$(real_host COLLECTOR_HOST)
COMPACTOR_SSH_HOST=$(real_host COMPACTOR_HOST)
BENCH_TRANSPORT=ssh-tunnel
# Quoted: this file is sourced by the shell, and the note contains a semicolon.
BENCH_TRANSPORT_NOTE="SSH port-forward workaround for security group sg-0ea9dd40012388877; functional results are valid, throughput and latency are not"
EOF
    chmod 0600 "$TUNNEL_ENV"

    echo
    pass "env override" ".env.tunnel written — scripts now use 127.0.0.1"
    echo
    warn "validity" "functional, correctness and compaction results: VALID"
    warn "validity" "throughput and latency: NOT VALID through a tunnel"
    echo "                               Runs are tagged BENCH_TRANSPORT=ssh-tunnel and the"
    echo "                               report banners them. The fix is three SG rules (NETWORK.md)."
    echo
    info "verify:  ./scripts/health-check.sh"
    ;;

  stop)
    info "closing SSH forwards"
    echo
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      IFS=: read -r name _ _ _ <<< "$line"
      stop_one "$name"
    done <<< "$FORWARDS"
    rm -f "$TUNNEL_ENV"
    pass "env override" ".env.tunnel removed — scripts use the real hosts again"
    ;;

  status)
    if [ -f "$TUNNEL_ENV" ]; then
      warn "mode" "TUNNELLED — throughput and latency from runs started now are NOT VALID"
    else
      info "mode: direct (no tunnel)"
    fi
    echo
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      IFS=: read -r name lport hostvar rport <<< "$line"
      pidf="$RUN_DIR/tunnel-$name.pid"
      rhost="$(real_host "$hostvar")"
      if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
        if tcp_open 127.0.0.1 "$lport" 3; then
          pass "$name" "127.0.0.1:$lport -> $rhost:$rport (pid $(cat "$pidf"))"
        else
          fail "$name" "pid alive but 127.0.0.1:$lport does not accept"
        fi
      else
        skip "$name" "down (would be 127.0.0.1:$lport -> $rhost:$rport)"
      fi
    done <<< "$FORWARDS"
    ;;

  *)
    sed -n '2,40p' "$0"
    exit 1
    ;;
esac

echo
summary
