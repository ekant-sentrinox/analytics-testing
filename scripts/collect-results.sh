#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Gather everything about one run into results/<test-id>/.
#
#   scripts/collect-results.sh <test-id>
#
# Pulls host metrics from all three servers, the slice of pipeline-exporter
# samples belonging to this run, service logs, and catalog/S3 statistics.
# Only the run's slice of each shared log is copied — the offsets recorded by
# run-test.sh make that exact rather than time-based and approximate.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

[ $# -ge 1 ] || die "usage: collect-results.sh <test-id>"
TEST_ID="$1"

load_env
RUN_DIR="$PROJECT_ROOT/results/$TEST_ID"
[ -d "$RUN_DIR" ] || die "no such run: $RUN_DIR"
mkdir -p "$RUN_DIR/raw" "$RUN_DIR/logs"

slice_from_offset() {           # slice_from_offset <src> <offsetfile> <dst>
  local src="$1" off_file="$2" dst="$3" off=0
  [ -f "$src" ] || { warn "slice" "$src missing"; return; }
  [ -f "$off_file" ] && off="$(cat "$off_file" 2>/dev/null || echo 0)"
  tail -n "+$((off + 1))" "$src" > "$dst"
  pass "collected" "$(basename "$dst") ($(wc -l < "$dst") samples)"
}

# --- SERVER 1 -------------------------------------------------------------------
info "SERVER 1 (generator)"
slice_from_offset "$PROJECT_ROOT/logs/pipeline.jsonl" \
                  "$RUN_DIR/.pipeline-offset" "$RUN_DIR/raw/pipeline.jsonl"
slice_from_offset "$PROJECT_ROOT/logs/host-generator.jsonl" \
                  "$RUN_DIR/.host-generator-offset" "$RUN_DIR/raw/host-generator.jsonl"

# --- SERVER 2 and 3 ---------------------------------------------------------------
for spec in "collector:$COLLECTOR_SSH_HOST:bench-collector:/opt/analytics-bench/collector" \
            "compactor:$COMPACTOR_SSH_HOST:bench-compactor:/opt/analytics-bench/compactor"; do
  IFS=: read -r role host unit base <<< "$spec"
  echo; info "SERVER ${role} ($host)"

  off="$(cat "$RUN_DIR/.host-$role-offset" 2>/dev/null || echo 0)"
  if remote "$host" "tail -n +$((off + 1)) /opt/analytics-bench/agent/logs/host-$role.jsonl 2>/dev/null" \
      > "$RUN_DIR/raw/host-$role.jsonl" 2>/dev/null && [ -s "$RUN_DIR/raw/host-$role.jsonl" ]; then
    pass "collected" "host-$role.jsonl ($(wc -l < "$RUN_DIR/raw/host-$role.jsonl") samples)"
  else
    warn "collected" "no host metrics from $host — was the agent running?"
  fi

  # Service log tail. The whole file can be large after a soak; the tail is what
  # is diagnostic, and the full file stays on the server.
  if remote "$host" "tail -n 5000 $base/logs/${role}.log 2>/dev/null" \
      > "$RUN_DIR/logs/$role.log" 2>/dev/null && [ -s "$RUN_DIR/logs/$role.log" ]; then
    pass "collected" "$role.log ($(wc -l < "$RUN_DIR/logs/$role.log") lines)"
  else
    warn "collected" "no service log from $host"
  fi

  # Errors and warnings, extracted so they are impossible to miss in the report.
  grep -Ei 'ERROR|WARN|Exception|OutOfMemory|RESOURCE_EXHAUSTED' \
    "$RUN_DIR/logs/$role.log" 2>/dev/null > "$RUN_DIR/logs/$role.errors.log" || true
  n="$(wc -l < "$RUN_DIR/logs/$role.errors.log" 2>/dev/null || echo 0)"
  [ "$n" -gt 0 ] && warn "$role errors" "$n error/warning lines — see logs/$role.errors.log" \
                 || pass "$role errors" "none"

  # systemd's own view: restarts are a result and must be recorded.
  remote "$host" "systemctl show $unit -p NRestarts -p ActiveState -p SubState \
                  -p ExecMainStartTimestamp -p MemoryCurrent 2>/dev/null" \
    > "$RUN_DIR/raw/$role-unit.txt" 2>/dev/null || true
  restarts="$(grep -oP 'NRestarts=\K\d+' "$RUN_DIR/raw/$role-unit.txt" 2>/dev/null || echo '?')"
  if [ "$restarts" = "0" ]; then
    pass "$role restarts" "0"
  else
    warn "$role restarts" "$restarts — a restart during a measured run invalidates the level"
  fi

  # GC log, if the JVM wrote one.
  remote "$host" "tail -n 2000 $base/logs/gc.log 2>/dev/null" \
    > "$RUN_DIR/logs/$role-gc.log" 2>/dev/null || true
  [ -s "$RUN_DIR/logs/$role-gc.log" ] && pass "collected" "$role-gc.log" || true
done

# --- compaction events out of the compactor log -------------------------------------
# The compactor uses a LoggingMeterRegistry, so its timers exist only as log
# lines. Parse them into JSONL so the report can treat them as data.
if [ -s "$RUN_DIR/logs/compactor.log" ]; then
  "$(python_bin)" "$PROJECT_ROOT/bench/parse_compactor_log.py" \
    --input "$RUN_DIR/logs/compactor.log" \
    --output "$RUN_DIR/raw/compaction.jsonl" 2>/dev/null \
    && pass "parsed" "compaction.jsonl ($(wc -l < "$RUN_DIR/raw/compaction.jsonl") events)" \
    || warn "parsed" "could not extract compaction events from the log"
fi

# --- final catalog and S3 state --------------------------------------------------------
echo; info "catalog and storage"
"$PROJECT_ROOT/scripts/snapshot-state.sh" > "$RUN_DIR/state-collected.json" 2>/dev/null \
  && pass "collected" "state-collected.json" || warn "collected" "state snapshot failed"

# --- docker, if the monitoring stack is up ------------------------------------------------
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker stats --no-stream --format '{{json .}}' > "$RUN_DIR/raw/docker-stats.jsonl" 2>/dev/null || true
  [ -s "$RUN_DIR/raw/docker-stats.jsonl" ] && pass "collected" "docker-stats.jsonl" \
                                           || skip "docker stats" "no containers"
else
  skip "docker stats" "docker unavailable"
fi

# --- environment ---------------------------------------------------------------------------
"$PROJECT_ROOT/scripts/collect-env.sh" > "$RUN_DIR/environment.json" 2>/dev/null \
  && pass "collected" "environment.json" || warn "collected" "environment capture failed"

echo
info "results in $RUN_DIR"
du -sh "$RUN_DIR" 2>/dev/null | sed 's/^/  /'
summary
