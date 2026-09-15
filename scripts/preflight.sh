#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run before every serious test.
#
#   scripts/preflight.sh [--quick]
#
# Exits non-zero if any MANDATORY check fails. Optional checks (Docker, the
# monitoring stack) warn instead — a throughput run does not need Grafana.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

load_env
PY="$(python_bin)"

echo "analytics-distributed-test preflight — $(utc_now)"
echo

# --- host ------------------------------------------------------------------------
info "host"
. /etc/os-release 2>/dev/null || true
pass "os" "${PRETTY_NAME:-unknown} $(uname -r)"

cores="$(nproc)"
mem_gb="$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo)"
pass "cpu / memory" "${cores} logical cores, ${mem_gb} GiB"
[ "$cores" -lt 2 ] && warn "cpu" "fewer than 2 cores — the generator will be the bottleneck"

free_gb="$(df -BG --output=avail "$PROJECT_ROOT" | tail -1 | tr -dc '0-9')"
if [ "${free_gb:-0}" -ge 2 ]; then
  pass "disk space" "${free_gb} GiB free on $(df --output=target "$PROJECT_ROOT" | tail -1)"
else
  fail "disk space" "${free_gb} GiB free — results and logs need headroom"
fi

# /tmp on Amazon Linux 2023 is tmpfs. Anything that spills there consumes RAM.
if findmnt -no FSTYPE /tmp 2>/dev/null | grep -q tmpfs; then
  warn "/tmp" "tmpfs (RAM-backed) — DuckDB temp_directory must not point here; config/*.yaml uses /var/tmp"
fi

if [ -z "$(swapon --show 2>/dev/null)" ]; then
  warn "swap" "none configured — memory pressure becomes an OOM kill, not a slowdown"
fi

# --- runtimes ----------------------------------------------------------------------
echo; info "runtimes"
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  pass "python venv" "$("$PROJECT_ROOT/.venv/bin/python" --version 2>&1)"
else
  fail "python venv" "missing — run scripts/setup.sh"
fi

for mod in grpc jwt yaml duckdb psycopg boto3 prometheus_client; do
  if "$PY" -c "import $mod" 2>/dev/null; then
    :
  else
    fail "python module" "$mod not importable — run scripts/setup.sh"
  fi
done
"$PY" -c "import grpc, duckdb" 2>/dev/null && \
  pass "python modules" "$("$PY" -c 'import grpc,duckdb;print("grpc %s, duckdb %s"%(grpc.__version__,duckdb.__version__))')"

# DuckDB version parity matters: DuckLake catalog metadata is version dependent
# and the servers write with the 1.5.4.0 JDBC driver.
want="$("$PY" "$PROJECT_ROOT/scripts/lib/cfg.py" get "$PROJECT_ROOT/config/duckdb.yaml" duckdb.expected_cli_version)"
have="$("$PY" -c 'import duckdb;print(duckdb.__version__)' 2>/dev/null || echo none)"
if [ "$have" = "$want" ]; then
  pass "duckdb version" "$have matches the server driver"
else
  warn "duckdb version" "python duckdb $have vs expected $want — engine comparisons across versions are not valid"
fi

command -v aws  >/dev/null 2>&1 && pass "aws cli" "$(aws --version 2>&1 | cut -d' ' -f1)" \
                                || fail "aws cli" "not installed"
command -v ssh  >/dev/null 2>&1 && pass "ssh" "present" || fail "ssh" "not installed"
command -v curl >/dev/null 2>&1 && pass "curl" "present" || fail "curl" "not installed"

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    pass "docker" "$(docker --version | cut -d, -f1) — daemon reachable"
    docker compose version >/dev/null 2>&1 \
      && pass "docker compose" "$(docker compose version --short 2>/dev/null)" \
      || warn "docker compose" "plugin missing — monitoring stack unavailable"
  else
    warn "docker" "installed but the daemon is not reachable as $(id -un) (newgrp docker, or use sudo)"
  fi
else
  warn "docker" "not installed — optional; only the monitoring stack needs it"
fi

# --- configuration ------------------------------------------------------------------
echo; info "configuration"
for f in .env config/generator.yaml config/collector.yaml config/compactor.yaml \
         config/postgres.yaml config/s3.yaml config/duckdb.yaml; do
  [ -f "$PROJECT_ROOT/$f" ] && pass "file" "$f" || fail "file" "$f missing"
done

for v in COLLECTOR_HOST COMPACTOR_HOST COLLECTOR_GRPC_PORT COLLECTOR_HEALTH_PORT \
         COMPACTOR_HEALTH_PORT PG_HOST PG_DATABASE PG_USER S3_BUCKET S3_PREFIX \
         AWS_REGION DUCKLAKE_CATALOG OTEL_JWT_SECRET_B64 OTEL_INGESTION_QUEUE; do
  if [ -n "${!v:-}" ]; then :; else fail "env" "$v is unset or empty in .env"; fi
