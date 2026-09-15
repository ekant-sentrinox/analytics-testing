# Architecture

What the pipeline actually does, verified against the deployed services and the
`dazzleduck-sql-server` source at commit `4b48e9f9` — not assumed from the
diagram.

---

## Topology

```
┌──────────────────────────────┐
│ SERVER 1   10.16.21.20       │   i-0257966a73c5176ec   t3.large
│ generator                    │   2 vCPU / 7.6 GiB / 8 GiB root
│                              │
│  generator/src/main.py       │   open-loop OTLP client
│  pipeline_exporter.py        │   :9101 Prometheus
│  host_agent.py               │   1 s /proc sampling
│  prometheus :9090            │   docker
│  grafana    :3000            │   docker
└──────────────┬───────────────┘
               │
               │  OTLP ExportLogsServiceRequest over gRPC, plaintext
               │  :4317
               │  authorization: Bearer <HS512 JWT>
               │  claim x-dd-ingestion-queue = "logs"
               ▼
┌──────────────────────────────┐
│ SERVER 2   10.16.24.204      │   i-0be2c5bc12842b924   t3.large
│ dazzleduck-sql-otel-collector│
│                              │
│  systemd bench-collector     │   /opt/analytics-bench/collector
│  gRPC   :4317                │   JVM -Xmx2g
│  health :8081                │   DuckDB threads=2, memory_limit=2GB
└──────┬──────────────┬────────┘
       │              │
       │ catalog      │ data
       ▼              ▼
┌──────────────┐  ┌────────────────────────────────┐
│ PostgreSQL   │  │ S3                             │
│ RDS 18.4     │  │ sentri-analytics-performance-  │
│ analytics-   │  │ test/bench/                    │
│ perf-catalog │  │                                │
│ db:          │  │ Parquet data files             │
│ bench_catalog│  │ instance role auth             │
│ 29 ducklake_*│  │ analytics-performance-s3-role  │
│ tables       │  │                                │
└──────▲───────┘  └────────────▲───────────────────┘
       │                       │
       └───────────┬───────────┘
                   │
┌──────────────────┴───────────┐
│ SERVER 3   10.16.25.10       │   i-06f213280bc815721   t3.large
│ ducklake-compactor           │
│                              │
│  systemd bench-compactor     │   /opt/analytics-bench/compactor
│  health :8080                │   minor 1 min / major 10 min
│                              │   housekeeping 5 min
└──────────────────────────────┘
```

Both SERVER 2 and SERVER 3 attach the **same** DuckLake catalog and the **same**
S3 data path. They are two concurrent writers against one lake: the collector
registers new files, the compactor replaces them with merged ones. DuckLake's
snapshot model is what makes that safe.

---

## The ingest path, statement by statement

```
OTLP ExportLogsServiceRequest  (gRPC :4317)
  │
  ├─ JwtServerInterceptor
  │    verifies the HS512 signature against otel_collector.secret_key
  │    extracts the x-dd-ingestion-queue claim  (Headers.CLAIM_INGESTION_QUEUE)
  │    no claim        -> INVALID_ARGUMENT   (there is NO default queue)
  │    bad signature   -> UNAUTHENTICATED
  │
  ├─ OtelServiceBase.resolveQueue
  │    claim -> ParquetIngestionQueue, created lazily on first use
  │
  ├─ LogRecordConverter
  │    flattens ResourceLogs → ScopeLogs → LogRecord into flat rows
  │    13 columns; see "Schema" below
  │
  ├─ writeArrowFile
  │    Arrow IPC to a temp file, NO_COMPRESSION
  │
  ├─ ParquetIngestionQueue.add
  │    batch joins a bucket; the bucket flushes when
  │      accumulated bytes >= min_bucket_size   (16 MiB here)
  │      or max_delay_ms elapses                (5000 ms here)
  │
  ├─ DuckDB COPY                     bucket -> ONE Parquet file on S3
  │    ("data phase")
  │
  └─ DuckLake catalog commit         file registered in PostgreSQL
       ("post-ingest phase")         + one watermark row, same transaction
       │
       └─ the export RPC's response completes HERE
```

### Four consequences that shape every number in this harness

**1. The RPC does not ack until the batch is durable.**
`OtelServiceBase` completes the gRPC response from the `addBatch` future
(`batchCompleteHandler`). So client-observed latency is *queue wait + COPY +
catalog commit*, not network time, and "accepted" genuinely means "persisted and
committed". This is why `accepted` is the honest throughput number and why p99
latency is measured in hundreds of milliseconds rather than microseconds.

**2. Backpressure is explicit and typed.**
When pending write bytes exceed `max_pending_write` (500 MiB), the queue throws
`PendingWriteExceededException`, which surfaces as gRPC `RESOURCE_EXHAUSTED`
carrying a `RetryInfo` delay. Counting these is how rejected ingestion is
measured. The generator never silently retries them — `retry_mode: none` by
default, and the mode used is recorded in every manifest.

