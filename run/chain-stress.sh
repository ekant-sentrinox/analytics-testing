#!/usr/bin/env bash
# PEAK test — escalate until something gives, after all other testing is done
# (so an induced overload cannot pollute the measurement runs).
set -uo pipefail
cd /home/ec2-user/analytics-distributed-test
while ! ls run/bench-job-*chain-track-a*.done >/dev/null 2>&1; do sleep 120; done
sleep 120   # let the box go quiet after track A

# Escalate 5k -> 40k rec/s, 3 min holds. Stops safely at the first of:
# throughput plateau, RESOURCE_EXHAUSTED, error rate > 5%, p99 > 60s, or a
# component going down. Attribution built in: if the generator or the SSH
# tunnel is the binding constraint, it says so instead of calling it a
# pipeline ceiling.
./tests/stress/run.sh --start 5000 --step 5000 --max 40000 --hold 180 2>&1

# fold the new numbers into the reports
./scripts/generate-report.sh --all >/dev/null 2>&1 || true
./.venv/bin/python bench/customer_report.py
echo "peak test + report refresh done at $(date -u +%FT%TZ)"
