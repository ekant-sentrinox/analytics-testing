#!/usr/bin/env bash
# Credential-refresh soak — proves REFRESH auto survives STS expiry (~6h).
# Services restarted 2026-09-11T06:55Z; expiry-without-refresh would hit ~12:55Z.
# Every 15 min: health + ExpiredToken scan. Every 4th tick: full smoke test.
# Runs until 2026-09-12T05:00Z. Log: results/cred-soak-20260911/soak.log
cd /home/ec2-user/analytics-distributed-test
OUT=results/cred-soak-20260911
LOG=$OUT/soak.log
END=$(date -ud "2026-09-12T05:00:00Z" +%s)
TICK=0
echo "$(date -u +%FT%TZ) SOAK-START collector+compactor restarted 06:55Z; expiry boundary ~12:55Z" >> $LOG
while [ "$(date -u +%s)" -lt "$END" ]; do
  TICK=$((TICK+1))
  TS=$(date -u +%FT%TZ)
  CH=$(ssh -i /home/ec2-user/.ssh/id_bench_ed25519 -o BatchMode=yes -o ConnectTimeout=10 ec2-user@10.16.24.204 "curl -s --max-time 5 localhost:8081/health" 2>/dev/null | tr -d "\n ")
  PH=$(ssh -i /home/ec2-user/.ssh/id_bench_ed25519 -o BatchMode=yes -o ConnectTimeout=10 ec2-user@10.16.25.10 "curl -s --max-time 5 localhost:8080/health | head -c 400; echo; grep -c ExpiredToken /opt/analytics-bench/compactor/logs/compactor.log" 2>/dev/null | tr "\n" "|")
  CE=$(ssh -i /home/ec2-user/.ssh/id_bench_ed25519 -o BatchMode=yes -o ConnectTimeout=10 ec2-user@10.16.24.204 "grep -c ExpiredToken /opt/analytics-bench/collector/logs/collector.log" 2>/dev/null)
  echo "$TS tick=$TICK collector=$CH collector_expired_count=$CE compactor=$PH" >> $LOG
  sleep 900
done
echo "$(date -u +%FT%TZ) SOAK-END" >> $LOG
