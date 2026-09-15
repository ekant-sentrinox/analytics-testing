# analytics-distributed-test

A working benchmark harness for the DazzleDuck telemetry pipeline running across
three EC2 instances: an OTLP load generator, an OTLP collector writing Parquet
into DuckLake, and a DuckLake compactor — with PostgreSQL RDS as the catalog and
S3 as the data store.

It is executable, not documentation. `scripts/run-test.sh smoke` provisions
nothing, asks nothing, and produces `results/<test-id>/report.md` with charts.

---

## The one thing to read first

Three numbers get confused in every pipeline benchmark, and confusing them is
how a system gets signed off at a rate it cannot hold:

| | what it is |
|---|---|
| **offered** | records the generator *attempted* to submit. A property of the generator. **Not throughput.** |
| **accepted** | records the collector *acked*. The export RPC does not return until the batch is on disk and committed to the catalog, so accepted means persisted. |
| **rejected** | records in RPCs that came back `RESOURCE_EXHAUSTED` — the collector explicitly refusing work. |

Throughput is **accepted, sustained with a flat backlog**. A pipeline can ack
every record while quietly accumulating small files it will never catch up on;
that is a system that fails next week, reported today as a pass. Every test here
checks the backlog trend alongside the accept rate, and the report refuses to
call a level sustainable on the accept rate alone.

---

## Architecture

```
                       SERVER 1 — 10.16.21.20
                       generator (this project)
                                │
                                │  OTLP/gRPC :4317
                                │  JWT, x-dd-ingestion-queue claim
                                ▼
                       SERVER 2 — 10.16.24.204
                       dazzleduck-sql-otel-collector
                       OTLP → Arrow → Parquet → DuckLake
                                │
                  ┌─────────────┴─────────────┐
                  ▼                           ▼
       PostgreSQL RDS                        S3
       analytics-perf-catalog      sentri-analytics-performance-test
       DuckLake catalog                  /bench/  (Parquet)
                  ▲                           ▲
                  └─────────────┬─────────────┘
                                │
                       SERVER 3 — 10.16.25.10
                       dazzleduck-sql-ducklake-compactor
                       merge adjacent files, expire, cleanup
```

Full detail, including why the RPC blocks until durable and what that means for
every latency number: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Prerequisites

Already true on these hosts; listed so a rebuild is reproducible.

| | |
|---|---|
| All three | Amazon Linux 2023, t3.large (2 vCPU / 7.6 GiB / 8 GiB root), chrony synced |
| SERVER 1 | Python 3.11, Docker + compose (monitoring only), AWS CLI, `duckdb` CLI, SSH access to 2 and 3 |
| SERVER 2 | Java 21 (Corretto), Maven, the `dazzleduck-sql-server` checkout |
| SERVER 3 | Java 21, Maven, Git, the checkout |
| Credentials | S3 via the EC2 instance role `analytics-performance-s3-role`. Catalog password in a file, path in `PG_PASSWORD_FILE`. No access keys anywhere. |

---

## Install

```bash
cd ~/analytics-distributed-test

./scripts/setup.sh            # venv, deps, .env, JWT secret, git init
$EDITOR .env                  # check hosts and PG_PASSWORD_FILE
./scripts/setup-ssh.sh        # shows what SERVER 1 needs to reach 2 and 3
./scripts/setup-ssh.sh --apply

./scripts/bootstrap-catalog.sh   # create the DuckLake tables (idempotent)
./scripts/deploy-collector.sh    # build + install + start on SERVER 2
./scripts/deploy-compactor.sh    # build + install + start on SERVER 3

./scripts/preflight.sh           # 40-odd checks; non-zero if anything mandatory fails
```

Both services install into `/opt/analytics-bench/{collector,compactor}` with
`conf/ bin/ logs/ run/` and a systemd unit — nothing is scattered through home
directories, and `systemctl status bench-collector` works the way you expect.

---

## Run a test

