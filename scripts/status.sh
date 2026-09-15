#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One screen showing the whole environment.
#
#   scripts/status.sh [--json]
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

JSON=0
[ "${1:-}" = "--json" ] && JSON=1

load_env
PY="$(python_bin)"

remote_json() {                 # remote_json host port path
  remote "$1" "curl -sf --max-time 4 http://127.0.0.1:$2$3" 2>/dev/null || true
}

COLLECTOR_HEALTH="$(remote_json "$COLLECTOR_SSH_HOST" "$COLLECTOR_HEALTH_PORT" /health)"
COMPACTOR_HEALTH="$(remote_json "$COMPACTOR_SSH_HOST" "$COMPACTOR_HEALTH_PORT" /health)"
COLLECTOR_UNIT="$(remote "$COLLECTOR_SSH_HOST" 'systemctl is-active bench-collector' 2>/dev/null || echo unreachable)"
COMPACTOR_UNIT="$(remote "$COMPACTOR_SSH_HOST" 'systemctl is-active bench-compactor' 2>/dev/null || echo unreachable)"

CATALOG="$(PG_PASSWORD="$PG_PASSWORD" "$PY" - <<'PY' 2>/dev/null || echo '{}'
import json, os
import psycopg
dsn = (f"host={os.environ['PG_HOST']} port={os.environ['PG_PORT']} "
       f"dbname={os.environ['PG_DATABASE']} user={os.environ['PG_USER']} "
       f"password={os.environ['PG_PASSWORD']} connect_timeout=5")
with psycopg.connect(dsn) as c, c.cursor() as cur:
    cur.execute("""SELECT count(*), COALESCE(sum(file_size_bytes),0), COALESCE(sum(record_count),0)
                   FROM ducklake_data_file WHERE end_snapshot IS NULL""")
    files, nbytes, rows = cur.fetchone()
    cur.execute("SELECT count(*) FROM ducklake_snapshot")
    snaps = cur.fetchone()[0]
    cur.execute("SELECT pg_database_size(current_database())")
    size = cur.fetchone()[0]
print(json.dumps({"files": files, "bytes": int(nbytes), "rows": int(rows),
                  "snapshots": snaps, "db_bytes": int(size)}))
PY
)"

S3="$(aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "${S3_PREFIX%/}/" \
      --region "$AWS_REGION" --query '{objects: length(Contents || `[]`), bytes: sum(Contents[].Size || `[0]`)}' \
      --output json 2>/dev/null || echo '{}')"

if [ "$JSON" -eq 1 ]; then
  "$PY" - "$COLLECTOR_HEALTH" "$COMPACTOR_HEALTH" "$CATALOG" "$S3" \
          "$COLLECTOR_UNIT" "$COMPACTOR_UNIT" <<'PY'
import json, sys
def j(s):
    try: return json.loads(s) if s.strip() else None
    except Exception: return None
col, com, cat, s3, cu, mu = sys.argv[1:7]
print(json.dumps({
    "collector": {"unit": cu, "health": j(col)},
    "compactor": {"unit": mu, "health": j(com)},
    "catalog": j(cat), "s3": j(s3),
}, indent=2))
PY
  exit 0
fi

hr() { printf '%s\n' "----------------------------------------------------------------------"; }

echo
hr; printf '  ENVIRONMENT   %s\n' "$(utc_now)"; hr

printf '\n  SERVER 1  generator   %s\n' "$GENERATOR_HOST"
if [ -f "$PROJECT_ROOT/run/exporter.pid" ] && kill -0 "$(cat "$PROJECT_ROOT/run/exporter.pid")" 2>/dev/null; then
  printf '            exporter    running (pid %s, :%s)\n' \
    "$(cat "$PROJECT_ROOT/run/exporter.pid")" "${PIPELINE_EXPORTER_PORT:-9101}"
else
  printf '            exporter    stopped\n'
fi
if [ -f "$PROJECT_ROOT/run/agent.pid" ] && kill -0 "$(cat "$PROJECT_ROOT/run/agent.pid")" 2>/dev/null; then
  printf '            host agent  running (pid %s)\n' "$(cat "$PROJECT_ROOT/run/agent.pid")"
else
  printf '            host agent  stopped\n'
fi
printf '            load        %s\n' "$(cut -d' ' -f1-3 /proc/loadavg)"
printf '            disk        %s free\n' "$(df -h --output=avail "$PROJECT_ROOT" | tail -1 | tr -d ' ')"

printf '\n  SERVER 2  collector   %s   unit=%s\n' "$COLLECTOR_HOST" "$COLLECTOR_UNIT"
if [ -n "$COLLECTOR_HEALTH" ]; then
  echo "$COLLECTOR_HEALTH" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("            health      %s, up %ss, queues %s, batches %s" % (
    d.get("status"), int(d.get("uptimeSeconds",0)), d.get("knownQueues"), d.get("batchesProcessed")))'
else
  printf '            health      NO RESPONSE\n'
fi

printf '\n  SERVER 3  compactor   %s   unit=%s\n' "$COMPACTOR_HOST" "$COMPACTOR_UNIT"
if [ -n "$COMPACTOR_HEALTH" ]; then
  echo "$COMPACTOR_HEALTH" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("            health      %s, up %s" % (d.get("status"), d.get("uptime")))
for name, s in (d.get("databases") or {}).items():
    print("            %-11s minor=%s major=%s merged=%s | small=%s medium=%s total=%s" % (
        name, s.get("totalMinorCompactions"), s.get("totalMajorCompactions"),
        s.get("totalFilesCompacted"), s.get("currentSmallFiles"),
        s.get("currentMediumFiles"), s.get("currentTotalFiles")))
    print("            next run    %s" % s.get("nextExecutionTime"))'
else
  printf '            health      NO RESPONSE\n'
fi

printf '\n  CATALOG   %s/%s\n' "$PG_HOST" "$PG_DATABASE"
echo "$CATALOG" | "$PY" -c '
import json,sys
d=json.load(sys.stdin) or {}
if not d: print("            unreachable"); raise SystemExit
rows = format(d.get("rows", 0), ",")
print("            live files  %s  (%.2f MiB, %s rows)" % (
    d.get("files"), d.get("bytes", 0)/1048576, rows))
print("            snapshots   %s      catalog db %.2f MiB" % (
    d.get("snapshots"), d.get("db_bytes", 0)/1048576))'

printf '\n  S3        s3://%s/%s/\n' "$S3_BUCKET" "${S3_PREFIX%/}"
echo "$S3" | "$PY" -c '
import json,sys
d=json.load(sys.stdin) or {}
n=d.get("objects") or 0; b=d.get("bytes") or 0
print("            objects     %s  (%.2f MiB)" % (n, (b or 0)/1048576))'

printf '\n  RESULTS   %s\n' "$PROJECT_ROOT/results"
n=$(find "$PROJECT_ROOT/results" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
printf '            %s run(s)\n' "$n"
if [ "$n" -gt 0 ]; then
  find "$PROJECT_ROOT/results" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null \
    | sort | tail -5 | sed 's/^/            /'
fi
echo; hr; echo
