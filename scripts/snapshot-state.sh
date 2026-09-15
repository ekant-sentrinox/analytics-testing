#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Print a JSON snapshot of catalog, S3 and service state to stdout.
#
#   scripts/snapshot-state.sh
#
# Taken before and after every run. The difference between the two is what
# the run actually did to the lake, which is more trustworthy than any
# absolute count on a catalog that other things may also be writing to.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

load_env
PY="$(python_bin)"

COLLECTOR_HEALTH="$(remote "$COLLECTOR_SSH_HOST" \
  "curl -sf --max-time 4 http://127.0.0.1:$COLLECTOR_HEALTH_PORT/health" 2>/dev/null || echo '')"
COMPACTOR_HEALTH="$(remote "$COMPACTOR_SSH_HOST" \
  "curl -sf --max-time 4 http://127.0.0.1:$COMPACTOR_HEALTH_PORT/health" 2>/dev/null || echo '')"

S3_JSON="$(aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "${S3_PREFIX%/}/" \
  --region "$AWS_REGION" \
  --query '{objects: length(Contents || `[]`), bytes: sum(Contents[].Size || `[0]`)}' \
  --output json 2>/dev/null || echo 'null')"

PG_PASSWORD="$PG_PASSWORD" COLLECTOR_HEALTH="$COLLECTOR_HEALTH" \
COMPACTOR_HEALTH="$COMPACTOR_HEALTH" S3_JSON="$S3_JSON" "$PY" - <<'PY'
import json, os, time

def maybe(text):
    try:
        return json.loads(text) if text and text.strip() else None
    except Exception:
        return None

out = {
    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "captured_at_epoch": time.time(),
    "collector_health": maybe(os.environ.get("COLLECTOR_HEALTH", "")),
    "compactor_health": maybe(os.environ.get("COMPACTOR_HEALTH", "")),
    "s3": maybe(os.environ.get("S3_JSON", "")),
    "catalog": None,
    "catalog_error": None,
}

try:
    import psycopg
    dsn = (f"host={os.environ['PG_HOST']} port={os.environ['PG_PORT']} "
           f"dbname={os.environ['PG_DATABASE']} user={os.environ['PG_USER']} "
           f"password={os.environ['PG_PASSWORD']} connect_timeout=8")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*), COALESCE(sum(file_size_bytes),0), COALESCE(sum(record_count),0)
            FROM ducklake_data_file WHERE end_snapshot IS NULL""")
        files, nbytes, rows = cur.fetchone()
        cur.execute("""
            SELECT count(*), COALESCE(sum(file_size_bytes),0), COALESCE(sum(record_count),0)
            FROM ducklake_data_file WHERE end_snapshot IS NULL AND file_size_bytes < 8388608""")
        sf, sb, sr = cur.fetchone()
        cur.execute("SELECT count(*), COALESCE(max(snapshot_id),0) FROM ducklake_snapshot")
        snaps, max_snap = cur.fetchone()
        cur.execute("SELECT pg_database_size(current_database())")
        dbsize = cur.fetchone()[0]
        # File-size distribution: one number for "how fragmented is the lake",
        # which is the thing compaction is supposed to be fixing.
        cur.execute("""
            SELECT COALESCE(min(file_size_bytes),0), COALESCE(max(file_size_bytes),0),
                   COALESCE(avg(file_size_bytes),0)
            FROM ducklake_data_file WHERE end_snapshot IS NULL""")
        fmin, fmax, favg = cur.fetchone()
    out["catalog"] = {
        "live_files": files, "live_bytes": int(nbytes), "live_rows": int(rows),
        "small_files": sf, "small_bytes": int(sb), "small_rows": int(sr),
        "snapshots": snaps, "max_snapshot_id": int(max_snap),
        "catalog_db_bytes": int(dbsize),
        "file_size_min": int(fmin), "file_size_max": int(fmax),
        "file_size_avg": float(favg),
    }
except Exception as exc:                                          # noqa: BLE001
    out["catalog_error"] = str(exc).split("\n")[0][:200]

print(json.dumps(out, indent=2, default=str))
PY
