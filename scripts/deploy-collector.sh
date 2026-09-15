#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Render and install the otel-collector onto SERVER 2.
#
#   scripts/deploy-collector.sh [--no-build] [--no-restart]
#
# Installs into /opt/analytics-bench/collector (conf/ bin/ logs/ run/) and
# registers a systemd unit. The source checkout on SERVER 2 is treated as
# read-only build input and is never modified beyond `mvn package`.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

BUILD=1
RESTART=1
for a in "$@"; do
  case "$a" in
    --no-build)   BUILD=0 ;;
    --no-restart) RESTART=0 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown argument: $a" ;;
  esac
done

load_env
CFG="$PROJECT_ROOT/config/collector.yaml"
PY="$(python_bin)"
c() { "$PY" "$PROJECT_ROOT/scripts/lib/cfg.py" get "$CFG" "$1" ${2+--default "$2"}; }

# --- values from config/collector.yaml ---------------------------------------
export BASE_DIR;      BASE_DIR="$(c collector.install.base_dir)"
export REPO_DIR;      REPO_DIR="$(c collector.install.repo_dir)"
export MODULE;        MODULE="$(c collector.install.module)"
export MAIN_CLASS;    MAIN_CLASS="$(c collector.install.main_class)"
export SYSTEMD_UNIT;  SYSTEMD_UNIT="$(c collector.install.systemd_unit)"
export JVM_MAX_HEAP;  JVM_MAX_HEAP="$(c collector.jvm.max_heap)"
export JVM_OPTS;      JVM_OPTS="$(c collector.jvm.opts)"
export MIN_BUCKET_SIZE;  MIN_BUCKET_SIZE="$(c collector.ingestion.min_bucket_size)"
export MAX_DELAY_MS;     MAX_DELAY_MS="$(c collector.ingestion.max_delay_ms)"
export DUCKDB_THREADS;      DUCKDB_THREADS="$(c collector.duckdb.threads)"
export DUCKDB_MEMORY_LIMIT; DUCKDB_MEMORY_LIMIT="$(c collector.duckdb.memory_limit)"
export DUCKDB_TEMP_DIR;     DUCKDB_TEMP_DIR="$(c collector.duckdb.temp_directory)"

# Basic-auth password for the local `bench` user. Only used by the /v1 login
# path; the generator signs its own JWTs and never sends Basic credentials.
export COLLECTOR_BASIC_PASSWORD="${COLLECTOR_BASIC_PASSWORD:-bench}"

[ -n "${OTEL_JWT_SECRET_B64:-}" ] || die "OTEL_JWT_SECRET_B64 is empty — run scripts/gen-secret.sh first"

HOST="$COLLECTOR_SSH_HOST"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

info "Rendering collector config for $HOST"
render_template "$PROJECT_ROOT/collector/config/application.conf.tmpl"        "$STAGE/application.conf"
render_template "$PROJECT_ROOT/collector/config/run.sh.tmpl"                  "$STAGE/run.sh"
render_template "$PROJECT_ROOT/collector/config/bench-collector.service.tmpl" "$STAGE/$SYSTEMD_UNIT"
chmod 0755 "$STAGE/run.sh"

# Fail before touching the server if the rendered HOCON is not parseable.
if command -v java >/dev/null 2>&1 && [ -f "$PROJECT_ROOT/scripts/lib/hocon-check.jar" ]; then
  java -jar "$PROJECT_ROOT/scripts/lib/hocon-check.jar" "$STAGE/application.conf" \
    || die "rendered application.conf is not valid HOCON"
fi

# --- build -------------------------------------------------------------------
if [ "$BUILD" -eq 1 ]; then
  info "Building $MODULE on $HOST (this is a no-op if nothing changed)"
  remote "$HOST" "
    set -e
    export JAVA_HOME=/usr/lib/jvm/java-21-amazon-corretto.x86_64
    export PATH=\$JAVA_HOME/bin:\$PATH
    cd '$REPO_DIR'
    ./mvnw -B -ntp -q -DskipTests install -pl '$MODULE' -am
    ./mvnw -B -ntp -q dependency:copy-dependencies -DoutputDirectory=target/lib -pl '$MODULE'
  " || die "build failed on $HOST — see the maven output above"
  pass "build" "$MODULE on $HOST"
fi

# --- install -----------------------------------------------------------------
info "Installing into $BASE_DIR on $HOST"
remote "$HOST" "
  set -e
  sudo mkdir -p '$BASE_DIR'/{conf,bin,logs,run}
  sudo chown -R $SSH_USER:$SSH_USER '$BASE_DIR'
  mkdir -p '$DUCKDB_TEMP_DIR'
"

copy_to "$HOST" "$STAGE/application.conf" "$BASE_DIR/conf/application.conf"
copy_to "$HOST" "$STAGE/run.sh"           "$BASE_DIR/bin/run.sh"
copy_to "$HOST" "$STAGE/$SYSTEMD_UNIT"    "/tmp/$SYSTEMD_UNIT"
# conf/ is first on the classpath, so this shadows the module's DEBUG default.
copy_to "$HOST" "$PROJECT_ROOT/collector/config/logback.xml" "$BASE_DIR/conf/logback.xml"

remote "$HOST" "
  set -e
  chmod 0600 '$BASE_DIR/conf/application.conf'
  chmod 0755 '$BASE_DIR/bin/run.sh'
  sudo install -m 0644 -o root -g root '/tmp/$SYSTEMD_UNIT' '/etc/systemd/system/$SYSTEMD_UNIT'
  rm -f '/tmp/$SYSTEMD_UNIT'
  sudo systemctl daemon-reload
  sudo systemctl enable '$SYSTEMD_UNIT' >/dev/null
"
pass "install" "$BASE_DIR + /etc/systemd/system/$SYSTEMD_UNIT"

if [ "$RESTART" -eq 1 ]; then
  info "Restarting $SYSTEMD_UNIT"
  remote "$HOST" "sudo systemctl restart '$SYSTEMD_UNIT'"

  # Probe on the remote loopback, not across the network. Whether the port is
  # reachable from here is a security-group question and belongs to
  # connectivity-check.sh; conflating the two turns "the SG is closed" into
  # "the deploy failed", which sends you debugging the wrong thing.
  body=""
  for _ in $(seq 1 60); do
    body="$(remote "$HOST" "curl -sf --max-time 3 http://127.0.0.1:$COLLECTOR_HEALTH_PORT/health" 2>/dev/null || true)"
    [ -n "$body" ] && break
    sleep 2
  done

  if [ -n "$body" ]; then
    pass "collector health (local)" "$(echo "$body" | tr -d '\n ' )"
  else
    fail "collector health (local)" "no /health response on $HOST after 120s"
    remote "$HOST" "sudo systemctl status '$SYSTEMD_UNIT' --no-pager -l | head -20; tail -n 40 '$BASE_DIR/logs/collector.log'" || true
  fi

  if tcp_open "$HOST" "$COLLECTOR_GRPC_PORT" 3; then
    pass "collector grpc (remote)" "$HOST:$COLLECTOR_GRPC_PORT reachable"
  else
    warn "collector grpc (remote)" \
      "$HOST:$COLLECTOR_GRPC_PORT not reachable from here — the service is up, so this is the security group. See NETWORK.md"
  fi
fi

summary
