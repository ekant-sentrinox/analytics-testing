# Compactor (SERVER 3 — 10.16.25.10)

`dazzleduck-sql-ducklake-compactor`. **A remote service** — runs on SERVER 3
under systemd. This directory holds its config templates and deploy script.

```
compactor/config/
├── application.conf.tmpl           HOCON — loaded from the CLASSPATH, see below
├── simplelogger.properties         NOT logback; see below
├── run.sh.tmpl
└── bench-compactor.service.tmpl
```

## Deploy

```bash
../scripts/deploy-compactor.sh
../scripts/deploy-compactor.sh --no-build
```

## On-host layout

```
/opt/analytics-bench/compactor/
├── conf/application.conf           0600
├── conf/simplelogger.properties
├── bin/run.sh
├── logs/compactor.log  logs/gc.log
└── run/
/etc/systemd/system/bench-compactor.service
/var/tmp/duckdb-compactor
```

```bash
systemctl status bench-compactor
curl -s localhost:8080/health | python3 -m json.tool
```

## What it does

```sql
CALL ducklake_merge_adjacent_files('bench', max_file_size := 8388608);   -- minor,  1 min
CALL ducklake_merge_adjacent_files('bench', max_file_size := 67108864);  -- major, 10 min
CALL ducklake_expire_snapshots('bench', ...);                            -- housekeeping, 5 min
CALL ducklake_cleanup_old_files('bench', ...);                           -- housekeeping
```

Minor and major differ only in `max_file_size` and in schedule.

## Three things that will trip you up

### 1. There is no `-c` flag

`CompactionConfig.rawConfig()` calls `ConfigFactory.load("application")` — it
reads `application.conf` off the **classpath**, not from a path you pass it.
`bin/run.sh` puts `conf/` first so it shadows the copy baked into the module:

```
CP="$BASE_DIR/conf:$REPO_DIR/$MODULE/target/classes:$REPO_DIR/$MODULE/target/lib/*"
```

Only `--conf key=value` overrides work on the command line.

### 2. It uses slf4j-simple, not logback

The dependency tree contains `slf4j-simple` and **no** `logback-classic`, so a
`logback.xml` on the classpath is silently ignored. `simplelogger.properties` is
what it reads.

This is not cosmetic. The compactor publishes its Micrometer meters through a
`LoggingMeterRegistry` — **its metrics ARE log lines**:

```
2026-09-10T11:01:56.469Z [logging-metrics-publisher] [INFO] LoggingMeterRegistry -
  ducklake.compaction.duration{database=bench,step=merge,type=minor}
  throughput=0.016667/s mean=0.024414737s max=0.027184873s
```

slf4j-simple prints **no timestamp** by default, which turns those measurements
into a bag of numbers with no time axis. `simplelogger.properties` enables
`showDateTime` in UTC, and `bench/parse_compactor_log.py` turns the result into
`raw/compaction.jsonl`.

### 3. A major run *replaces* the minor run for that tick

In `CompactionService.runCompaction`, when a major run is due it runs **instead
of** that tick's minor run, not in addition to it. A single compactor therefore
never produces concurrent minor and major activity.

Any report claiming to have measured "concurrent minor + major compaction" from
one compactor instance is wrong. To get genuine concurrency:

| | |
|---|---|
| **C1** | two catalogs, one compactor each, staggered — closest to real multi-tenant |
| **C2** | two compactors on one catalog with different sizes and frequencies — also exercises concurrent-writer conflicts |
| **C3** | single compactor, sequential — **what is deployed here**, and what the report says |

## Config

| | default | notes |
|---|---|---|
| `minor_compaction_frequency` | 1 minute | |
| `major_compaction_frequency` | 10 minutes | shortened from the module default of 1 hour so a 10-min staircase step contains several cycles |
| `housekeeping_frequency` | 5 minutes | |
| `snapshot_retention` | 15 minutes | |
| `minor_compaction_max_size` | 8 MB | merge candidates below this |
| `major_compaction_max_size` | 64 MB | |
| DuckDB threads / memory / temp | 2 / 2GB / `/var/tmp/duckdb-compactor` | |

> **A sizing interaction worth understanding before reading the numbers.**
> `minor_compaction_max_size` (8 MiB) sits *below* the collector's
> `min_bucket_size` (16 MiB), so which compaction tier does the work depends on
> the ingest rate:
>
> - **At low rates** the bucket flushes on `max_delay_ms` (5 s) long before it
>   reaches 16 MiB. At 1000 rec/s with ~500-byte records that is roughly 2.5 MiB
>   per file — well under 8 MiB, so **minor compaction does the work**.
> - **At high rates** the bucket fills to `min_bucket_size` first and every file
>   is ~16 MiB, above the minor threshold. Minor compaction then finds nothing:
>   `totalMinorCompactions` climbs while `totalFilesCompacted` stays 0, and
>   **major compaction (64 MiB) does all the merging**.
>
> Neither is wrong, but the crossover means the compaction tier under test
> changes with the offered rate — worth stating explicitly when comparing
> levels. If you want minor compaction exercised at high rates, raise
> `minor_compaction_max_size` above `min_bucket_size`.

If you lengthen staircase steps, lengthen these frequencies in proportion.

## Health

```json
{
  "status": "UP",
  "uptime": "PT13M30.6S",
  "databases": {
    "bench": {
      "totalMinorCompactions": 13, "totalMajorCompactions": 1,
      "totalFilesCompacted": 0,
      "lastExecutionTime": "...", "nextExecutionTime": "...",
      "currentSmallFiles": 0, "currentMediumFiles": 0, "currentTotalFiles": 0
    }
  }
}
```

Polled every 10 s by the pipeline exporter. Merge **durations** are not here —
they only exist in the log, hence the parser.

## Shutdown

`SIGTERM`, `TimeoutStopSec=120`. A merge killed part-way leaves
written-but-unregistered Parquet on S3 — orphans, not corruption, because the
merge commits atomically. `ducklake_cleanup_old_files` reclaims them on the
housekeeping timer. `tests/recovery/run.sh compactor-sigkill` verifies exactly
that.