**3. Flush thresholds decide file size, and file size decides compaction load.**
`min_bucket_size` and `max_delay_ms` set how large each Parquet file is. Small
files mean more of them, which means more compaction work, which means the
compactor becomes the constraint before the collector does. These are the first
knobs to move when the backlog trends up.

**4. Compaction is a DuckLake merge, not generic Parquet rewriting.**
`CALL ducklake_merge_adjacent_files(db, max_file_size := N)`. Minor and major
differ only in `max_file_size` (8 MiB vs 64 MiB) and in schedule. Housekeeping —
`ducklake_expire_snapshots` and `ducklake_cleanup_old_files` — runs on its own
timer.

---

## Data inlining — the setting that makes or breaks the benchmark

Both services attach with:

```sql
ATTACH 'ducklake:postgres:...' AS bench (
    DATA_PATH 's3://sentri-analytics-performance-test/bench/',
    DATA_INLINING_ROW_LIMIT 0          -- required
);
```

Without `DATA_INLINING_ROW_LIMIT 0`, DuckLake keeps small commits as rows
*inlined in the PostgreSQL catalog* instead of writing Parquet to S3. Ingestion
still works and row counts still add up — but no data file is created, the
compactor has nothing to merge, and the benchmark measures a completely
different system while looking correct.

This was observed directly here: an early probe insert produced zero rows in
`ducklake_data_file` and created a `ducklake_inlined_data_1_1` table in
PostgreSQL.

---

## Schema

`OtelLogSchema.SCHEMA` in the collector, mirrored by `sql/catalog_bootstrap.sql.tmpl`:

| column | Arrow | DuckDB |
|---|---|---|
| `timestamp` | Timestamp(MILLISECOND) | `TIMESTAMP_MS` |
| `observed_timestamp` | Timestamp(MILLISECOND) | `TIMESTAMP_MS` |
| `severity_number` | Int32 | `INTEGER` |
| `severity_text` | Utf8 | `VARCHAR` |
| `body` | Utf8 | `VARCHAR` |
| `trace_id` | Utf8 | `VARCHAR` |
| `span_id` | Utf8 | `VARCHAR` |
| `flags` | Int32 | `INTEGER` |
| `event_name` | Utf8 | `VARCHAR` — always NULL, see below |
| `attributes` | Map(Utf8,Utf8) | `MAP(VARCHAR, VARCHAR)` |
| `resource_attributes` | Map(Utf8,Utf8) | `MAP(VARCHAR, VARCHAR)` |
| `scope_name` | Utf8 | `VARCHAR` |
| `scope_version` | Utf8 | `VARCHAR` |

`event_name` is always NULL: `LogRecordConverter` hardcodes it with the comment
*"field added in proto > 1.3.2"*, and neither the Java (`opentelemetry-proto
1.3.2-alpha`) nor the Python (`opentelemetry-proto 1.29.0`) build has the field.
The generator does not set it either.

**All OTLP attribute values become strings.** `attributes` is
`MAP(VARCHAR, VARCHAR)`, so `bench.seq` — emitted as an int64 — arrives as text
and is cast back with `TRY_CAST(...) AS BIGINT` in the correctness validator.

### Watermark table

```sql
CREATE TABLE bench.main.ingest_watermark (
    min_timestamp           TIMESTAMP_MS,
    max_timestamp           TIMESTAMP_MS,
    row_count               BIGINT,
    min_commit_snapshot_id  BIGINT
);
```

One row per flushed bucket, written **in the same transaction as the file
registration**. That gives two things nothing else does: end-to-end visibility
lag (`now() - max(max_timestamp)`), and a transactionally-consistent row count
to reconcile against — an independent witness to what the catalog believes it
accepted.

---

## Per-record identity

Every generated record carries:

- `bench.gen_id` — a UUID per generator process, also on the Resource
- `bench.seq` — monotonic within that generator, contiguous across the run

Without these, "zero data loss" is an assertion. With them it is a query:

```sql
-- duplication
SELECT gen_id, seq, count(*) c FROM run_rows GROUP BY 1,2 HAVING c > 1;
-- gaps
SELECT gen_id, min(seq), max(seq), count(DISTINCT seq) FROM run_rows GROUP BY 1;
```

reconciled against `manifest.json`, which records the exact seq range attempted
and the exact count acked.

```
offered - accepted  = rejected or failed    expected under backpressure, not loss
accepted - landed   = DATA LOSS             hard failure at any throughput
landed - accepted   = DUPLICATION           equally a hard failure
```

---

## Compaction

```sql
CALL ducklake_merge_adjacent_files('bench', max_file_size := 8388608);   -- minor
CALL ducklake_merge_adjacent_files('bench', max_file_size := 67108864);  -- major
CALL ducklake_expire_snapshots('bench', older_than => ...);              -- housekeeping
CALL ducklake_cleanup_old_files('bench', ...);                           -- housekeeping
```

