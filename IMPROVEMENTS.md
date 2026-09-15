# Suggested improvements — `dazzleduck-sql-server`

Findings from deploying and benchmarking `dazzleduck-sql-otel-collector` and
`dazzleduck-sql-ducklake-compactor` across three EC2 hosts with a PostgreSQL
RDS catalog and S3 data, at commit `4b48e9f9` (0.2.18-SNAPSHOT).

Each item states what was observed, why it matters, and a concrete change.
Severity is about operability and correctness of measurement, not code taste.

| # | Finding | Severity |
|---|---|---|
| 1 | Collector metrics are unreachable (`SimpleMeterRegistry`) | **high** |
| 2 | Compactor has no SLF4J binding at runtime scope — logs and metrics vanish | **high** |
| 3 | Collector defaults to DEBUG logging | **high** |
| 4 | Compaction metrics exist only as log lines, with no timestamp | medium-high |
| 5 | Compactor cannot be given a config file | medium |
| 6 | No runnable artifact without Docker | medium |
| 7 | DuckLake data inlining silently defeats compaction | medium |
| 8 | Health endpoints cannot be bound to loopback | medium |
| 9 | `/health` omits queue depth | medium |
| 10 | Major compaction silently replaces the minor tick | medium |
| 11 | `minor_compaction_max_size` below `min_bucket_size` makes minor merges a no-op | low-medium |
| 12 | `event_name` is a permanently NULL column | low |
| 13 | DuckDB/DuckLake version coupling is undocumented | low |
| 14 | `temp_directory` default is dangerous on tmpfs hosts | low |
| 15 | Catalog password must be inlined in a HOCON string | low |

---

## 1. Collector metrics are registered and then thrown away — **high**

`CollectorProperties.java:36`

```java
private MeterRegistry meterRegistry = new SimpleMeterRegistry();
```

`OtelCollectorMetrics` is genuinely well built — per-queue tags, a real `Timer`
for latency percentiles, `FunctionCounter`s bound to the live queue, careful
de-registration on eviction so an evicted queue can be GC'd. All of it lands in
a registry with **no exporter**, so none of it can be observed by anything.

Unreachable as a result:

```
dazzleduck.otel.export.requests / records / errors / latency
dazzleduck.otel.writer.bytes_written / batches_written / write_failures
dazzleduck.otel.writer.data_phase_ms
dazzleduck.otel.writer.post_ingest_phase_ms
dazzleduck.otel.writer.pending_batches / pending_buckets
```

Two of those are the costly ones:

- **`data_phase_ms` vs `post_ingest_phase_ms`** answers *is the bottleneck
  DuckDB or the catalog?* directly. It is the single most useful diagnostic in
  the pipeline and there is no way to read it.
- **`pending_batches` / `pending_buckets`** are queue depth. Without them there
  is no early warning of saturation: the first observable symptom is
  `RESOURCE_EXHAUSTED`, at which point the queue is already full and requests
  are being refused. Operationally that is the difference between "scale up
  now" and "we started dropping traffic".

Everything a benchmark can currently see about the collector is
`batchesProcessed` from `/health`, plus what the client infers.

**Suggested change.** A config key selecting the registry, defaulting to
today's behaviour:

```hocon
otel_collector.metrics {
  registry = prometheus     # simple | logging | prometheus
  path     = "/metrics"     # served on the existing health HttpServer, port 8081
}
```

`micrometer-registry-prometheus` is a small dependency and the health
`HttpServer` already exists, so this is roughly a class and a handler. It would
close the largest observability gap in the project.

---

## 2. The compactor has no SLF4J binding at runtime scope — **high**

Measured on the deployed host:

```console
$ ./mvnw dependency:list -pl dazzleduck-sql-ducklake-compactor -DincludeScope=runtime
   org.slf4j:slf4j-api:jar:2.0.16:compile

$ ./mvnw dependency:list -pl dazzleduck-sql-ducklake-compactor
   org.slf4j:slf4j-api:jar:2.0.16:compile
   org.slf4j:slf4j-simple:jar:2.0.16:test        <-- TEST scope only
```

`slf4j-simple` comes from the parent POM at `<scope>test</scope>`. So a correct
runtime classpath — which is what `jib` builds — contains `slf4j-api` and **no
provider**. SLF4J then falls back to the NOP logger and discards everything.

That is worse than losing diagnostics, because `Main.java:25` does:

```java
MeterRegistry registry = new LoggingMeterRegistry();
```

The compactor's Micrometer meters **are** log lines. With no binding, the
container emits no logs *and no metrics at all* — merge durations, file counts
and housekeeping timings all silently disappear. A production compactor would
be entirely unobservable.

