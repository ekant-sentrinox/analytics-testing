#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Delete benchmark data. Destructive — read this before running it.
#
#   scripts/cleanup.sh --data     [--yes]   S3 objects + catalog rows for the lake
#   scripts/cleanup.sh --results  [--yes]   local results/ and logs/
#   scripts/cleanup.sh --all      [--yes]
#
# Hard boundaries, enforced not just documented:
#   * S3 deletion is restricted to s3://$S3_BUCKET/$S3_PREFIX/ and the script
#     refuses to run if S3_PREFIX is empty, "/", or ".".
#   * It NEVER deletes the bucket, and never touches a key outside the prefix.
#   * Catalog cleanup drops only the benchmark TABLES inside $DUCKLAKE_CATALOG.
#     It never drops the database and never touches the other databases on that
#     RDS instance (bench_150k, bench_fixture, bench_meta, ollylake_meta ...).
#   * Without --yes it prints exactly what it would remove and exits.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

DO_DATA=0; DO_RESULTS=0; ASSUME_YES=0
for a in "$@"; do
  case "$a" in
    --data)    DO_DATA=1 ;;
    --results) DO_RESULTS=1 ;;
    --all)     DO_DATA=1; DO_RESULTS=1 ;;
    --yes)     ASSUME_YES=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) die "unknown argument: $a" ;;
  esac
done
[ "$DO_DATA" -eq 1 ] || [ "$DO_RESULTS" -eq 1 ] || { sed -n '2,22p' "$0"; exit 1; }

load_env
PY="$(python_bin)"

# --- guard rails --------------------------------------------------------------
case "${S3_PREFIX:-}" in
  ""|"/"|"."|"*") die "refusing to run: S3_PREFIX is '${S3_PREFIX:-}', which would target the whole bucket" ;;
esac
PREFIX="${S3_PREFIX%/}/"
[ -n "${S3_BUCKET:-}" ] || die "S3_BUCKET is not set"
[ -n "${DUCKLAKE_CATALOG:-}" ] || die "DUCKLAKE_CATALOG is not set"

echo
warn "scope" "s3://$S3_BUCKET/$PREFIX  and  catalog tables in $PG_DATABASE"
warn "scope" "nothing outside that prefix, and no database, is touched"
echo

# --- what would go ---------------------------------------------------------------
if [ "$DO_DATA" -eq 1 ]; then
  n="$(aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "$PREFIX" --region "$AWS_REGION" \
        --query 'length(Contents || `[]`)' --output text 2>/dev/null || echo 0)"
  b="$(aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "$PREFIX" --region "$AWS_REGION" \
        --query 'sum(Contents[].Size || `[0]`)' --output text 2>/dev/null || echo 0)"
  info "S3: $n object(s), $b byte(s) under s3://$S3_BUCKET/$PREFIX"
  info "Catalog: tables $DUCKLAKE_CATALOG.$DUCKLAKE_SCHEMA.{$DUCKLAKE_LOGS_TABLE,$DUCKLAKE_WATERMARK_TABLE}"
fi
if [ "$DO_RESULTS" -eq 1 ]; then
  r="$(find "$PROJECT_ROOT/results" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)"
  info "Local: $r run directory(ies) in results/, plus logs/*.jsonl"
fi

if [ "$ASSUME_YES" -ne 1 ]; then
  echo
  warn "dry run" "nothing was deleted. Re-run with --yes to proceed."
  exit 0
fi

echo
read -r -p "Type the bucket name to confirm deletion under its test prefix: " typed
[ "$typed" = "$S3_BUCKET" ] || die "confirmation did not match — nothing deleted"

# --- catalog first --------------------------------------------------------------
# Order matters: dropping the tables through DuckLake also schedules their data
# files for deletion, so the catalog and the object store stay consistent. Wiping
# S3 first would leave the catalog pointing at files that no longer exist.
if [ "$DO_DATA" -eq 1 ]; then
  info "dropping benchmark tables from $DUCKLAKE_CATALOG"
  sql="INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;
INSTALL ducklake; LOAD ducklake; INSTALL postgres; LOAD postgres;
CREATE OR REPLACE SECRET s3_role (TYPE S3, PROVIDER credential_chain, REGION '$AWS_REGION');
ATTACH 'ducklake:postgres:host=$PG_HOST port=$PG_PORT dbname=$PG_DATABASE user=$PG_USER password=$PG_PASSWORD' AS $DUCKLAKE_CATALOG (DATA_PATH 's3://$S3_BUCKET/$PREFIX', DATA_INLINING_ROW_LIMIT 0);
DROP TABLE IF EXISTS $DUCKLAKE_CATALOG.$DUCKLAKE_SCHEMA.$DUCKLAKE_LOGS_TABLE;
DROP TABLE IF EXISTS $DUCKLAKE_CATALOG.$DUCKLAKE_SCHEMA.$DUCKLAKE_WATERMARK_TABLE;
CALL ducklake_expire_snapshots('$DUCKLAKE_CATALOG', older_than => now());
CALL ducklake_cleanup_old_files('$DUCKLAKE_CATALOG', cleanup_all => true);
SELECT 'dropped' AS status;"
  if "$PY" "$PROJECT_ROOT/scripts/lib/duckdb_exec.py" --sql "$sql" --format csv --last-only \
      >/tmp/cleanup.$$ 2>&1; then
    pass "catalog" "benchmark tables dropped and old files cleaned up"
  else
    warn "catalog" "drop/cleanup reported errors: $(tail -2 /tmp/cleanup.$$ | tr '\n' ' ')"
  fi
  rm -f /tmp/cleanup.$$

  # Sweep anything DuckLake left behind, still scoped to the prefix.
  info "removing remaining objects under s3://$S3_BUCKET/$PREFIX"
  if aws s3 rm "s3://$S3_BUCKET/$PREFIX" --recursive --region "$AWS_REGION" >/dev/null 2>&1; then
    left="$(aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "$PREFIX" \
            --region "$AWS_REGION" --query 'length(Contents || `[]`)' --output text 2>/dev/null || echo '?')"
    pass "s3" "prefix cleared ($left object(s) remain)"
  else
    fail "s3" "recursive delete failed"
  fi

  info "re-creating the benchmark tables"
  "$PROJECT_ROOT/scripts/bootstrap-catalog.sh" >/dev/null 2>&1 \
    && pass "catalog" "tables re-created, ready for the next run" \
    || warn "catalog" "re-create failed — run scripts/bootstrap-catalog.sh by hand"
fi

# --- local artifacts ---------------------------------------------------------------
if [ "$DO_RESULTS" -eq 1 ]; then
  find "$PROJECT_ROOT/results" -maxdepth 1 -mindepth 1 -type d -exec rm -rf {} + 2>/dev/null || true
  rm -f "$PROJECT_ROOT"/logs/*.jsonl "$PROJECT_ROOT"/logs/*.out 2>/dev/null || true
  touch "$PROJECT_ROOT/results/.gitkeep" "$PROJECT_ROOT/logs/.gitkeep"
  pass "local" "results/ and logs/ cleared"
fi

echo
summary
