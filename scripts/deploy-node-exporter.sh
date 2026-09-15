#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Install node_exporter on SERVER 2 and SERVER 3 (optional).
#
#   scripts/deploy-node-exporter.sh [--remove]
#
# OPTIONAL, and worth being clear about why. Host metrics for the report already
# come from bench/host_agent.py, which samples /proc at 1 s and writes JSONL —
# finer than any Prometheus scrape and captured per run. node_exporter adds
# nothing to the report; it exists so the Infrastructure dashboard is populated
# while watching a long soak.
#
# There is no Docker on servers 2 and 3, so this installs the static binary
# under /opt/analytics-bench/node-exporter with a systemd unit. Needs the
# security group to allow 9100 between the hosts.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

REMOVE=0
[ "${1:-}" = "--remove" ] && REMOVE=1

load_env

VERSION="1.8.2"
ARCH="linux-amd64"
TARBALL="node_exporter-${VERSION}.${ARCH}.tar.gz"
URL="https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}"
BASE=/opt/analytics-bench/node-exporter
PORT="${NODE_EXPORTER_PORT:-9100}"

UNIT=$(cat <<UNITEOF
[Unit]
Description=Prometheus node_exporter (benchmark)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SSH_USER
ExecStart=$BASE/node_exporter --web.listen-address=:$PORT \\
  --collector.filesystem.mount-points-exclude='^/(sys|proc|dev|run)(\$|/)'
Restart=on-failure
RestartSec=5s
# It is a monitoring agent on a two-vCPU box being benchmarked. Cap it so it
# cannot become part of what is being measured.
CPUQuota=10%
MemoryMax=128M

[Install]
WantedBy=multi-user.target
UNITEOF
)

for spec in "collector:$COLLECTOR_SSH_HOST" "compactor:$COMPACTOR_SSH_HOST"; do
  IFS=: read -r role host <<< "$spec"

  if [ "$REMOVE" -eq 1 ]; then
    if remote "$host" "
        sudo systemctl disable --now node-exporter 2>/dev/null || true
        sudo rm -f /etc/systemd/system/node-exporter.service
        sudo rm -rf '$BASE'
        sudo systemctl daemon-reload" 2>/dev/null; then
      pass "$role" "node_exporter removed from $host"
    else
      fail "$role" "removal failed on $host"
    fi
    continue
  fi

  info "installing node_exporter $VERSION on $role ($host)"
  if remote "$host" "
      set -e
      sudo mkdir -p '$BASE' && sudo chown $SSH_USER:$SSH_USER '$BASE'
      cd '$BASE'
      if [ ! -x node_exporter ]; then
        curl -sSL --retry 3 --max-time 120 -o '$TARBALL' '$URL'
        tar xzf '$TARBALL' --strip-components=1 'node_exporter-${VERSION}.${ARCH}/node_exporter'
        rm -f '$TARBALL'
        chmod +x node_exporter
      fi
      ./node_exporter --version 2>&1 | head -1
  " >/tmp/ne.$$ 2>&1; then
    pass "$role" "binary: $(head -1 /tmp/ne.$$ | cut -c1-60)"
  else
    fail "$role" "install failed: $(tail -2 /tmp/ne.$$ | tr '\n' ' ')"
    rm -f /tmp/ne.$$
    continue
  fi
  rm -f /tmp/ne.$$

  if printf '%s\n' "$UNIT" | remote_stdin "$host" \
      "sudo tee /etc/systemd/system/node-exporter.service >/dev/null &&
       sudo systemctl daemon-reload &&
       sudo systemctl enable --now node-exporter" >/dev/null 2>&1; then
    sleep 2
    if remote "$host" "curl -sf --max-time 4 http://127.0.0.1:$PORT/metrics | head -1" >/dev/null 2>&1; then
      pass "$role" "node-exporter.service active on :$PORT"
    else
      fail "$role" "service started but :$PORT is not serving metrics"
    fi
  else
    fail "$role" "could not install the systemd unit"
  fi

  if tcp_open "$host" "$PORT" 4; then
    pass "$role reachable" "$host:$PORT from SERVER 1"
  else
    warn "$role reachable" "$host:$PORT blocked from here — add 9100 to the security group (NETWORK.md)"
  fi
done

echo
[ "$REMOVE" -eq 1 ] || info "Prometheus already lists these targets; they will go green on the next scrape."
summary
