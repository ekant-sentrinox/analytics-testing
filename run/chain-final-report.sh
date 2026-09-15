#!/usr/bin/env bash
# After the campaign AND track A finish: refresh all reports, including the
# customer-facing one, so results/report/ always holds the final numbers.
set -uo pipefail
cd /home/ec2-user/analytics-distributed-test
while ! ls run/bench-job-*chain-track-a*.done >/dev/null 2>&1; do sleep 120; done
./scripts/generate-report.sh --all >/dev/null 2>&1 || true
./.venv/bin/python bench/customer_report.py
echo "final reports regenerated at $(date -u +%FT%TZ)"
