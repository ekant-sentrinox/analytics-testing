#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Verify S3 access before a run.
#
#   scripts/check-s3.sh
#
# Checks identity, bucket reachability, and list/read/write inside the
# configured test prefix. The write probe writes one small object and deletes
# it again, inside s3://$S3_BUCKET/$S3_PREFIX/. Nothing outside that prefix is
# ever touched, listed for deletion, or counted.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

load_env
PY="$(python_bin)"
c() { "$PY" "$PROJECT_ROOT/scripts/lib/cfg.py" get "$PROJECT_ROOT/config/s3.yaml" "$1" ${2+--default "$2"}; }

BUCKET="${S3_BUCKET:?S3_BUCKET not set}"
PREFIX="${S3_PREFIX:?S3_PREFIX not set}"
REGION="${AWS_REGION:-us-west-2}"
PROBE_KEY="$(c s3.preflight.probe_key "_preflight/probe.txt")"
FULL_PROBE="${PREFIX%/}/${PROBE_KEY#/}"

command -v aws >/dev/null 2>&1 || die "aws CLI not found"

# --- 1. identity --------------------------------------------------------------
if ident="$(aws sts get-caller-identity --output json 2>&1)"; then
  arn="$(echo "$ident" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')"
  pass "aws identity" "$arn"
  case "$arn" in
    *assumed-role*) : ;;
    *) warn "aws identity" "not an instance role — this project expects IAM role auth, not keys" ;;
  esac
else
  fail "aws identity" "$(echo "$ident" | head -1)"
  summary; exit 1
fi

if [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  warn "aws credentials" "AWS_SECRET_ACCESS_KEY is set in the environment — the instance role should be used instead"
fi

# --- 2. bucket ----------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  pass "bucket" "s3://$BUCKET reachable in $REGION"
else
  fail "bucket" "head-bucket failed for s3://$BUCKET"
  summary; exit 1
fi

# --- 3. list ------------------------------------------------------------------
if out="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "${PREFIX%/}/" \
          --max-items 1 --region "$REGION" 2>&1)"; then
  # KeyCount does not survive the CLI's auto-pagination (it merges pages and
  # drops the per-page field), so count the merged Contents instead.
  n="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "${PREFIX%/}/" \
        --region "$REGION" --query 'length(Contents || `[]`)' --output text 2>/dev/null || echo '?')"
  pass "list" "s3://$BUCKET/${PREFIX%/}/ ($n objects)"
else
  fail "list" "$(echo "$out" | head -1)"
fi

# --- 4. write -----------------------------------------------------------------
tmp="$(mktemp)"
printf 'analytics-distributed-test preflight %s\n' "$(utc_now)" > "$tmp"
if aws s3 cp "$tmp" "s3://$BUCKET/$FULL_PROBE" --region "$REGION" >/dev/null 2>&1; then
  pass "write" "s3://$BUCKET/$FULL_PROBE"
else
  fail "write" "cannot put objects under the test prefix"
fi

# --- 5. read ------------------------------------------------------------------
if aws s3 cp "s3://$BUCKET/$FULL_PROBE" - --region "$REGION" 2>/dev/null | grep -q preflight; then
  pass "read" "probe object read back"
else
  fail "read" "could not read the probe object back"
fi

# --- 6. clean up the probe (and only the probe) --------------------------------
if aws s3 rm "s3://$BUCKET/$FULL_PROBE" --region "$REGION" >/dev/null 2>&1; then
  pass "delete" "probe removed"
else
  warn "delete" "probe left behind at s3://$BUCKET/$FULL_PROBE — harmless, remove by hand"
fi
rm -f "$tmp"

# --- 7. DuckDB's own S3 path ----------------------------------------------------
# The aws CLI and DuckDB resolve credentials differently. The CLI succeeding does
# not prove DuckDB can read the bucket, and DuckDB is what actually writes the
# lake — so test that too.
probe_sql="INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws;
CREATE OR REPLACE SECRET s3_role (TYPE S3, PROVIDER credential_chain, REGION '$REGION');
SELECT count(*) AS visible FROM glob('s3://$BUCKET/${PREFIX%/}/**');"
if out="$("$PY" "$PROJECT_ROOT/scripts/lib/duckdb_exec.py" --sql "$probe_sql" --format csv --last-only 2>&1)"; then
  pass "duckdb s3" "credential_chain works ($(echo "$out" | tail -1) objects globbed)"
else
  fail "duckdb s3" "$(echo "$out" | head -2 | tr '\n' ' ')"
fi

summary
