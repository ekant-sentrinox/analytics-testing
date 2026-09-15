#!/usr/bin/env bash
# Runs after the campaign: Track A engine benchmark on this host (the venv with
# duckdb 1.5.4 lives here, and the generator is idle once the campaign is done).
set -uo pipefail
cd /home/ec2-user/analytics-distributed-test
# wait for the campaign job to write its done marker
while ! ls run/bench-job-*campaign*.done >/dev/null 2>&1; do sleep 60; done
sleep 60   # let the compactor settle
./.venv/bin/python bench/track_a.py --out results/track-a --work /var/tmp/track-a 2>&1