This deployment only works because `dependency:copy-dependencies` defaults to
all scopes and pulled the test-scoped `slf4j-simple` onto the classpath by
accident.

**Suggested change.** Add a runtime-scope binding to the module — for
consistency with `dazzleduck-sql-otel-collector`, `logback-classic` — and ship a
default `logback.xml`. Worth auditing the other executable modules for the same
gap; `dazzleduck-sql-flight` also constructs a `LoggingMeterRegistry`.

---

## 3. The collector logs at DEBUG out of the box — **high**

`dazzleduck-sql-otel-collector` depends on `logback-classic` but ships **no
`logback.xml`** in `src/main/resources` (only `logback-test.xml` under test).
Logback's fallback configuration is root level DEBUG to console, so gRPC and
Netty log every internal setting on startup:

```
42 KB of log output before a single record arrived
```

Under load that is continuous disk I/O and CPU competing with the workload, on
hosts where the workload is the thing being measured. A benchmark run with this
default is partly measuring logback.

**Suggested change.** Ship a `logback.xml` with root at INFO, `io.grpc` and
`io.netty` at WARN, and an `AsyncAppender` with `neverBlock=true` so a slow disk
can never stall the ingest path. Roughly:

```xml
<logger name="io.grpc"  level="WARN"/>
<logger name="io.netty" level="WARN"/>
<root level="INFO"><appender-ref ref="ASYNC"/></root>
```

---

## 4. Compaction metrics have no time axis — medium-high

Follows from 2. The `LoggingMeterRegistry` output looks like:

```
ducklake.compaction.duration{database=bench,step=merge,type=minor} \
  throughput=0.016667/s mean=0.024414737s max=0.027184873s
```

Useful numbers — but slf4j-simple prints **no timestamp** by default, so out of
the box they cannot be placed on a timeline. Correlating a merge with a backlog
spike is impossible. Adding `showDateTime` recovered it here, but a metrics
transport that depends on the log formatter being configured correctly is
fragile by construction.

**Suggested change.** Same fix as 1 — expose a real registry. Keep the logging
registry as a fallback, and ship a logging config that includes timestamps so
the fallback is usable.

---

## 5. The compactor cannot be given a config file — medium

`CompactionConfig.rawConfig()`:

```java
return overrides
    .withFallback(ConfigFactory.load("application"))   // CLASSPATH only
    .withFallback(ConfigFactory.systemProperties())
    .resolve()
    .getConfig(CONFIG_PATH);
```

`ConfigFactory.load("application")` reads `application.conf` from the
**classpath**. There is no `-c` / `--config` flag; only `--conf key=value`
overrides. Deploying site configuration therefore means putting a directory
ahead of the module's own jar on the classpath:

```
CP="$BASE_DIR/conf:$MODULE/target/classes:$MODULE/target/lib/*"
```

That works, but it is obscure, easy to get wrong, and inconsistent with the
collector, which accepts `-c <file>` (`Main.Args.configFile`). A multi-line
startup SQL script is also impractical to pass through `--conf`.

**Suggested change.** Accept `-c/--config <file>` in the compactor, matching the
collector, and prefer it over the classpath resource. `CollectorConfig` already
has the pattern to copy.

---

## 6. No runnable artifact without Docker — medium

Both modules build only a thin jar plus a `jib` image; `maven-jar-plugin` sets
`Main-Class` but there is no shaded jar and no dependency layout. On a host
without Docker — which is the normal case for a plain VM deployment, and was the
case on all three of these — you have to discover for yourself that the recipe
is:

```console
./mvnw -DskipTests install -pl <module> -am
./mvnw dependency:copy-dependencies -DoutputDirectory=target/lib -pl <module>
java -cp "target/classes:target/lib/*" <main-class>
```

And that command is a trap: `copy-dependencies` defaults to **all** scopes, so
it silently puts test-scoped jars on the runtime classpath. That is exactly how
finding 2 stayed hidden here.

**Suggested change.** Either add `maven-shade-plugin` to produce a runnable
`-all.jar`, or bind `dependency:copy-dependencies` with
`<includeScope>runtime</includeScope>` in the module POM and document the
`java -cp` line in each README. The second is smaller and also removes the
scope trap.

---

## 7. DuckLake data inlining silently defeats compaction — medium

With DuckLake's default inlining, small commits are stored as rows **inside the
PostgreSQL catalog** rather than as Parquet on S3. Observed directly: an early
probe insert produced

- `SELECT count(*)` returning the right answer,
- **zero** rows in `ducklake_data_file`,
- a new `ducklake_inlined_data_1_1` table in PostgreSQL.