```bash
./scripts/run-test.sh smoke              # 10 rec/s, 60s — proves the path works
./scripts/run-test.sh medium             # 5k rec/s, 10 min
./tests/throughput/run.sh                # staircase to the sustainable ceiling
./tests/soak/run.sh --hours 6 --rps 3000
./tests/drain/run.sh                     # after a soak: how fast the backlog clears
./tests/recovery/run.sh collector-sigkill
```

Each run does: preflight → snapshot state → generate → settle → collect from all
three servers → validate correctness → generate report.

```
results/20260910-153000-throughput/
├── report.md              ← generated, never hand-edited
├── charts/*.png
├── metadata.json          config echo, host facts, clock offset
├── manifest.json          the reconciliation contract
├── generator.{json,jsonl,csv}
├── correctness.json       loss / duplication / gap verdict
├── environment.json       all three servers + effective config + env hash
├── state-before.json  state-collected.json
├── raw/                   pipeline, per-host 1 s metrics, compaction events
└── logs/                  collector, compactor, GC, extracted errors
```

---

## Test profiles

`smoke · light · medium · high · throughput · soak · stress · recovery`

Defined in `config/generator.yaml` under `profiles:`, all editable. Override per
run without touching the file:

```bash
./scripts/run-test.sh medium --rps 8000 --duration 1200
```

What each one is for, and its pass criteria:
**[TEST_SCENARIOS.md](TEST_SCENARIOS.md)**.

---

## Monitoring

```bash
./scripts/start.sh --monitoring
```

Prometheus on `:9090`, Grafana on `:3000`, seven provisioned dashboards.

The pipeline exporter (`monitoring/exporters/pipeline_exporter.py`) is the load
bearing piece: **the collector and the compactor publish no scrapeable metrics
at all** — one is built with a `SimpleMeterRegistry`, the other with a
`LoggingMeterRegistry`. The exporter polls their `/health` endpoints, the
PostgreSQL catalog, the DuckLake watermark and S3, and is the only source for
backlog, visibility lag and compaction counters.

Details and the list of what still cannot be measured:
**[MONITORING.md](MONITORING.md)**.

---

## Collecting and reporting

```bash
./scripts/collect-results.sh <test-id>
./scripts/validate-correctness.sh <test-id>
./scripts/generate-report.sh <test-id>
./scripts/generate-report.sh --all
```

The report is generated from files. A section whose input file is missing says
`NOT MEASURED` and names the file — it is never estimated, and never quietly
omitted. Reading one: **[RESULTS.md](RESULTS.md)**.

---

## Operating

```bash
./scripts/status.sh                 # one screen: all three servers, catalog, S3
./scripts/health-check.sh [--deep]  # stage by stage; --deep pushes real records through
./scripts/connectivity-check.sh     # every network path, refused vs dropped
./scripts/check-s3.sh
./scripts/check-postgres.sh
./scripts/start.sh | stop.sh | restart.sh | reset.sh
./scripts/cleanup.sh --data         # dry run; needs --yes and typing the bucket name
```

---

## Known limitations

Honest list. Nothing here is worked around silently.

1. **Security group.** All three hosts share `sg-0ea9dd40012388877`, which
   allows port 22 only. Ports 4317, 8080, 8081 (and 9100 for node_exporter) are
   *dropped*, so the generator cannot reach the collector and Prometheus cannot
   reach the health endpoints. The services are healthy on their own loopbacks.
   The exact rules to add are in **[NETWORK.md](NETWORK.md)**. Until they are
   added, no Track B number can be produced.
2. **Collector queue depth (B1) is unmeasurable.** `writer.pending_batches` and
   `writer.pending_buckets` live in an unexported registry. The first visible
   sign of queue saturation is `RESOURCE_EXHAUSTED`, by which point it is full.
3. **`data_phase_ms` vs `post_ingest_phase_ms` is unmeasurable.** The single
   most useful diagnostic in the pipeline — *is the bottleneck DuckDB or the
   catalog?* — is registered and never exported.
4. **Hardware bounds the plan.** 2 vCPU and ~5 GiB free disk per host. The
   source spec's 100M/500M-row datasets and 100 GB–1 TB compaction tiers do not
   fit and are reported `NOT TESTED`, not estimated.
