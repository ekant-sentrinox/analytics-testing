#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Give SERVER 1 key-based SSH to SERVER 2 and SERVER 3.
#
#   scripts/setup-ssh.sh            print what is missing, change nothing
#   scripts/setup-ssh.sh --apply    create the key and install it
#
# SERVER 1 is the control node: deploy-*.sh, collect-results.sh, health-check.sh
# and the recovery tests all reach the other two over SSH. Out of the box
# SERVER 1 has an authorized_keys but no private key of its own, so none of that
# works.
#
# This creates a DEDICATED keypair (~/.ssh/id_bench_ed25519) rather than reusing
# an operator key, and APPENDS the public half to authorized_keys on 2 and 3. It
# never rewrites authorized_keys, never touches an existing key, and is
# idempotent — a second run is a no-op.
#
# --apply needs an existing way in to servers 2 and 3 from wherever you run it:
# either SERVER 1 already has some working credential, or you run the printed
# install line yourself from a machine that does.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

load_env

KEY="$HOME/.ssh/id_bench_ed25519"
COMMENT="analytics-distributed-test control node $(hostname -s)"

if [ ! -f "$KEY" ]; then
  if [ "$APPLY" -eq 0 ]; then
    warn "control key" "absent — would create $KEY"
  else
    info "creating $KEY"
    ssh-keygen -t ed25519 -N '' -C "$COMMENT" -f "$KEY" >/dev/null
    chmod 0600 "$KEY"
    pass "control key" "created $KEY"
  fi
else
  pass "control key" "$KEY already present"
fi

[ -f "$KEY.pub" ] || { warn "control key" "no public key yet; re-run with --apply"; exit 0; }
PUB="$(cat "$KEY.pub")"

for target in "$COLLECTOR_SSH_HOST" "$COMPACTOR_SSH_HOST"; do
  label="ssh -> $target"
  if ssh -n -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -i "$KEY" "${SSH_USER}@${target}" true 2>/dev/null; then
    pass "$label" "already working"
    continue
  fi

  if [ "$APPLY" -eq 0 ]; then
    warn "$label" "not authorised"
    echo "        to authorise, run this from a host that can already reach $target:"
    echo "        ssh ${SSH_USER}@${target} \"mkdir -p ~/.ssh && chmod 700 ~/.ssh && \\"
    echo "          grep -qxF '$PUB' ~/.ssh/authorized_keys 2>/dev/null || \\"
    echo "          echo '$PUB' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys\""
    continue
  fi

  # Append-only. A duplicate line is skipped; nothing existing is removed.
  if ssh -n -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "${SSH_USER}@${target}" \
      "mkdir -p ~/.ssh && chmod 700 ~/.ssh &&
       grep -qxF '$PUB' ~/.ssh/authorized_keys 2>/dev/null ||
       echo '$PUB' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys" 2>/dev/null
  then
    if ssh -n -o ConnectTimeout=5 -o BatchMode=yes -i "$KEY" "${SSH_USER}@${target}" true 2>/dev/null; then
      pass "$label" "authorised"
    else
      fail "$label" "key installed but login still refused"
    fi
  else
    fail "$label" "could not reach $target to install the key — do it manually (see above)"
  fi
done

# Point .env at the control key so every other script picks it up.
if [ -f "$KEY" ] && ! grep -q "^SSH_KEY=$KEY\$" "$PROJECT_ROOT/.env"; then
  if [ "$APPLY" -eq 1 ]; then
    tmp="$(mktemp)"
    grep -v '^SSH_KEY=' "$PROJECT_ROOT/.env" > "$tmp" || true
    printf 'SSH_KEY=%s\n' "$KEY" >> "$tmp"
    mv "$tmp" "$PROJECT_ROOT/.env"
    chmod 0600 "$PROJECT_ROOT/.env"
    pass ".env SSH_KEY" "$KEY"
  else
    warn ".env SSH_KEY" "would be set to $KEY"
  fi
fi

summary