### A scheduler detail that invalidates a common claim

In `CompactionService.runCompaction`, **a due major run replaces that tick's
minor run** — it does not run in addition to it. A single compactor instance
therefore never produces concurrent minor and major activity. Any report
claiming to have measured "concurrent minor + major compaction" from one
compactor is wrong.

To get genuine concurrency you need one of:

- **C1** two catalogs, one compactor each, staggered schedules — closest to a
  real multi-tenant deployment;
- **C2** two compactor processes against one catalog with different sizes and
  frequencies — also exercises concurrent-writer conflict handling;
- **C3** single compactor, sequential — what is deployed here. Reported as such.

This deployment is **C3**.

---

## Backlog — three quantities, never averaged

| | what | where it comes from | status here |
|---|---|---|---|
| **B1** | collector in-memory queue depth | `writer.pending_batches`, `writer.pending_buckets` | **unavailable** — `SimpleMeterRegistry`, no exporter, `/health` exposes only `batchesProcessed` |
| **B2** | uncompacted files in the catalog | `SELECT count(*), sum(file_size_bytes), sum(record_count) FROM ducklake_data_file WHERE end_snapshot IS NULL` | measured, every 10 s |
| **B3** | end-to-end visibility lag | `now() - max(max_timestamp)` from `ingest_watermark` | measured, every 60 s |

B2 is what compaction drains and is the one the sustainability criterion is
defined on. B1's absence is a real gap: the first visible sign of queue
saturation is `RESOURCE_EXHAUSTED`, by which point the queue is already full.

Verified metadata columns (PostgreSQL 18.4, DuckLake as written by DuckDB
1.5.x): `ducklake_data_file(data_file_id, table_id, begin_snapshot,
end_snapshot, file_order, path, path_is_relative, file_format, record_count,
file_size_bytes, footer_size, row_id_start, partition_id, encryption_key,
mapping_id, partial_max)`. `end_snapshot IS NULL` means live.

---

## Where measurement comes from

| what | source | resolution |
|---|---|---|
| offered / accepted / rejected, latency, in-flight, stalls | generator, native | 1 s |
| collector liveness, batches | `/health` via pipeline exporter | 10 s |
| compaction counters, file-size classes | `/health` via pipeline exporter | 10 s |
| compaction *durations* | parsed from the compactor log | per event |
| B2 backlog, catalog stats | PostgreSQL, direct | 10 s |
| B3 lag | DuckLake watermark via DuckDB | 60 s |
| S3 objects and bytes | ListObjectsV2 | 60 s |
| CPU, memory, disk, network, RSS, JVM heap, restarts, OOM | `host_agent.py` on each host | 1 s |

The compactor's Micrometer meters are **log lines**, not metrics — it uses a
`LoggingMeterRegistry`. `bench/parse_compactor_log.py` turns them back into a
time series, which only works because
`compactor/config/simplelogger.properties` enables timestamps; slf4j-simple
prints none by default, and without them the merge durations have no time axis
at all.

---

## Deployment layout on the servers

```
/opt/analytics-bench/
├── collector/            SERVER 2
│   ├── conf/application.conf     0600, rendered from the template
│   ├── conf/logback.xml          shadows the module's DEBUG default
│   ├── bin/run.sh                reads the catalog password, execs java
│   └── logs/collector.log  logs/gc.log
├── compactor/            SERVER 3
│   ├── conf/application.conf     first on the classpath: the compactor has no -c flag
│   ├── conf/simplelogger.properties
│   ├── bin/run.sh
│   └── logs/compactor.log  logs/gc.log
└── agent/                all three
    ├── bin/host_agent.py
    └── logs/host-<role>.jsonl

/etc/systemd/system/bench-collector.service
/etc/systemd/system/bench-compactor.service
/var/tmp/duckdb-{collector,compactor}      DuckDB spill — NOT /tmp, which is tmpfs
```

Two loading quirks worth knowing:

- The **compactor has no `-c` flag**. `CompactionConfig.rawConfig()` calls
  `ConfigFactory.load("application")`, i.e. it reads `application.conf` off the
  *classpath*. `bin/run.sh` puts `conf/` first so it shadows the copy baked into
  the module. Only `--conf key=value` overrides work on the command line.
- The **catalog password is never written next to the config**. Both configs
  reference `${PG_PASSWORD}`; `bin/run.sh` reads it from the existing `.pw` file
  and exports it, and Typesafe Config resolves the substitution from the
  environment at startup.

---

## Clock

All three hosts run chrony against the AWS time source (`169.254.169.123`),
stratum 4, observed offset under 10 µs. End-to-end lag (B3) is computed across
machines, so this matters; the offset is captured into `environment.json` on
every run so a reader can judge whether a lag figure is trustworthy.
