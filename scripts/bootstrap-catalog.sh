#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Create the DuckLake tables the collector writes into.
#
#   scripts/bootstrap-catalog.sh [--show]
#
# Idempotent (CREATE TABLE IF NOT EXISTS) and additive only — it never drops or
# alters anything. Runs from SERVER 1 against the shared RDS catalog and the
# shared S3 bucket, both of which are treated as pre-existing infrastructure.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

SHOW=0
[ "${1:-}" = "--show" ] && SHOW=1

load_env
: "${PG_PASSWORD:?catalog password could not be read from $PG_PASSWORD_FILE}"

# Deliberately the venv's duckdb 1.5.4, not the host CLI (1.5.2): DuckLake's
# catalog metadata schema is version dependent and the server writes with 1.5.4.
PY="$(python_bin)"
EXEC="$PROJECT_ROOT/scripts/lib/duckdb_exec.py"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

render_template "$PROJECT_ROOT/sql/catalog_bootstrap.sql.tmpl" "$STAGE/bootstrap.sql"

if [ "$SHOW" -eq 1 ]; then
  # Redact before showing: this file contains the catalog password.
  sed 's/password=[^ ]*/password=***/' "$STAGE/bootstrap.sql"
  exit 0
fi

info "bootstrapping catalog $DUCKLAKE_CATALOG ($PG_DATABASE on $PG_HOST)"
info "data path s3://$S3_BUCKET/$S3_PREFIX/"

out="$("$PY" "$EXEC" --file "$STAGE/bootstrap.sql" --format csv 2>&1)" || {
  echo "$out" | sed 's/password=[^ ]*/password=***/'
  die "catalog bootstrap failed"
}

if echo "$out" | grep -q catalog_bootstrap_ok; then
  pass "catalog bootstrap" "$DUCKLAKE_CATALOG.$DUCKLAKE_SCHEMA.{$DUCKLAKE_LOGS_TABLE,$DUCKLAKE_WATERMARK_TABLE}"
else
  echo "$out" | sed 's/password=[^ ]*/password=***/'
  fail "catalog bootstrap" "did not report success"
fi

summary
