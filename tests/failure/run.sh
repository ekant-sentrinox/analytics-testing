#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# FAILURE — the deliberate fault matrix.
#
#   tests/failure/run.sh [fault ...]        run the named faults
#   tests/failure/run.sh --list             show what is available and what is not
#   tests/failure/run.sh --all              every fault that is safe here
#
# Every fault is scoped to bench-collector / bench-compactor on the two
# configured hosts. Several faults from the benchmark spec are deliberately NOT
# implemented on this environment; --list explains each one rather than leaving
# a silent gap in the matrix.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")/../.." && pwd)/scripts/lib/common.sh"

load_env

show_list() {
  cat <<'LIST'

  IMPLEMENTED
    collector-sigterm   Graceful stop under load. Expect MAINTENANCE on /health,
                        a clean drain, and ZERO loss.
    collector-sigkill   Hard kill mid-flush. Measures records lost, duplicates
                        on restart, and recovery time.
    compactor-sigkill   Kill mid-merge. Expect orphaned Parquet on S3, no
                        catalog corruption, compaction resumes.
    memory-pressure     Re-run the collector with a low MemoryMax cgroup limit
                        and drive load until it is OOM-killed. Records the
                        limit, the kill, and whether data was lost.
    disk-pressure       Fill SERVER 2's root filesystem to ~95% and observe how
                        the collector fails. Checks the failure is a clean
                        rejection, not corruption.

  NOT IMPLEMENTED HERE — and why
    catalog-unavailable Would need to stop PostgreSQL. The catalog is a SHARED
                        RDS instance with other databases on it (bench_150k,
                        bench_fixture, bench_meta, ollylake_meta). Stopping it
                        would affect work that is not ours. Run this against a
                        dedicated instance.
    storage-latency     `tc netem` against S3 would need a proxy or a route we
                        control; S3 is reached directly over the AWS network.
                        Feasible with a local MinIO, not with real S3.
    network-partition   Same reason: the security group and route table are not
                        writable from the instance role.

LIST
}

[ $# -eq 0 ] && { show_list; exit 0; }
[ "$1" = "--list" ] && { show_list; exit 0; }

FAULTS=()
if [ "$1" = "--all" ]; then
  FAULTS=(collector-sigterm collector-sigkill compactor-sigkill)
  warn "note" "--all omits memory-pressure and disk-pressure; run those explicitly"
else
  FAULTS=("$@")
fi

CAMPAIGN="$(new_test_id failures)"
CAMPAIGN_DIR="$PROJECT_ROOT/results/$CAMPAIGN"
mkdir -p "$CAMPAIGN_DIR"
info "failure campaign $CAMPAIGN — ${#FAULTS[@]} fault(s)"
echo

for fault in "${FAULTS[@]}"; do
  echo "=============================================================="
  info "fault: $fault"
  echo "=============================================================="
  case "$fault" in
    collector-sigterm|collector-sigkill|compactor-sigkill)
      "$PROJECT_ROOT/tests/recovery/run.sh" "$fault" --rps 2000 --duration 600 --at 240 \
        2>&1 | tee "$CAMPAIGN_DIR/$fault.txt"
      ;;
    memory-pressure)
      "$PROJECT_ROOT/tests/failure/memory_pressure.sh" 2>&1 | tee "$CAMPAIGN_DIR/$fault.txt"
      ;;
    disk-pressure)
      "$PROJECT_ROOT/tests/failure/disk_pressure.sh" 2>&1 | tee "$CAMPAIGN_DIR/$fault.txt"
      ;;
    catalog-unavailable|storage-latency|network-partition)
      warn "$fault" "NOT TESTED — see tests/failure/run.sh --list for the reason"
      echo "NOT TESTED — see --list" > "$CAMPAIGN_DIR/$fault.txt"
      ;;
    *)
      fail "$fault" "unknown fault"
      ;;
  esac
  echo
  info "letting the pipeline settle before the next fault"
  sleep 60
done

echo
info "campaign artifacts: $CAMPAIGN_DIR"
ls -1 "$CAMPAIGN_DIR" | sed 's/^/  /'