So ingestion appears to work, row counts reconcile, and the compactor has
nothing to merge — because no files exist. For a benchmark this is the worst
class of bug: everything looks correct while measuring a different system. In
production it means data accumulating in the catalog database instead of object
storage.

The fix is `DATA_INLINING_ROW_LIMIT 0` on `ATTACH`, but nothing in the
collector's configuration, README or reference.conf mentions it.

**Suggested change.** Have `DuckLakeIngestionTaskFactoryProvider` check the
attached catalog's inlining setting at startup and log a clear warning (or
refuse to start) when it is non-zero, since the registration path assumes files.
At minimum, document it in the DuckLake section of the collector README.

---

## 8. Health endpoints cannot be bound to loopback — medium

Both health servers:

```java
server = HttpServer.create(new InetSocketAddress(port), 0);
```

`InetSocketAddress(int)` binds the **wildcard** address, and neither service
exposes a bind-address setting. Confirmed on the running hosts — `ss -lntp`
shows `*:8081` and `*:8080`.

For a deployment whose monitoring runs host-locally — which is the correct
pattern, and the one adopted here — the endpoint only ever needs to answer on
`127.0.0.1`. As it stands the security group is the *only* control preventing
network exposure; there is no defence in depth, and a permissive group
immediately exposes an unauthenticated endpoint that reports internal state.

The gRPC server has the same shape (`NettyServerBuilder.forPort(...)`), though
for the collector that port genuinely must be reachable.

**Suggested change.** Add an optional bind address, defaulting to today's
behaviour:

```hocon
otel_collector.health { port = 8081, bind_address = "0.0.0.0" }
dazzleduck_sql_compaction { health_port = 8080, health_bind_address = "0.0.0.0" }
```

`HttpServer.create(new InetSocketAddress(bindAddress, port), 0)` is the whole
change.

---

## 9. `/health` omits queue depth — medium

```json
{"status":"HEALTHY","uptimeSeconds":9217,"grpcPort":4317,"knownQueues":1,"batchesProcessed":0}
```

`batchesProcessed` is a cumulative count of batches — not records, and no
indication of whether the writer is keeping up. Meanwhile
`ParquetIngestionQueue.getStats()` already exposes exactly what an operator
needs, and `OtelCollectorMetrics` already reads it for its gauges.

Given finding 1, `/health` is the only machine-readable surface, so this is the
difference between seeing saturation coming and being surprised by
`RESOURCE_EXHAUSTED`.

**Suggested change.** Add per-queue detail from the stats already on hand:

```json
"queues": [{
  "id": "logs",
  "pendingBatches": 3, "pendingBuckets": 1,
  "pendingWriteBytes": 12582912, "maxPendingWrite": 524288000,
  "recordsWritten": 1200, "bytesWritten": 8388608, "writeFailures": 0
}]
```

`pendingWriteBytes` against `maxPendingWrite` is the single most valuable ratio
here: it is the distance to backpressure.

---

## 10. A due major compaction silently replaces the minor tick — medium

In `CompactionService.runCompaction`, when a major run is due it runs **instead
of** that tick's minor run, not in addition. The behaviour is defensible; the
problem is that it is invisible. Nothing in `application.conf`, the README or
the log says so, and the counters (`totalMinorCompactions`,
`totalMajorCompactions`) do not distinguish "minor ran" from "minor was skipped
because major ran".

Two consequences:

- A single compactor can never produce concurrent minor and major activity. Any
  report claiming to have measured that from one instance is wrong — worth
  knowing before designing a compaction benchmark.
- Small files can wait longer than `minor_compaction_frequency` suggests, with
  no signal that a skip happened.

**Suggested change.** Document it in `application.conf` next to the two
frequency keys, log at INFO when a minor run is skipped in favour of a major
one, and add a `totalMinorSkippedForMajor` counter to `/health`. Optionally a
`run_minor_with_major = false` key for operators who want both.

---

## 11. Default sizes make minor compaction a no-op at high rates — low-medium

The suggested collector `min_bucket_size` is 16 MiB while the compactor's
`minor_compaction_max_size` default is 8 MiB. So:

- **Low ingest rate** — buckets flush on `max_delay_ms` well under 16 MiB
  (~2.5 MiB at 1k rec/s with 500-byte records), files land below 8 MiB, and
  minor compaction does the work.
- **High ingest rate** — buckets fill to 16 MiB, every file is above the minor
  threshold, minor compaction finds nothing (`totalMinorCompactions` climbs
  while `totalFilesCompacted` stays 0) and major compaction does everything.

Neither is broken, but the tier doing the work changes with load, which is
surprising when reading compaction metrics and makes cross-level comparison in a
staircase misleading.