5. **t3 is burstable.** CPU credit exhaustion during a long soak throttles the
   vCPU and is indistinguishable from a software regression on a graph. Check
   credits before blaming code.
6. **The generator may bind first.** A Python generator on 2 vCPU is plausibly
   the limit somewhere in the tens of thousands of records/s. The stress test
   watches generator CPU and pacer stall and says so explicitly rather than
   reporting it as a pipeline ceiling.
7. **Three faults are not implemented here** — catalog-unavailable,
   storage-latency, network-partition — because the catalog is a shared RDS
   instance and the route table is not writable from the instance role.
   `tests/failure/run.sh --list` explains each.

Suggested fixes to the upstream project, gathered while building this:
**[IMPROVEMENTS.md](IMPROVEMENTS.md)**.

---

## Documentation

| | |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | fresh host → smoke test → report, shortest path |
| [ARCHITECTURE.md](ARCHITECTURE.md) | the real data path and what it implies |
| [CONFIGURATION.md](CONFIGURATION.md) | every variable and every YAML key |
| [NETWORK.md](NETWORK.md) | IPs, ports, security group rules |
| [TEST_PLAN.md](TEST_PLAN.md) | methodology: sustainability, correctness, repeats |
| [TEST_SCENARIOS.md](TEST_SCENARIOS.md) | every test, its procedure and pass criteria |
| [MONITORING.md](MONITORING.md) | metrics, dashboards, logs, blind spots |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | failures seen, with the actual fix |
| [RESULTS.md](RESULTS.md) | how to read a report without over-claiming |
| [IMPROVEMENTS.md](IMPROVEMENTS.md) | findings for dazzleduck-sql-server |
| [CHANGELOG.md](CHANGELOG.md) | |

---

## Safety

The bucket and the RDS instance are **shared**. This project:

- writes only under `s3://$S3_BUCKET/$S3_PREFIX/`, and `cleanup.sh` refuses to
  run if that prefix is empty, `/` or `.`;
- never deletes the bucket and never touches a key outside the prefix;
- creates and drops only its own tables inside its own database, never a
  database, and never the other databases on that instance;
- stops and starts only `bench-collector` and `bench-compactor` on the two
  configured hosts;
- keeps no secret in git — `.env`, rendered configs and `*.pw` are ignored, S3
  auth is the instance role, and the catalog password is read from a file at
  process start and never copied.

## Tuning log & requirements (updated 2026-09-11)

Everything in this section was changed or discovered on 2026-09-11 during the
post-campaign tuning session. Original config files are preserved next to the
tuned ones as `config/*.yaml.bak-tuning` — `diff` them to see exactly what changed.

### Configuration changes in effect

