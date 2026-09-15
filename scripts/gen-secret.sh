#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Generate the HMAC secret shared by the collector and the generator, and write
# it into .env as OTEL_JWT_SECRET_B64.
#
#   scripts/gen-secret.sh [--force]
#
# 64 random bytes, base64. That length matters: jjwt sizes the signature
# algorithm to the key, and the generator signs HS512 at >= 64 bytes. Rotating
# the secret requires redeploying the collector — the two must match.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

ENV_FILE="$PROJECT_ROOT/.env"
[ -f "$ENV_FILE" ] || die "no .env — run scripts/setup.sh first"

if grep -q '^OTEL_JWT_SECRET_B64=.\+' "$ENV_FILE" && [ "$FORCE" -eq 0 ]; then
  warn "jwt secret" "already set; use --force to rotate (requires redeploying the collector)"
  exit 0
fi

SECRET="$(openssl rand -base64 64 | tr -d '\n')"

tmp="$(mktemp)"
grep -v '^OTEL_JWT_SECRET_B64=' "$ENV_FILE" > "$tmp" || true
printf 'OTEL_JWT_SECRET_B64=%s\n' "$SECRET" >> "$tmp"
mv "$tmp" "$ENV_FILE"
chmod 0600 "$ENV_FILE"

pass "jwt secret" "64 bytes written to .env (redeploy the collector to apply)"