**Suggested change.** Note the interaction where both defaults are documented,
and consider defaulting `minor_compaction_max_size` to something above a typical
`min_bucket_size` so minor compaction has a defined role at any rate.

---

## 12. `event_name` is a permanently NULL column — low

`LogRecordConverter.java:46`

```java
values[OtelLogSchema.COL_EVENT_NAME] = null; // field added in proto > 1.3.2
```

The column is in `OtelLogSchema`, in every Parquet file and in every target
table DDL, and is always NULL — the pinned `opentelemetry-proto 1.3.2-alpha`
has no such field. Costs a little space and some confusion for anyone writing
the table schema.

**Suggested change.** Either bump `otel.proto.version` to a release that has
`LogRecord.event_name` and populate it, or drop the column until then. If it
stays, say so in the schema documentation so operators do not go looking for
data in it.

---

## 13. DuckDB / DuckLake version coupling is undocumented — low

`pom.xml` pins `duckdb.version=1.5.4.0`. The DuckLake catalog metadata schema is
version-dependent, so reading a catalog written by 1.5.4 with a different DuckDB
can misinterpret it. On these hosts the `duckdb` CLI was **1.5.2** while the
server wrote with **1.5.4.0** — an easy trap for anyone inspecting the catalog
by hand or scripting against it.

**Suggested change.** State the coupling in the DuckLake section of the README:
use a client matching `duckdb.version`, and treat metadata read with another
version as unreliable.

---

## 14. `temp_directory` default is dangerous on tmpfs hosts — low

DuckDB spills to `temp_directory`, which defaults into the working directory or
`/tmp` depending on how it is invoked. On Amazon Linux 2023 — and most modern
systemd distributions — **`/tmp` is tmpfs**, i.e. RAM. On a host with no swap
(the default for these instances) a spill there converts a disk spill into an
OOM kill: the exact failure the spill was meant to prevent.

**Suggested change.** Set `temp_directory` explicitly in both modules'
`reference.conf` / `application.conf` startup scripts to a real disk path such
as `/var/tmp/dazzleduck`, with a comment explaining why not `/tmp`.

---

## 15. The catalog password must be inlined in a HOCON string — low

The DuckLake `ATTACH` goes in `startup_script_provider.content`, so the
PostgreSQL password ends up inside a HOCON string, typically written to disk
next to the config.

It can be kept out of the file with environment substitution, but only via
value concatenation, because triple-quoted HOCON strings do not substitute:

```hocon
content = "... password="${PG_PASSWORD}"' AS bench (DATA_PATH '...');"
```

That forces the entire startup script onto **one long line** — the approach used
here, and it works, but it is unpleasant and easy to get wrong.

**Suggested change.** Either support `script_location` with environment
expansion applied to the file's contents, or add an explicit credentials block
for DuckLake catalogs:

```hocon
otel_collector.ducklake {
  catalog = bench
  postgres { host = ..., database = ..., user = ..., password_file = "/run/secrets/pg" }
  data_path = "s3://bucket/prefix/"
  data_inlining_row_limit = 0
}
```

That would also give a natural place to validate finding 7.

---

## What worked well

Worth stating, since the list above is all problems:

- **Ack-on-durability.** `OtelServiceBase` completing the gRPC response from the
  batch-write future is the right decision and makes the whole system honest:
  "accepted" means persisted and committed. Most collectors ack on receipt and
  leave the client unable to tell durable from buffered. It also makes
  client-observed latency a genuine measure of the write path.
- **Typed backpressure.** `PendingWriteExceededException` surfacing as
  `RESOURCE_EXHAUSTED` with `RetryInfo` is exactly right — a distinguishable,
  actionable rejection rather than a timeout or a silent drop.
- **Transactional watermarks.** Committing the watermark row in the same
  transaction as the file registration gives an independent, consistent witness
  to what the catalog accepted. It made zero-loss verification straightforward
  and is a genuinely good design touch.
- **Metric hygiene.** `OtelCollectorMetrics` de-registering per-queue meters on
  eviction, with a comment explaining that the `FunctionCounter`s would
  otherwise pin an evicted queue in memory, is the kind of detail usually
  discovered via a leak. Finding 1 is that this care goes unrewarded for want of
  an exporter.
- **Graceful shutdown.** The `MAINTENANCE` state on `/health` with a
  configurable LB-drain window before the gRPC server stops is a real
  production affordance, not an afterthought.
- **Code comments.** Several explain *why*, not *what* — the `totalBatchesProcessed`
  comment about surviving queue eviction, and `Main.withOverrides` explaining
  why a missing config provider is fatal rather than falling back. Both answered
  questions before they were asked.
