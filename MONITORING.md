# Monitoring

Two collection paths, for two different jobs.

| | for | resolution | survives the run |
|---|---|---|---|
| **Prometheus + Grafana** | *watching* a run | 15 s | 15 days, then gone |
| **JSONL agents + exporter** | *measuring* a run | 1 s | yes — files the report is generated from |

The report **never reads Prometheus**. A benchmark result has to be
reproducible from files that can be re-analysed months later, not from a TSDB
with a retention policy. Grafana is for the human watching a soak at 2am.

---

## Start it

```bash
./scripts/start.sh              # host agents + pipeline exporter (needed for a run)
./scripts/start.sh --monitoring # + Prometheus and Grafana in Docker
./scripts/start.sh --all
```

| | |
|---|---|
| Prometheus | `http://10.16.21.20:9090` |
| Grafana | `http://10.16.21.20:3000` — admin/admin, anonymous viewer enabled |
| Pipeline exporter | `http://10.16.21.20:9101/metrics` |
| Generator | `http://10.16.21.20:9102/metrics` — only while a test runs |

Reaching 3000/9090 from a workstation needs security-group rules; see
[NETWORK.md](NETWORK.md).

---

## What is scrapeable — and what is not

This is the most important section on the page.

### Scrapeable

| Target | Port | Source |
|---|---|---|
| generator | 9102 | this project, native Prometheus |
| pipeline exporter | 9101 | this project |
| node_exporter | 9100 | optional, `scripts/deploy-node-exporter.sh` |

### Not scrapeable — by construction

**The collector publishes no Prometheus metrics.** It is built with a
`SimpleMeterRegistry`, so its Micrometer meters live in process memory with no
exporter. `/health` returns JSON, not Prometheus text, and exposes only
`status`, `uptimeSeconds`, `grpcPort`, `knownQueues` and `batchesProcessed`.

Registered and unreachable:

```
dazzleduck.otel.export.requests            RPCs received
dazzleduck.otel.export.records             records accepted
dazzleduck.otel.export.errors              failed exports
dazzleduck.otel.export.latency             p50/p95/p99 ack latency
dazzleduck.otel.writer.bytes_written       cumulative Parquet bytes
dazzleduck.otel.writer.batches_written     buckets flushed
dazzleduck.otel.writer.write_failures      failed bucket writes
dazzleduck.otel.writer.data_phase_ms       COPY-to-Parquet time
dazzleduck.otel.writer.post_ingest_phase_ms   catalog-commit time
dazzleduck.otel.writer.pending_batches     queue depth (B1)
dazzleduck.otel.writer.pending_buckets     queue depth (B1)
```

The costliest loss is the last three. `data_phase_ms` versus
`post_ingest_phase_ms` answers **"is the bottleneck DuckDB or the catalog?"**
directly, and it is the single most useful diagnostic in the pipeline.
`pending_batches` / `pending_buckets` are B1 — without them, the first visible
sign of queue saturation is `RESOURCE_EXHAUSTED`, by which point the queue is
already full.

**The compactor's meters are log lines.** It uses a `LoggingMeterRegistry`:

```
2026-09-10T11:01:56.469Z [logging-metrics-publisher] [INFO] LoggingMeterRegistry -
  ducklake.compaction.duration{database=bench,step=merge,type=minor}
  throughput=0.016667/s mean=0.024414737s max=0.027184873s
```

`bench/parse_compactor_log.py` turns these back into a time series at collection
time. That only works because `compactor/config/simplelogger.properties` turns
on timestamps — slf4j-simple prints none by default, and without them the merge
durations have no time axis at all.

Neither service is listed as a Prometheus target. Adding them would leave two
permanently-red dots and train everyone to ignore a down target. The pipeline
exporter polls their `/health` endpoints instead. Fixes:
[IMPROVEMENTS.md](IMPROVEMENTS.md).

---

## The pipeline exporter

`monitoring/exporters/pipeline_exporter.py` — the load-bearing piece. It is the
only source for backlog, visibility lag, compaction counters, catalog health and
S3 growth.

| Source | Every | Provides |
|---|---|---|
| collector `/health` | 10 s | up, uptime, known queues, batches processed |
| compactor `/health` | 10 s | minor/major/merged counters, small/medium/total file counts |
| PostgreSQL | 10 s | **B2 backlog**, snapshots, connections, active queries, db size, commits, rollbacks, deadlocks |
| DuckLake watermark | 60 s | **B3 visibility lag**, committed rows |
| S3 `ListObjectsV2` | 60 s | object count, bytes under the prefix |

The two slow sources are slow on purpose: a full `ListObjectsV2` over the prefix
is the most expensive thing this process does, and at 10 s it would become a
load source of its own.

Every scrape records success or failure per source (`bench_scrape_ok`,
`bench_scrape_errors_total`). **A source that is down produces a recorded
failure, never a silently missing sample** — a gap in a graph and a zero look
identical, and only one of them is true.

```bash
# one poll, printed, no daemon
./.venv/bin/python monitoring/exporters/pipeline_exporter.py --once --port 9199
```

---

## Host agents

`bench/host_agent.py` on each of the three servers, 1 s, reading `/proc`
directly — no psutil, so it runs under the stock Python 3.9 on a bare host.

Captures: CPU total and per core · load · context switches · memory used,
available, page cache, dirty · swap · disk read/write bytes, IOPS, util% per
device · filesystem free space · network rx/tx · watched process RSS, threads,
CPU · JVM heap via `jcmd` · **process restarts** (detected from a changed
start time) · **OOM kills** from `dmesg`.

