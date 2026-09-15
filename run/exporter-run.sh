#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# SERVER 1 — pipeline exporter launcher. Rendered by scripts/deploy-agent.sh.
# Invoked by systemd (bench-exporter.service).
#
# Exists for the same reason the collector's wrapper does: the catalog password
# is read here from the file that already holds it and exported, so it is never
# copied into a config or a systemd EnvironmentFile.
#
# Health mode is `ssh`: this process reads each host's local agent snapshot over
# port 22 rather than scraping 8080/8081 across the network. That is what keeps
# those ports closed in the security group.
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="/home/ec2-user/analytics-distributed-test"
cd "$PROJECT_DIR"

if [ ! -r "/home/ec2-user/.config/sentrinox/analytics-perf-catalog.pw" ]; then
  echo "fatal: catalog password file not readable: /home/ec2-user/.config/sentrinox/analytics-perf-catalog.pw" >&2
  exit 78
fi
PG_PASSWORD="$(tr -d '\r\n' < "/home/ec2-user/.config/sentrinox/analytics-perf-catalog.pw")"
export PG_PASSWORD

export COLLECTOR_SSH_HOST="10.16.24.204"
export COMPACTOR_SSH_HOST="10.16.25.10"
export PG_HOST="analytics-perf-catalog.ctu62c8oii9y.us-west-2.rds.amazonaws.com"
export PG_PORT="5432"
export PG_DATABASE="bench_catalog"
export PG_USER="analytics"
export S3_BUCKET="sentri-analytics-performance-test"
export S3_PREFIX="bench"
export AWS_REGION="us-west-2"
export DUCKLAKE_CATALOG="bench"
export DUCKLAKE_SCHEMA="main"
export DUCKLAKE_WATERMARK_TABLE="ingest_watermark"
export HOME="/home/ec2-user"

exec "$PROJECT_DIR/.venv/bin/python" \
  "$PROJECT_DIR/monitoring/exporters/pipeline_exporter.py" \
  --health-mode ssh \
  --ssh-user "ec2-user" \
  --ssh-key "/home/ec2-user/.ssh/id_bench_ed25519" \
  --port "9101" \
  --jsonl "$PROJECT_DIR/logs/pipeline.jsonl"
