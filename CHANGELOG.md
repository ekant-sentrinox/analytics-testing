# Changelog

## 0.1.0 — 2026-09-10

First working version. Built and verified against the live three-server
environment; nothing below is aspirational.

### Environment brought up

- DuckLake catalog created on the existing RDS instance
  (`bench_catalog` on `analytics-perf-catalog`, PostgreSQL 18.4, master user
  `analytics`), with `bench.main.logs` and `bench.main.ingest_watermark`.
- Data path on the existing bucket, `s3://sentri-analytics-performance-test/bench/`,
  authenticated by the EC2 instance role. Read/write verified from all three
  hosts and from DuckDB's own `credential_chain` path.
- SERVER 3 provisioned from bare: Java 21, Maven, Git, PostgreSQL client, the
  repo checkout, and the compactor built.
- Both services deployed under systemd into `/opt/analytics-bench/`, with
  `conf/ bin/ logs/ run/`, restart policy, graceful-stop timeouts and log
  rotation. Both confirmed healthy.
- Dedicated SSH keypair for SERVER 1 as control node, appended to
  `authorized_keys` on 2 and 3.

### Built

- **Generator** — open-loop, rate-controlled OTLP/gRPC client. Token-bucket
  pacer, bounded in-flight with explicit stall accounting, HS512 JWT with the
  `x-dd-ingestion-queue` claim, per-record `bench.gen_id` / `bench.seq`,
  deterministic seeded payloads, staircase mode, three-way rate accounting,
  1 Hz JSONL + CSV, `manifest.json`, Prometheus endpoint. 19 unit tests.
- **Pipeline exporter** — the only Prometheus surface for the compactor, the
  catalog backlog (B2), visibility lag (B3), PostgreSQL health and S3 growth.
- **Host agent** — 1 s `/proc` sampling on all three hosts: CPU, memory, disk
  I/O and free space, network, watched-process RSS, JVM heap, restarts, OOM
  kills. No third-party dependencies, so it runs on a bare host.
- **Correctness validator** — reconciles landed rows against the generator
  manifest: loss, duplication, sequence gaps, watermark agreement, and
  per-column checksums for before/after a merge.
- **Report generator** — `report.md` plus up to eleven charts from a run's
  files, with a named source for every number and `NOT MEASURED` where an input
  is absent.
- **Compactor log parser** — the compactor's meters are log lines; this turns
  them into a time series.
- 18 scripts (setup, deploy, preflight, connectivity, health, start/stop/
  restart/status/reset/cleanup, run-test, collect, validate, report).
- 8 test scenarios: smoke, functional, throughput staircase, soak, drain,
  stress, recovery, failure matrix.
- Prometheus + Grafana stack with 7 generated dashboards and alert rules that
  encode the sustainability criteria.
- 11 documentation files.

### Fixed while building

- **Collector logged at DEBUG** — gRPC and Netty produced 42 KB before a single
  record arrived. Added `logback.xml` at INFO with a non-blocking async appender
  that drops rather than stalling the ingest path.
- **Compactor log had no timestamps** — its dependency tree has slf4j-simple and
  no logback, so `logback.xml` was silently ignored. Its `LoggingMeterRegistry`
  output *is* its metrics, so this left the merge durations with no time axis.
  Added `simplelogger.properties`.
- **DuckDB version split** — the host CLI is 1.5.2 while the server's JDBC
  driver is 1.5.4.0, and the DuckLake metadata schema is version-dependent.
  Every SQL path now goes through the venv's pinned `duckdb==1.5.4`.
- **`DATA_INLINING_ROW_LIMIT 0`** — without it DuckLake kept small commits as
  rows inlined in PostgreSQL and wrote no Parquet at all. Observed directly: a
  probe insert produced zero rows in `ducklake_data_file`.
- **Generator template race** — workers shared a template pool while `stamp()`
  mutates in place, so two threads could tag records with each other's sequence
  numbers, silently breaking the correctness check. Templates are now owned per
  worker, with a unit test.
- **`event_name`** — set by the generator but absent from the proto version
  either side is built against. Removed; the collector hardcodes the column to
  NULL for the same reason.
- **DuckDB `temp_directory`** — `/tmp` is tmpfs on Amazon Linux 2023 and there
  is no swap, so a spill there converts a disk spill into an OOM kill. Pointed
  at `/var/tmp`.
- Step-drain now waits on in-flight RPCs, not just the queue, so a level's
  accepted count is not smeared into the next level.
- `PROBE_MS` lost across a command substitution in `connectivity-check.sh`;
  `KeyCount` dropped by the AWS CLI's auto-pagination in `check-s3.sh`.

### Known limitations

1. **Security group `sg-0ea9dd40012388877` allows port 22 only.** Ports 4317,
   8080, 8081 are dropped, so the generator cannot reach the collector and no
   Track B measurement is possible. Both services are healthy on their own
   loopbacks. Rules to add: [NETWORK.md](NETWORK.md). The instance role has no
   `ec2:*` permissions.
2. **B1 (collector queue depth) is unmeasurable** — `SimpleMeterRegistry`, no
   exporter.
3. **`data_phase_ms` vs `post_ingest_phase_ms` is unmeasurable** — the one
   diagnostic that would say whether DuckDB or the catalog is the constraint.
4. Track A (engine-only) sweeps not implemented; matrix is in
   `config/duckdb.yaml`.
5. 100M/500M-row datasets and 100 GB–1 TB compaction tiers do not fit on 8 GiB
   root volumes — `NOT TESTED`, not estimated.
6. Three faults (catalog-unavailable, storage-latency, network-partition) are
   not implemented here; `tests/failure/run.sh --list` gives the reason for
   each.
7. Concurrent minor + major compaction is not possible with one compactor — a
   major run replaces the minor tick. Reported as sequential (C3).

### Notes

- Findings for the upstream project: [IMPROVEMENTS.md](IMPROVEMENTS.md).
- No secrets are committed. `.env`, rendered configs and `*.pw` are gitignored;
  S3 auth is the instance role; the catalog password is read from a file at
  process start and never copied beside a config.
- Nothing is pushed anywhere. `git init` only.