Restarts and OOM kills are results, not diagnostics: a restart during a measured
level invalidates that level, and the report says so.

```
/opt/analytics-bench/agent/logs/host-<role>.jsonl
```

`collect-results.sh` copies only the slice belonging to the run, using line
offsets recorded at its start — exact, rather than time-based and approximate.

---

## Dashboards

Seven, provisioned automatically, reloaded from disk every 30 s.

| Dashboard | For |
|---|---|
| **Pipeline — end to end** | the default home page: offered vs accepted vs rejected, all three backlogs, storage |
| Generator | rates, latency percentiles, in-flight, pacer stalls, errors by status |
| Collector | everything observable of SERVER 2, and a panel stating plainly what is not |
| Compactor | merge activity, file-size distribution, housekeeping |
| Infrastructure | CPU, memory, disk, network across all three (needs node_exporter) |
| Errors and health | every failure and every measurement blind spot |
| Storage and catalog | S3 growth, file sizes, PostgreSQL health |

The JSON is **generated**. Edit `monitoring/grafana/build_dashboards.py` and
re-run it; hand-editing 2000 lines of JSON across seven files is how panels
drift into three different units for the same metric.

```bash
python3 monitoring/grafana/build_dashboards.py
```

One palette across all of them, muted and colour-blind safe. Panels whose data
source does not exist are **included with an explanation** rather than omitted —
a dashboard with a visible "this cannot be measured yet" panel is more honest
than one that looks complete.

---

## Key metrics

### Rates

```promql
bench_generator_offered_rps       # attempted. NOT throughput.
bench_generator_accepted_rps      # acked, i.e. persisted
bench_generator_rejected_rps      # RESOURCE_EXHAUSTED
bench:accepted_over_offered       # must be >= 0.99
```

### Backlog

```promql
bench_backlog_files               # B2 — live data files
bench_backlog_small_files         # below minor_compaction_max_size
bench:backlog_files_slope_per_min # positive = falling behind
bench_watermark_lag_seconds       # B3 — how stale the lake is
```

B1 has no metric. See above.

### Latency

```promql
histogram_quantile(0.99,
  sum(rate(bench_generator_export_latency_seconds_bucket[1m])) by (le))
```

Includes queue wait, the DuckDB `COPY`, and the catalog commit. It is
persistence latency, not network round trip.

### Compaction

```promql
bench_compaction_minor_total
bench_compaction_major_total
rate(bench_compaction_files_compacted_total[5m]) * 60   # drain rate, files/min
bench:mean_live_file_bytes                              # should rise while merging
```

### Health

```promql
bench_collector_up
bench_compactor_up
bench_scrape_ok                   # 0 = a blind spot, usually the security group
```

---

## Alerts

`monitoring/prometheus/rules/pipeline.yml`. These encode the sustainability
criteria, so the live view and the generated report use the same definition —
change one and change the other, or they will disagree and only one will be
right.

| Alert | Fires | Means |
|---|---|---|
| `CollectorDown` | 1 m | ingestion is stopped |
| `CompactorDown` | 2 m | small files accumulate; not immediately data-threatening |
| `IngestionRejected` | 30 s | above capacity; the level is not sustainable |
| `AcceptRateBelowTarget` | 3 m | accepted < 99% of offered |
| `BacklogGrowing` | 10 m | **compaction is losing** — the important one |
| `VisibilityLagHigh` | 5 m | data more than 5 min stale |
| `CatalogConnectionsHigh` | 5 m | shared RDS connection pressure |
| `ScrapeFailing` | 5 m | a measurement blind spot |

---

## Logs

| What | Where |
|---|---|
| collector | `/opt/analytics-bench/collector/logs/collector.log` (+ `gc.log`) |
| compactor | `/opt/analytics-bench/compactor/logs/compactor.log` (+ `gc.log`) |
| host agents | `/opt/analytics-bench/agent/logs/host-<role>.jsonl` |
| generator | `results/<test-id>/generator.log` |
| exporter | `logs/exporter.out`, samples in `logs/pipeline.jsonl` |
| systemd | `journalctl -u bench-collector -f` |

Two logging fixes this project applies, both of which mattered:

- **The collector logged at DEBUG.** gRPC and Netty at DEBUG produced 42 KB
  before a single record arrived. Under load that is disk I/O and CPU competing
  with the thing being measured — a benchmark that logs at DEBUG is measuring
  logback. `collector/config/logback.xml` sets INFO, silences the transport, and
  uses a non-blocking async appender that drops rather than stalling the ingest
  path.
- **The compactor had no timestamps.** Its dependency tree contains
  slf4j-simple and no logback, so a `logback.xml` is silently ignored;
  `simplelogger.properties` is what it reads. Without `showDateTime` its
  LoggingMeterRegistry output — which *is* its metrics — had no time axis.

---

## Costs

Everything here is capped so the monitoring does not become part of what is
measured. On 2-vCPU hosts that is not theoretical.

| | limit |
|---|---|
| Prometheus | 0.5 CPU, 1 GiB, 15 d / 2 GB retention |
| Grafana | 0.5 CPU, 512 MiB |
| node_exporter | 0.2 CPU, 128 MiB (`CPUQuota=10%` on servers 2 and 3) |
| pipeline exporter | 0.5 CPU, 512 MiB |
| host agents | negligible — reads `/proc`, writes one JSON line per second |

If the exporter starts costing measurable CPU, raise `s3_interval` first: the
`ListObjectsV2` sweep dominates.
