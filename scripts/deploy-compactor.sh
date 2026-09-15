#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Render and install the DuckLake compactor onto SERVER 3.
#
#   scripts/deploy-compactor.sh [--no-build] [--no-restart]
#
# Installs into /opt/analytics-bench/compactor (conf/ bin/ logs/ run/) and
# registers a systemd unit.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

BUILD=1
RESTART=1
for a in "$@"; do
  case "$a" in
    --no-build)   BUILD=0 ;;
    --no-restart) RESTART=0 ;;
    -h|--help)    sed -n '2,10p' "$0"; exit 0 ;;
    *) die "unknown argument: $a" ;;
  esac
done

load_env
CFG="$PROJECT_ROOT/config/compactor.yaml"
PY="$(python_bin)"
c() { "$PY" "$PROJECT_ROOT/scripts/lib/cfg.py" get "$CFG" "$1" ${2+--default "$2"}; }

export BASE_DIR;     BASE_DIR="$(c compactor.install.base_dir)"
export REPO_DIR;     REPO_DIR="$(c compactor.install.repo_dir)"
export MODULE;       MODULE="$(c compactor.install.module)"
export MAIN_CLASS;   MAIN_CLASS="$(c compactor.install.main_class)"
export SYSTEMD_UNIT; SYSTEMD_UNIT="$(c compactor.install.systemd_unit)"
export JVM_MAX_HEAP; JVM_MAX_HEAP="$(c compactor.jvm.max_heap)"
export JVM_OPTS;     JVM_OPTS="$(c compactor.jvm.opts)"
export MINOR_COMPACTION_FREQUENCY; MINOR_COMPACTION_FREQUENCY="$(c compactor.schedule.minor_compaction_frequency)"
export MAJOR_COMPACTION_FREQUENCY; MAJOR_COMPACTION_FREQUENCY="$(c compactor.schedule.major_compaction_frequency)"
export HOUSEKEEPING_FREQUENCY;     HOUSEKEEPING_FREQUENCY="$(c compactor.schedule.housekeeping_frequency)"
export SNAPSHOT_RETENTION;         SNAPSHOT_RETENTION="$(c compactor.schedule.snapshot_retention)"
export MINOR_COMPACTION_MAX_SIZE;  MINOR_COMPACTION_MAX_SIZE="$(c compactor.sizes.minor_compaction_max_size)"
export MAJOR_COMPACTION_MAX_SIZE;  MAJOR_COMPACTION_MAX_SIZE="$(c compactor.sizes.major_compaction_max_size)"
export DUCKDB_THREADS;      DUCKDB_THREADS="$(c compactor.duckdb.threads)"
export DUCKDB_MEMORY_LIMIT; DUCKDB_MEMORY_LIMIT="$(c compactor.duckdb.memory_limit)"
export DUCKDB_TEMP_DIR;     DUCKDB_TEMP_DIR="$(c compactor.duckdb.temp_directory)"

HOST="$COMPACTOR_SSH_HOST"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

info "Rendering compactor config for $HOST"
render_template "$PROJECT_ROOT/compactor/config/application.conf.tmpl"        "$STAGE/application.conf"
render_template "$PROJECT_ROOT/compactor/config/run.sh.tmpl"                  "$STAGE/run.sh"
render_template "$PROJECT_ROOT/compactor/config/bench-compactor.service.tmpl" "$STAGE/$SYSTEMD_UNIT"
chmod 0755 "$STAGE/run.sh"

if [ "$BUILD" -eq 1 ]; then
  info "Building $MODULE on $HOST"
  remote "$HOST" "
    set -e
    export JAVA_HOME=/usr/lib/jvm/java-21-amazon-corretto.x86_64
    export PATH=\$JAVA_HOME/bin:\$PATH
    [ -d '$REPO_DIR' ] || git clone -q https://github.com/dazzleduck-web/dazzleduck-sql-server.git '$REPO_DIR'
    cd '$REPO_DIR'
    ./mvnw -B -ntp -q -DskipTests install -pl '$MODULE' -am
    ./mvnw -B -ntp -q dependency:copy-dependencies -DoutputDirectory=target/lib -pl '$MODULE'
  " || die "build failed on $HOST"
  pass "build" "$MODULE on $HOST"
fi

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
# slf4j-simple, not logback: see compactor/config/simplelogger.properties for why.
copy_to "$HOST" "$PROJECT_ROOT/compactor/config/simplelogger.properties" "$BASE_DIR/conf/simplelogger.properties"

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

  # Loopback probe on the remote host; cross-network reachability is a separate
  # concern (connectivity-check.sh).
  body=""
  for _ in $(seq 1 60); do
    body="$(remote "$HOST" "curl -sf --max-time 3 http://127.0.0.1:$COMPACTOR_HEALTH_PORT/health" 2>/dev/null || true)"
    [ -n "$body" ] && break
    sleep 2
  done

  if [ -n "$body" ]; then
    pass "compactor health (local)" "$(echo "$body" | tr -d '\n ' | cut -c1-160)"
  else
    fail "compactor health (local)" "no /health response on $HOST after 120s"
    remote "$HOST" "sudo systemctl status '$SYSTEMD_UNIT' --no-pager -l | head -20; tail -n 40 '$BASE_DIR/logs/compactor.log'" || true
  fi

  if tcp_open "$HOST" "$COMPACTOR_HEALTH_PORT" 3; then
    pass "compactor health (remote)" "$HOST:$COMPACTOR_HEALTH_PORT reachable"
  else
    warn "compactor health (remote)" \
      "$HOST:$COMPACTOR_HEALTH_PORT not reachable from here — service is up, so this is the security group. See NETWORK.md"
  fi
fi

summary