done
pass "env" "required variables present"

if [ -n "${OTEL_JWT_SECRET_B64:-}" ]; then
  bytes="$(printf '%s' "$OTEL_JWT_SECRET_B64" | base64 -d 2>/dev/null | wc -c)"
  if [ "${bytes:-0}" -ge 64 ]; then
    pass "jwt secret" "${bytes} bytes (HS512)"
  elif [ "${bytes:-0}" -ge 32 ]; then
    warn "jwt secret" "${bytes} bytes — HS256 only; regenerate with scripts/gen-secret.sh"
  else
    fail "jwt secret" "too short (${bytes} bytes)"
  fi
fi

# Generator config must parse and validate, with the real profile machinery.
if out="$("$PY" -m generator.src.main --config config/generator.yaml \
          --profile "${DEFAULT_PROFILE:-smoke}" --dry-run --log-level ERROR 2>&1)"; then
  pass "generator config" "validates, channel to $COLLECTOR_HOST:$COLLECTOR_GRPC_PORT ready"
else
  # A dry run also opens the gRPC channel, so this fails when the SG is closed.
  if echo "$out" | grep -q "never became ready"; then
    fail "generator config" "config OK but the gRPC channel to $COLLECTOR_HOST:$COLLECTOR_GRPC_PORT did not open"
  else
    fail "generator config" "$(echo "$out" | tail -1)"
  fi
fi

[ "$QUICK" -eq 1 ] && { echo; summary; exit $?; }

# --- infrastructure ----------------------------------------------------------------
echo; info "connectivity"
"$PROJECT_ROOT/scripts/connectivity-check.sh" >/tmp/pf-conn.$$ 2>&1 || true
if grep -q '^FAIL' /tmp/pf-conn.$$; then
  grep '^FAIL' /tmp/pf-conn.$$ | while read -r l; do echo "  $l"; done
  fail "connectivity" "$(grep -c '^FAIL' /tmp/pf-conn.$$) path(s) failing — see scripts/connectivity-check.sh"
else
  pass "connectivity" "all paths open"
fi
rm -f /tmp/pf-conn.$$

echo; info "s3"
"$PROJECT_ROOT/scripts/check-s3.sh" >/tmp/pf-s3.$$ 2>&1 && s3rc=0 || s3rc=1
if [ "$s3rc" -eq 0 ]; then
  pass "s3 access" "identity, list, read, write, duckdb all OK"
else
  grep -E '^(FAIL|WARN)' /tmp/pf-s3.$$ | while read -r l; do echo "  $l"; done
  fail "s3 access" "see scripts/check-s3.sh"
fi
rm -f /tmp/pf-s3.$$

echo; info "catalog"
"$PROJECT_ROOT/scripts/check-postgres.sh" >/tmp/pf-pg.$$ 2>&1 && pgrc=0 || pgrc=1
if [ "$pgrc" -eq 0 ]; then
  grep '^PASS  live files' /tmp/pf-pg.$$ | sed 's/^/  /' || true
  pass "catalog" "reachable, ducklake metadata present, tables registered"
else
  grep '^FAIL' /tmp/pf-pg.$$ | while read -r l; do echo "  $l"; done
  fail "catalog" "see scripts/check-postgres.sh"
fi
rm -f /tmp/pf-pg.$$

echo; info "services"
for spec in "collector:$COLLECTOR_SSH_HOST:$COLLECTOR_HEALTH_PORT:bench-collector" \
            "compactor:$COMPACTOR_SSH_HOST:$COMPACTOR_HEALTH_PORT:bench-compactor"; do
  IFS=: read -r name host port unit <<< "$spec"
  state="$(remote "$host" "systemctl is-active $unit" 2>/dev/null || echo unreachable)"
  if [ "$state" = "active" ]; then
    body="$(remote "$host" "curl -sf --max-time 3 http://127.0.0.1:$port/health" 2>/dev/null || true)"
    [ -n "$body" ] && pass "$name" "$unit active, /health responding" \
                   || fail "$name" "$unit active but /health is silent on $host"
  else
    fail "$name" "$unit is $state on $host — scripts/start.sh"
  fi
done

echo; info "monitoring"
if curl -sf --max-time 3 "http://127.0.0.1:${PIPELINE_EXPORTER_PORT:-9101}/metrics" >/dev/null 2>&1; then
  pass "pipeline exporter" "scraping on :${PIPELINE_EXPORTER_PORT:-9101}"
else
  warn "pipeline exporter" "not running — start with scripts/start.sh (optional for a single test)"
fi
if curl -sf --max-time 3 http://127.0.0.1:9090/-/healthy >/dev/null 2>&1; then
  pass "prometheus" "healthy on :9090"
else
  warn "prometheus" "not running — docker compose -f docker-compose.monitoring.yml up -d (optional)"
fi

echo
summary
