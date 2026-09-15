#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Validate the whole pipeline, stage by stage.
#
#   scripts/health-check.sh [--deep]
#
#   Generator -> Collector -> S3 -> Catalog -> Compactor
#
# --deep additionally pushes a handful of real records through the pipeline and
# reads them back out of the lake, which is the only check that proves the path
# actually works rather than that each component is individually alive.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

DEEP=0
[ "${1:-}" = "--deep" ] && DEEP=1

load_env
PY="$(python_bin)"

echo "pipeline health — $(utc_now)"
echo

# --- stage 1: generator host ------------------------------------------------
info "1. generator (SERVER 1, $GENERATOR_HOST)"
[ -x "$PROJECT_ROOT/.venv/bin/python" ] && pass "runtime" "venv present" \
                                        || fail "runtime" "no venv — scripts/setup.sh"
if "$PY" -c "import generator.src.payload" 2>/dev/null; then
  pass "generator module" "imports cleanly"
else
  fail "generator module" "import failed"
fi
free_gb="$(df -BG --output=avail "$PROJECT_ROOT" | tail -1 | tr -dc '0-9')"
[ "${free_gb:-0}" -ge 1 ] && pass "disk" "${free_gb} GiB free" \
                          || fail "disk" "only ${free_gb} GiB free"

# --- stage 2: collector -------------------------------------------------------
echo; info "2. collector (SERVER 2, $COLLECTOR_HOST)"
unit="$(remote "$COLLECTOR_SSH_HOST" 'systemctl is-active bench-collector' 2>/dev/null || echo unreachable)"
[ "$unit" = "active" ] && pass "service" "bench-collector active" \
                       || fail "service" "bench-collector is $unit"

body="$(remote "$COLLECTOR_SSH_HOST" \
  "curl -sf --max-time 5 http://127.0.0.1:$COLLECTOR_HEALTH_PORT/health" 2>/dev/null || true)"
if [ -n "$body" ]; then
  status="$(echo "$body" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("status"))')"
  queues="$(echo "$body" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("knownQueues"))')"
  batches="$(echo "$body" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("batchesProcessed"))')"
  case "$status" in
    HEALTHY)     pass "health" "HEALTHY, $queues queue(s), $batches batches processed" ;;
    MAINTENANCE) warn "health" "MAINTENANCE — a graceful shutdown drain is in progress" ;;
    *)           fail "health" "status $status" ;;
  esac
else
  fail "health" "no /health response on the collector's loopback"
fi

if tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 4; then
  pass "grpc reachable" "$COLLECTOR_HOST:$COLLECTOR_GRPC_PORT from here"
else
  fail "grpc reachable" "$COLLECTOR_HOST:$COLLECTOR_GRPC_PORT blocked — the generator cannot send. See NETWORK.md"
fi

# The queue name in the JWT must match a configured ingestion_queue, or every
# export is rejected with INVALID_ARGUMENT regardless of how healthy the
# service looks.
if remote "$COLLECTOR_SSH_HOST" \
    "grep -q 'ingestion_queue = \"$OTEL_INGESTION_QUEUE\"' /opt/analytics-bench/collector/conf/application.conf" 2>/dev/null; then
  pass "queue mapping" "'$OTEL_INGESTION_QUEUE' is configured on the collector"
else
  fail "queue mapping" "'$OTEL_INGESTION_QUEUE' not found in the collector config — every export would be rejected"
fi

# --- stage 3: S3 ----------------------------------------------------------------
echo; info "3. object storage (s3://$S3_BUCKET/$S3_PREFIX/)"
if aws s3api head-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION" >/dev/null 2>&1; then
  n="$(aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "${S3_PREFIX%/}/" \
       --region "$AWS_REGION" --query 'length(Contents || `[]`)' --output text 2>/dev/null || echo '?')"
  pass "bucket" "reachable, $n object(s) under the prefix"
else
  fail "bucket" "unreachable"
fi

# --- stage 4: catalog ---------------------------------------------------------------
echo; info "4. catalog ($PG_DATABASE on $PG_HOST)"
cat_json="$(PG_PASSWORD="$PG_PASSWORD" "$PY" - <<'PY' 2>/dev/null || echo ''
import json, os, psycopg
dsn = (f"host={os.environ['PG_HOST']} port={os.environ['PG_PORT']} dbname={os.environ['PG_DATABASE']} "
       f"user={os.environ['PG_USER']} password={os.environ['PG_PASSWORD']} connect_timeout=6")
with psycopg.connect(dsn) as c, c.cursor() as cur:
    cur.execute("SELECT count(*) FROM ducklake_data_file WHERE end_snapshot IS NULL")
    files = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM ducklake_snapshot")
    snaps = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM ducklake_table WHERE end_snapshot IS NULL")
    tables = cur.fetchone()[0]
