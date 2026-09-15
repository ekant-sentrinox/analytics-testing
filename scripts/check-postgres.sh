#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verify the PostgreSQL RDS catalog before a run.
#
#   scripts/check-postgres.sh
#
# Read-only apart from the connection itself. Confirms credentials, that the
# benchmark database exists, that the DuckLake metadata tables are present, and
# reports catalog size and current backlog. Never creates, drops or alters
# anything — bootstrap-catalog.sh owns schema changes.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

load_env

: "${PG_HOST:?PG_HOST not set}"; : "${PG_DATABASE:?PG_DATABASE not set}"
: "${PG_USER:?PG_USER not set}"

if [ -z "${PG_PASSWORD:-}" ]; then
  fail "password file" "could not read $PG_PASSWORD_FILE"
  summary; exit 1
fi
pass "password file" "$PG_PASSWORD_FILE (${#PG_PASSWORD} chars)"

# --- reachability ---------------------------------------------------------------
if tcp_open "$PG_HOST" "$PG_PORT" 5; then
  pass "tcp $PG_PORT" "$PG_HOST reachable"
else
  fail "tcp $PG_PORT" "$PG_HOST:$PG_PORT unreachable — security group or subnet routing"
  summary; exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  warn "psql" "not installed; falling back to the python client"
  PSQL=""
else
  PSQL=1
fi

q() {
  if [ -n "$PSQL" ]; then
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" \
      -d "$1" -tAc "$2" 2>&1
  else
    "$(python_bin)" - "$1" "$2" <<'PY' 2>&1
import os, sys
import psycopg
db, sql = sys.argv[1], sys.argv[2]
dsn = (f"host={os.environ['PG_HOST']} port={os.environ['PG_PORT']} dbname={db} "
       f"user={os.environ['PG_USER']} password={os.environ['PG_PASSWORD']} connect_timeout=5")
with psycopg.connect(dsn) as c, c.cursor() as cur:
    cur.execute(sql)
    for row in cur.fetchall():
        print("|".join("" if v is None else str(v) for v in row))
PY
  fi
}

# --- auth -----------------------------------------------------------------------
if ver="$(q postgres 'SELECT version()')" && echo "$ver" | grep -qi postgresql; then
  pass "authentication" "user $PG_USER — $(echo "$ver" | cut -c1-40)"
else
  fail "authentication" "$(echo "$ver" | head -1)"
  summary; exit 1
fi

# --- database -------------------------------------------------------------------
if q postgres "SELECT 1 FROM pg_database WHERE datname='$PG_DATABASE'" | grep -q 1; then
  pass "database" "$PG_DATABASE exists"
else
  fail "database" "$PG_DATABASE does not exist — run scripts/bootstrap-catalog.sh"
  summary; exit 1
fi

# --- ducklake metadata ------------------------------------------------------------
tables="$(q "$PG_DATABASE" "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'ducklake_%'")"
if [ "${tables:-0}" -gt 0 ] 2>/dev/null; then
  pass "ducklake metadata" "$tables ducklake_* tables in public"
else
  fail "ducklake metadata" "no ducklake_* tables — the catalog has never been attached"
fi

# --- the tables the collector writes into -------------------------------------------
for t in "$DUCKLAKE_LOGS_TABLE" "$DUCKLAKE_WATERMARK_TABLE"; do
  n="$(q "$PG_DATABASE" "SELECT count(*) FROM ducklake_table WHERE table_name='$t' AND end_snapshot IS NULL")"
  if [ "${n:-0}" -ge 1 ] 2>/dev/null; then
    pass "table $t" "registered in the catalog"
  else
    fail "table $t" "missing — run scripts/bootstrap-catalog.sh"
  fi
done

# --- state ---------------------------------------------------------------------------
size="$(q "$PG_DATABASE" 'SELECT pg_size_pretty(pg_database_size(current_database()))')"
snaps="$(q "$PG_DATABASE" 'SELECT count(*) FROM ducklake_snapshot')"
backlog="$(q "$PG_DATABASE" "SELECT count(*) || ' files / ' || COALESCE(pg_size_pretty(sum(file_size_bytes)),'0 B') || ' / ' || COALESCE(sum(record_count),0) || ' rows' FROM ducklake_data_file WHERE end_snapshot IS NULL")"
conns="$(q "$PG_DATABASE" 'SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()')"

pass "catalog size" "$size, $snaps snapshots, $conns connection(s)"
pass "live files (B2)" "$backlog"

summary