| File | Setting | Was | Now | Why |
|---|---|---|---|---|
| `config/collector.yaml` | `ingestion.min_bucket_size` | 16 MiB | **32 MiB** | The collector writes buckets on a single writer thread: each bucket = one DuckDB COPY + one serialized PostgreSQL catalog commit. Bigger buckets halve commits/sec at the same data rate, raising the throughput ceiling. Latency at low rates is unchanged (the 5 s `max_delay_ms` timer still flushes first there). |
| `config/generator.yaml` | `pipeline_model.min_bucket_size` | 16 MiB | **32 MiB** | Mirror of the collector value (used only for the generator's preflight feasibility check — keep the two in sync). |
| `config/generator.yaml` | stress profile `workers` / `max_inflight` | 60 / 160 | **80 / 320** | Client-side headroom so the generator is never the limiter once the direct 4317 path is open. |
| `config/compactor.yaml` | `sizes.minor_compaction_max_size` | 8 MB | **48 MB** | With 8 MB, every full-size bucket file exceeded the minor threshold, so minor compaction ran but merged nothing at high rates. 48 MB puts 32 MiB bucket files inside the minor tier. |
| `config/compactor.yaml` | `sizes.major_compaction_max_size` | 64 MB | **128 MB** | Keeps a clear tier above the raised minor threshold. |

Deployed with `scripts/deploy-collector.sh --no-build` and
`scripts/deploy-compactor.sh --no-build` (both services restarted). To revert:
copy the `.bak-tuning` files back over the originals and redeploy the same way.

### Baseline vs tuned (for comparing results)

Baseline (SSH tunnel, 16 MiB buckets), two repeats on a wiped lake, both
correctness PASS with zero loss / zero duplicates:
`20260911-034705-stress` — 14,396,000 records, mean 12,781 rec/s, p50 17.8 s.
`20260911-041702-stress` — 14,444,000 records, mean 12,826 rec/s, p50 17.7 s.
Tuned runs after this point are directly comparable to these two.

### Outstanding requirements (asked for, not yet in place)

1. **Security group rule — REQUIRED for peak numbers.** Inbound TCP **4317**
   on `sg-0ea9dd40012388877`, source = the same group (self-referencing),
   region us-west-2. Until it exists, all generator→collector traffic rides an
   SSH tunnel and every throughput number is a floor, not a peak.
   Recommended in the same request: TCP 8081 (collector health), 8080
   (compactor health), 9100 (node_exporter) — same self-referencing source.
   Details: `NETWORK.md`.
2. **Server 2 instance type (recommended).** The collector host
   (`i-0be2c5bc12842b924`) is a burstable t3.large; sustained load drains CPU
   credits and throttles mid-run. A non-burstable compute type (c6i.xlarge or
   larger) removes that risk and gives DuckDB's multi-threaded Parquet writer
   real cores.

### Operational constraints to respect

- **S3 budget: 250 GB total.** Wipe the lake between heavy runs
  (`echo <bucket-name> | scripts/cleanup.sh --data --yes`). A full stress run
  peaks well under 20 GiB, so this is comfortable — but do not stack many runs
  without cleanup. Note: `cleanup.sh --yes` still reads the bucket-name
  confirmation from stdin — pipe it in for non-interactive use (as above).
- **S3 credentials expire ~6 h after each service start** (DuckDB
  credential_chain fetches the EC2 instance-profile STS token once and never
  refreshes — IMPROVEMENTS.md finding 16). Symptom: every compaction/write
  fails with ExpiredToken while `/health` still says UP. Fix until the product
  handles it: restart `bench-collector` / `bench-compactor` before any run if
  they have been up longer than ~5 h.
- The compactor `/health` field `totalFilesCompacted` staying at 0 while
  cycles "complete" usually means the minor threshold is below the bucket file
  size (that is what the 48 MB change fixes), not that the compactor is broken.
### Tuned result (2026-09-11, run `20260911-050926-stress`, still via SSH tunnel)

Correctness **PASS** (9,304,000 records, zero loss/duplicates, zero rejected).
Peak step rate **14,818 rec/s** vs 13,382–13,616 baseline (**+9%**); generator
stall fell from ~25–30% to 7.8%; compaction merged **259 files during load**
(baseline: 0 — the 48 MB minor-tier fix working). p50 latency rose 17.8 s →
22.6 s, the expected cost of 32 MiB buckets + deeper client inflight. The run
self-stopped at step 3 (plateau detection). Direct-4317 comparison still
pending the security-group rule.

### Direct-path result (2026-09-11, run `20260911-063118-stress`, transport=direct)

Port 4317 opened (rule sgr-0ef64d09c99bd4b5a). Correctness PASS — 5,400,000
records, zero loss/dup/rejected. Sustained acceptance at 20k offered:
**12,799 rec/s with 0% generator stall** (tunnel runs: 25–30% stall). Stop rule
`accepted_ratio_below: 0.90` fired in step 1. Conclusion: the tunnel throttled
the offered path but was NOT the throughput ceiling — the serialized
COPY→catalog-commit writer cycle is (~13k/s at 32 MiB buckets on 2 vCPU).
Next gain requires writer pipelining (see final_report/COMPETITIVE_ANALYSIS.md),
not infrastructure. Caveat: t3 CPU credits were depleted by a full day of load;
re-baseline on fresh credits or c6i before quoting a headline number.

### Peak-configuration experiment (2026-09-11, after the direct-path run)

Goal: highest sustainable rate from the CURRENT binaries via configuration only.
Changes on top of the tuned config (backups: `config/*.yaml` history above,
`collector/config/application.conf.tmpl.bak-peak`,
`compactor/config/application.conf.tmpl.bak-peak`):

| Setting | Tuned | Peak | Why |
|---|---|---|---|
| collector `min_bucket_size` | 32 MiB | **64 MiB** | halve serialized catalog commits/sec again; the direct run proved the writer cycle is the ceiling |
| collector `duckdb.threads` | 2 | **4** | DuckDB guidance: threads > cores helps remote-I/O-bound work (S3 waits) |
| collector startup script | — | **`SET preserve_insertion_order = false`** | documented faster/lower-memory COPY; ordering guarantee lives in the producer-seq ledger, not file order |
| both startup scripts | — | **`REFRESH auto` on the S3 secret** | P0 fix for the 6-hour STS expiry (IMPROVEMENTS.md #16); accepted by DuckDB 1.5.4 at startup; full proof needs a >6 h soak |
| compactor minor/major | 48/128 MB | **96/256 MB** | keep minor = 1.5× bucket so the minor tier still has work at 64 MiB files |

Also this session: TEST13/14 harness bug fixed (`bench/track_a.py` — DuckDB 1.5.4
removed `duckdb_databases().temporary_storage_bytes`; now reads
`sum(size) FROM duckdb_temporary_files()`; backup `bench/track_a.py.bak-test13fix`).
Rerun results in `results/track-a-rerun-test13-14/` — all 6 points OK; note the
1M-row quick dataset never actually spills (spill_bytes 0 even at 512 MB).

CPU-credit check performed under full 2-core load on the collector: steal time
0–1 %, i.e. **no t3 throttling was occurring** — earlier suspicion of
credit-depletion as a variance source is withdrawn; run-to-run variance
(12.8–14.8 k) is now attributed to workload/lake-state differences, not AWS.

Bucket-cleanup policy CHANGED per AWS confirmation of unlimited S3: the lake is
NO LONGER wiped between runs by default (validation filters per-run by gen_id +
time window, so accumulation is harmless). Wipe only when a comparison needs a
fresh lake: `echo <bucket> | scripts/cleanup.sh --data --yes`.

### Peak iteration #1 result (run `20260911-065650-stress`, direct, 64 MiB + threads=4 + PIO=false)

Step-1 sustained **12,544 rec/s** (ratio 0.8711, stop rule fired) vs 12,799 at
32 MiB — **a −2 % change, i.e. noise. Doubling the bucket size did not move the
rate.** All 5.4 M offered records were eventually accepted (0 rejected, 0
failed); p50 21.9 s at saturation; stall 0 %.

**Conclusion — configuration peak reached.** Rate is invariant to bucket size,
so the single writer is *throughput-bound* (~6.3 MB/s of encode+upload+commit
work on 2 shared vCPUs), not fixed-cost-per-cycle-bound. No remaining config
knob adds CPU or lanes. The pipeline's config-only peak on this hardware is
**~12.5–12.8 k rec/s sustained (direct, saturated)** / ~14.8 k best step under
favourable conditions. Next gains require the writer-pipeline code change
(COMPETITIVE_ANALYSIS.md §6) or more vCPUs. Final config kept: 64 MiB buckets
(same rate, half the catalog txns, compaction-friendlier files), threads=4,
preserve_insertion_order=false, REFRESH auto, compactor 96/256 MB.

### Batching A/B (2026-09-11 afternoon, per protocol) — throughput-neutral, reverted

Three configs, same 2-vCPU host, same 4-lane build, same workload, all VERDICT PASS
(zero loss/dup/rejected): 1 MiB/5 s -> 14,416 rec/s; 32 MiB/1 s -> 14,647; 64 MiB/5 s
-> 14,780 (step-1 sustained). Spread +-1.3 % = noise; improvement ~0 %.
Mechanism: BulkIngestQueue.processWriteQueue greedily COMBINES queued buckets (up to
max_bucket_size 100 MB) before writing, so the pipeline self-batches at saturation and
min_bucket_size only governs file size + low-rate flush latency. Bottleneck remains
host CPU (~14.4-14.8k with >=2 lanes). Final kept config: 64 MiB / 5 s /
writer_parallelism=2 (4 lanes showed zero gain over 2 on 2 vCPU).

Writer-lane curve (same day, code change on branch performance-benchmark-spec):
1 lane 12,799 (ratio 0.889) -> 2 lanes 14,759 (1.000) -> 4 lanes 14,780 (1.000).

### CREDENTIAL SOAK RESULT — REFRESH auto FAILED on DuckDB 1.5.4 (2026-09-11 12:55Z)

The compactor, deployed 06:55:55Z with `CREATE OR REPLACE SECRET (TYPE S3,
PROVIDER credential_chain, REFRESH auto, ...)`, began failing every S3 operation
at **12:55:37Z — the 6-hour STS boundary to the minute** (ExpiredToken count
654 -> 684, minor/housekeeping cycles erroring). Conclusion: on the deployed
DuckDB 1.5.4, `REFRESH auto` does NOT recover from instance-profile STS expiry
in this failure mode (S3 returns HTTP 400 ExpiredToken; the documented
credential re-fetch hooks appear to cover 401/403 only). **The config-only fix
is INSUFFICIENT — the code-level fallback is REQUIRED**: catch
ExpiredToken/HTTP 4xx auth failures in the write/compaction paths, re-execute
the CREATE OR REPLACE SECRET via ConnectionPool.executeOnSingleton, retry once,
exponential backoff, plus consecutive-failure health degradation
(IMPROVEMENTS.md findings 16/17; design in final_report/COMPETITIVE_ANALYSIS.md
section 10). Until that ships: restart both services on a <6 h schedule
(systemd timer) as the operational stopgap. Compactor restarted 12:57:32Z,
healthy. Collector (restarted 10:33Z) crosses its own boundary ~16:33Z — its
failure/recovery will be observed via the live trickle as a second data point.

### Crash-safety of the parallel-writer code (SIGKILL, 2026-09-11 13:20Z) — PASS

Hard SIGKILL of the collector mid-flush with writer_parallelism=2 active
(run 20260911-131646-recovery-collector-sigkill): detection 0.7 s, outage 11.5 s
(systemd auto-restart), 391,000 acked records, 391,000 landed — ZERO acked-loss,
ZERO duplicates. Concurrent per-bucket producer-sequence rollback under two
lanes preserves the exact single-writer guarantee. The writer-parallelism change
is crash-safe and cleared for production.

### FINAL SOAK & ENGAGEMENT SUMMARY (2026-09-11 ~16:30 UTC)

Credential soak, 39 ticks over ~9h, continuous 150 rec/s trickle (paused only for
benchmarks). VERDICT: REFRESH auto config fix **FAILED** — compactor resumed
ExpiredToken at 12:55:37Z (6h boundary, exactly), recovered by restart 12:57:32Z.
Collector was restarted several times by the day's tests (last 13:20:57Z after the
SIGKILL test), moving its boundary to ~19:20Z — its first-crossing confirmation is
still pending tonight and is covered by the 20-min watch. Conclusion stands: the
code-level catch-and-recreate-secret fix is REQUIRED (IMPROVEMENTS #16; design in
COMPETITIVE_ANALYSIS.md §10). Operational stopgap: <6h scheduled service restarts.

Verified today: 22 correctness-PASS runs, 144,815,300 accepted records, 0 loss /
0 duplicates across every run. Peak validated rate 15,036 rec/s (max-stress, run
20260911-150804). Writer-parallelism (code change, branch performance-benchmark-spec)
crash-safe under SIGKILL. Trickle left running; soak script self-terminates 05:00Z.
