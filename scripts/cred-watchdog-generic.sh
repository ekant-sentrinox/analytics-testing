#!/usr/bin/env bash
# Generic S3-credential-expiry watchdog for the DazzleDuck benchmark services.
#
#   cred_watchdog.sh <service> <host> <logfile>
#
# Restarts <service> on <host> if its log shows an ExpiredToken error timestamped
# after the service's current run started. Reads the timestamp from
# `systemctl show ActiveEnterTimestamp` each time it runs, so it is safe to call
# repeatedly (e.g. from a timer every few minutes) without tracking state between
# runs, and never reacts to a stale ExpiredToken line from a previous run that
# the restart already resolved.
#
# Root cause (documented, not fixed here): the collector/compactor startup SQL
# creates the S3 secret with `PROVIDER credential_chain, REFRESH auto`, which
# does not actually re-fetch credentials before the underlying IAM role's
# temporary credentials expire (~6h). This is a restart-based stopgap, not a fix
# for that underlying DuckDB/aws-extension behaviour.
set -u
SERVICE="$1"; HOST="$2"; LOGFILE="$3"
KEY=/home/ec2-user/.ssh/id_bench_ed25519
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o BatchMode=yes ec2-user@$HOST"

[ -n "$SERVICE" ] && [ -n "$HOST" ] && [ -n "$LOGFILE" ] || {
  echo "usage: $0 <service> <host> <logfile>" >&2; exit 1; }

START_RAW=$($SSH "systemctl show '$SERVICE' -p ActiveEnterTimestamp --value" 2>/dev/null)
[ -n "$START_RAW" ] || { echo "$(date -u +%FT%TZ) $SERVICE: could not read start time, skipping" >&2; exit 0; }
START_EPOCH=$(date -d "$START_RAW" +%s 2>/dev/null) || exit 0

RESTART_NEEDED=0
while IFS= read -r line; do
  ts=$(echo "$line" | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}')
  [ -n "$ts" ] || continue
  ep=$(date -d "$ts" +%s 2>/dev/null) || continue
  if [ "$ep" -ge "$START_EPOCH" ]; then RESTART_NEEDED=1; break; fi
done < <($SSH "grep ExpiredToken '$LOGFILE' 2>/dev/null | tail -20")

if [ "$RESTART_NEEDED" -eq 1 ]; then
  echo "$(date -u +%FT%TZ) $SERVICE on $HOST: ExpiredToken since current run started ($START_RAW) — restarting"
  $SSH "sudo systemctl restart '$SERVICE'"
  echo "$(date -u +%FT%TZ) $SERVICE on $HOST: restart issued"
fi