print(json.dumps({"files": files, "snapshots": snaps, "tables": tables}))
PY
)"
if [ -n "$cat_json" ]; then
  echo "$cat_json" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("PASS  %-28s %s live file(s), %s snapshot(s), %s table(s)" % (
    "catalog", d["files"], d["snapshots"], d["tables"]))'
  CHECKS_RUN=$((CHECKS_RUN+1))
else
  fail "catalog" "query failed"
fi

# --- stage 5: compactor -------------------------------------------------------------
echo; info "5. compactor (SERVER 3, $COMPACTOR_HOST)"
unit="$(remote "$COMPACTOR_SSH_HOST" 'systemctl is-active bench-compactor' 2>/dev/null || echo unreachable)"
[ "$unit" = "active" ] && pass "service" "bench-compactor active" \
                       || fail "service" "bench-compactor is $unit"

body="$(remote "$COMPACTOR_SSH_HOST" \
  "curl -sf --max-time 5 http://127.0.0.1:$COMPACTOR_HEALTH_PORT/health" 2>/dev/null || true)"
if [ -n "$body" ]; then
  echo "$body" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("PASS  %-28s %s, uptime %s" % ("health", d.get("status"), d.get("uptime")))
for name, s in (d.get("databases") or {}).items():
    print("PASS  %-28s %s: minor=%s major=%s merged=%s, next %s" % (
        "catalog "+name, name, s.get("totalMinorCompactions"),
        s.get("totalMajorCompactions"), s.get("totalFilesCompacted"),
        s.get("nextExecutionTime")))'
  CHECKS_RUN=$((CHECKS_RUN+2))
  # The compactor must be attached to the same catalog the collector writes to,
  # or it will run forever and merge nothing.
  if echo "$body" | grep -q "\"$DUCKLAKE_CATALOG\""; then
    pass "catalog binding" "compactor is attached to '$DUCKLAKE_CATALOG'"
  else
    fail "catalog binding" "compactor is not attached to '$DUCKLAKE_CATALOG' — it will merge nothing"
  fi
else
  fail "health" "no /health response on the compactor's loopback"
fi

# --- deep: end-to-end ------------------------------------------------------------------
if [ "$DEEP" -eq 1 ]; then
  echo; info "6. end-to-end probe"
  if ! tcp_open "$COLLECTOR_HOST" "$COLLECTOR_GRPC_PORT" 4; then
    fail "e2e" "gRPC port unreachable — cannot run the probe"
  else
    ID="healthcheck-$(date -u +%H%M%S)"
    before="$(PG_PASSWORD="$PG_PASSWORD" "$PY" - <<'PY' 2>/dev/null || echo 0
import os, psycopg
dsn = (f"host={os.environ['PG_HOST']} port={os.environ['PG_PORT']} dbname={os.environ['PG_DATABASE']} "
       f"user={os.environ['PG_USER']} password={os.environ['PG_PASSWORD']} connect_timeout=6")
with psycopg.connect(dsn) as c, c.cursor() as cur:
    cur.execute("SELECT COALESCE(sum(record_count),0) FROM ducklake_data_file WHERE end_snapshot IS NULL")
    print(cur.fetchone()[0])
PY
)"
    if "$PY" -m generator.src.main --config "$PROJECT_ROOT/config/generator.yaml" \
         --profile smoke --rps 20 --duration 10 --test-id "$ID" \
         --results-dir "$PROJECT_ROOT/logs/healthcheck" --log-level ERROR >/dev/null 2>&1; then
      acked="$("$PY" -c "import json;print(json.load(open('$PROJECT_ROOT/logs/healthcheck/$ID/manifest.json'))['total_accepted'])")"
      pass "e2e send" "$acked records acked by the collector"

      # Wait out one flush plus a compaction tick before deciding it did not land.
      landed=0
      for _ in $(seq 1 24); do
        sleep 5
        after="$(PG_PASSWORD="$PG_PASSWORD" "$PY" - <<'PY' 2>/dev/null || echo 0
import os, psycopg
dsn = (f"host={os.environ['PG_HOST']} port={os.environ['PG_PORT']} dbname={os.environ['PG_DATABASE']} "
       f"user={os.environ['PG_USER']} password={os.environ['PG_PASSWORD']} connect_timeout=6")
with psycopg.connect(dsn) as c, c.cursor() as cur:
    cur.execute("SELECT COALESCE(sum(record_count),0) FROM ducklake_data_file WHERE end_snapshot IS NULL")
    print(cur.fetchone()[0])
PY
)"
        landed=$((after - before))
        [ "$landed" -ge "$acked" ] && break
      done
      if [ "$landed" -ge "$acked" ]; then
        pass "e2e land" "$landed row(s) registered in the catalog — full path works"
      else
        fail "e2e land" "only $landed of $acked rows visible in the catalog after 120s"
      fi
    else
      fail "e2e send" "the probe generator run failed"
    fi
  fi
fi

echo
summary
