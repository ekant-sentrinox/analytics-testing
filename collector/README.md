# Collector (SERVER 2 — 10.16.24.204)

`dazzleduck-sql-otel-collector`. **A remote service** — it runs on SERVER 2
under systemd, not in this project's Docker Compose. This directory holds the
templates that configure it and the script that deploys it.

```
collector/config/
├── application.conf.tmpl          HOCON, rendered per deploy
├── logback.xml                    shadows the module's DEBUG default
├── run.sh.tmpl                    launcher: reads the password, execs java
└── bench-collector.service.tmpl   systemd unit
```

## Deploy

```bash
../scripts/deploy-collector.sh              # build, install, restart
../scripts/deploy-collector.sh --no-build   # config-only change
```

Values come from `config/collector.yaml` and `.env`. **Do not hand-edit the
rendered config on the server** — the next deploy overwrites it.

## On-host layout

```
/opt/analytics-bench/collector/
├── conf/application.conf     0600
├── conf/logback.xml
├── bin/run.sh
├── logs/collector.log  logs/gc.log
└── run/
/etc/systemd/system/bench-collector.service
/var/tmp/duckdb-collector          DuckDB spill
```

```bash
systemctl status bench-collector
journalctl -u bench-collector -f
curl -s localhost:8081/health
```

## Config that matters

| | default | why |
|---|---|---|
| `min_bucket_size` | 16 MiB | **the most consequential knob.** Sets Parquet file size, which sets compaction load. |
| `max_delay_ms` | 5000 | flush even when under size; the floor on visibility lag at low rates |
| `max_pending_write` | 500 MiB | above this: `PendingWriteExceededException` → gRPC `RESOURCE_EXHAUSTED` |
| DuckDB `threads` | 2 | = vCPUs |
| DuckDB `memory_limit` | 2 GB | |
| DuckDB `temp_directory` | `/var/tmp/duckdb-collector` | **not `/tmp`** — tmpfs on AL2023; spilling there converts a disk spill into an OOM kill on a swapless host |
| JVM `-Xmx` | 2g | Arrow and DuckDB are off-heap; a bigger heap starves them |

Raising `min_bucket_size` → fewer, larger files, less compaction work, higher
visibility lag, more memory per queue. Lowering it does the opposite. First pair
to move when the backlog trends up.

## Three deployment details worth knowing

**`DATA_INLINING_ROW_LIMIT 0` is required.** Without it DuckLake keeps small
commits as rows inlined in the PostgreSQL catalog instead of writing Parquet to
S3. Ingestion works and row counts add up, but no data file is created and the
compactor has nothing to merge — the benchmark measures a different system while
looking correct.

**The catalog password is never written next to the config.** The HOCON
references `${PG_PASSWORD}`; `bin/run.sh` reads it from the existing `.pw` file
and exports it, and Typesafe Config resolves the substitution at startup. That
is the only reason the wrapper script exists.

**The module logs at DEBUG by default.** gRPC and Netty at DEBUG produced 42 KB
before a single record arrived; under load that is disk I/O and CPU competing
with the thing being measured. `logback.xml` sets INFO, silences the transport,
and uses a non-blocking async appender that drops rather than stalling the
ingest path.

## Auth

Every request needs a signed Bearer JWT carrying `x-dd-ingestion-queue`. **There
is no default queue** — a token without the claim is `INVALID_ARGUMENT`, not
routed somewhere sensible. The secret is shared with the generator via
`OTEL_JWT_SECRET_B64`; rotating it requires redeploying the collector.

## Observability — and its limit

`/health` is the only machine-readable surface:

```json
{"status":"HEALTHY","uptimeSeconds":277,"grpcPort":4317,"knownQueues":1,"batchesProcessed":0}
```

`status` is `HEALTHY`, `MAINTENANCE` (graceful drain in progress) or `DOWN`.

The Micrometer meters — `export.records`, `export.latency`,
`writer.data_phase_ms`, `writer.post_ingest_phase_ms`, `writer.pending_batches`
— are registered in a `SimpleMeterRegistry` and **never exported anywhere**.
The two costly losses:

- **B1 queue depth** is unmeasurable, so the first sign of saturation is
  `RESOURCE_EXHAUSTED`, when the queue is already full.
- **`data_phase_ms` vs `post_ingest_phase_ms`** would answer *is the bottleneck
  DuckDB or the catalog?* directly. It is the single most useful diagnostic in
  the pipeline and it is unreachable.

See [../IMPROVEMENTS.md](../IMPROVEMENTS.md).

## Shutdown

`SIGTERM` starts a graceful drain: `/health` flips to `MAINTENANCE` (503) for
`shutdown_grace_period_ms` so a load balancer stops routing, then the gRPC
server stops and in-flight batches flush. The unit allows `TimeoutStopSec=90`.
Killing it earlier is what data loss looks like — and is exactly what
`tests/recovery/run.sh collector-sigkill` measures.
