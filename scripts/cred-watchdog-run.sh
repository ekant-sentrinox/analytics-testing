#!/usr/bin/env bash
cd /home/ec2-user/analytics-distributed-test
./scripts/cred-watchdog-generic.sh bench-collector 10.16.24.204 /opt/analytics-bench/collector/logs/collector.log
./scripts/cred-watchdog-generic.sh bench-compactor 10.16.25.10 /opt/analytics-bench/compactor/logs/compactor.log
