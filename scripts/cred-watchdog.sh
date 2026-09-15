#!/usr/bin/env bash
# Session-independent credential watchdog: if a service logs a NEW ExpiredToken,
# restart it (the documented <6h stopgap for IMPROVEMENTS #16). Runs until 05:30Z.
cd /home/ec2-user/analytics-distributed-test
LOG=results/cred-soak-20260911/watchdog.log
END=$(date -ud "2026-09-12T05:30:00Z" +%s)
CB=0; PB=684
while [ "$(date -u +%s)" -lt "$END" ]; do
  CE=$(ssh -i /home/ec2-user/.ssh/id_bench_ed25519 -o BatchMode=yes -o ConnectTimeout=10 ec2-user@10.16.24.204 "grep -c ExpiredToken /opt/analytics-bench/collector/logs/collector.log" 2>/dev/null || echo "$CB")
  PE=$(ssh -i /home/ec2-user/.ssh/id_bench_ed25519 -o BatchMode=yes -o ConnectTimeout=10 ec2-user@10.16.25.10 "grep -c ExpiredToken /opt/analytics-bench/compactor/logs/compactor.log" 2>/dev/null || echo "$PB")
  if [ "$CE" -gt "$CB" ] 2>/dev/null; then
    echo "$(date -u +%FT%TZ) collector ExpiredToken $CB->$CE — restarting" >> $LOG
    ./scripts/deploy-collector.sh --no-build >/dev/null 2>&1; CB=$CE
  fi
  if [ "$PE" -gt "$PB" ] 2>/dev/null; then
    echo "$(date -u +%FT%TZ) compactor ExpiredToken $PB->$PE — restarting" >> $LOG
    ./scripts/deploy-compactor.sh --no-build >/dev/null 2>&1; PB=$PE
  fi
  sleep 120
done
echo "$(date -u +%FT%TZ) watchdog exit" >> $LOG
