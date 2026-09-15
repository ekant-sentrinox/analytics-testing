# Reading results

How to read a report without over-claiming, and what each file in a run
directory is for.

---

## A run directory

```
results/20260910-153000-throughput/
├── report.md               generated; never hand-edit
├── charts/*.png
├── metadata.json           config echo, host facts, clock offset, git commit
├── manifest.json           the reconciliation contract
├── generator.json          final totals + per-step results
├── generator.jsonl         per-second samples
├── generator.csv           the same, flat, for plotting
├── generator.log           stdout
├── correctness.json        loss / duplication / gap verdict
├── correctness.txt         the same, human-readable
├── environment.json        all three servers, effective config, env_hash
├── preflight.txt
├── state-before.json       catalog + S3 + service health before
├── state-collected.json    ... and after settling
├── run.json                exit code, wall time, settle window
├── raw/
│   ├── pipeline.jsonl      backlog, lag, compaction counters, PG stats
│   ├── host-generator.jsonl
│   ├── host-collector.jsonl
│   ├── host-compactor.jsonl
│   ├── compaction.jsonl    parsed from the compactor log
│   └── *-unit.txt          systemd NRestarts, MemoryCurrent
└── logs/
    ├── collector.log  collector.errors.log  collector-gc.log
    └── compactor.log  compactor.errors.log  compactor-gc.log
```

`report.md` is generated from these by `bench/report.py`. If a number looks
wrong, the file it came from is named next to it in the report — go and read it.

---

## The three rates

Everything else depends on getting this right.

| | |
|---|---|
| **offered** | records the generator *attempted*. A property of the generator process. **Not throughput.** |
| **accepted** | records the collector *acked*. The export RPC does not return until the batch is written to Parquet and committed to the catalog, so accepted means **persisted**. |
| **rejected** | records in RPCs that returned `RESOURCE_EXHAUSTED` — the collector explicitly refusing work. |

```
offered - accepted  = rejected or failed   expected under backpressure; NOT loss
accepted - landed   = DATA LOSS            hard failure at any throughput
landed - accepted   = DUPLICATION          hard failure
```

If someone quotes "we did 20k records a second", the only follow-up that matters
is: *offered or accepted, and was the backlog flat?*

---

## Sustainable versus peak

The report gives two numbers and keeps them apart.

**Maximum sustainable rate** — the highest level where all of these held:

```
accepted / offered >= 0.99
backlog slope over the steady-state window not positive
zero RESOURCE_EXHAUSTED
no restart, no OOM
correctness PASS
```

**Highest instantaneous accepted rate** — the best one-second sample.

The second is always larger and is almost never a number you can run a system
at. Quote the first.

If the report says a sustainable figure is **provisional**, the backlog series
was missing and only the accept-rate half of the criterion could be checked.
That is a weaker claim and is labelled as one.

---

## Backlog — three quantities

Never averaged together.

| | what | how to read it |
|---|---|---|
| **B1** | collector in-memory queue depth | **not available** — see below |
| **B2** | uncompacted files in the catalog | the one that matters. Rising in steady state = compaction is losing. |
| **B3** | end-to-end visibility lag | how stale a query against the lake is |

**B2 slope is the sustainability test.** The report fits a least-squares line
over the steady-state window (first 25% discarded as warm-up) and reports
files/min. Positive means the collector is creating files faster than the
compactor merges them — the system is falling behind *while acking everything*.
That level is not sustainable however good the accept rate looks.

**B1 is unavailable and this is a real gap.** `writer.pending_batches` and
`writer.pending_buckets` are registered in the collector's `SimpleMeterRegistry`
and never exported. The consequence: there is no early warning of queue
saturation. The first visible sign is `RESOURCE_EXHAUSTED`, by which point the
queue is already full. When the report says B1 is `NOT AVAILABLE`, that is a
missing instrument, not an empty queue.

---

## Latency

Measured around the **whole export RPC**, which includes queue wait, the DuckDB
`COPY` to Parquet, and the DuckLake catalog commit. It is persistence latency,
not network round trip.

So p99 in the hundreds of milliseconds is normal and healthy. A p99 of 2 ms
would be suspicious — it would suggest the ack is not waiting for durability,
which would make "accepted" meaningless.

A rising p99 with a flat accept rate usually means the writer is queueing:
compare against B2 and `min_bucket_size`.

### Latency pinned at `max_delay_ms` — read this before reporting a low rate

If p50 sits at roughly **5000 ms** and barely moves, the run was
**latency-bound, not throughput-bound**, and the accepted rate says nothing
about capacity.

A bucket flushes when it reaches `min_bucket_size` (16 MiB) **or** after
`max_delay_ms` (5 s), whichever comes first. At low rates the bucket never
fills, so every flush waits the full timer — and because the RPC does not ack
until the flush completes, every client request waits with it. That gives a hard
ceiling on what any durability-respecting client can offer:

```
max_rps  =  workers x batch_size / flush_seconds
flush_seconds = min(max_delay_ms/1000,  min_bucket_size / (rps x record_bytes))
```

Measured here, and the reason this section exists:

| | |
|---|---|
| Profile | `light`, target **1000 rec/s** |
| Concurrency | 4 workers x 200 records |
| Predicted ceiling | 4 x 200 / 5 s = **160 rec/s** |
| **Observed** | **161 rec/s** |
| p50 latency | **4998 ms** — the flush timer, to within 2 ms |
| Pacer stalled | 262 s of a 383 s run (68%) |

Nothing was wrong with the pipeline. The client could not ask for more.

The generator now computes this ceiling from `generator.pipeline` in
`config/generator.yaml` and logs `UNREACHABLE RATE` at startup when a target
exceeds it; the warnings are also recorded in `metadata.json`. If you see that
warning, raise `workers` or `batch_size` — do not report the resulting rate as a
throughput result.

Two corollaries worth keeping in mind:

- **The crossover.** Above roughly 33k rec/s the 16 MiB size trigger fires
  before the 5 s timer, flush latency falls, and the concurrency requirement
  eases. Low-rate and high-rate runs are therefore limited by *different*
  mechanisms — which is also why the compaction tier doing the work changes with
  load.
- **`stalled_ms` is the tell.** A large stall with low generator CPU means the
  in-flight bound is masking how far behind the pipeline is, or that the target
  was never reachable. Check it before concluding anything about capacity.

---

## Correctness

```
VERDICT PASS
  PASS  row_count      exact match
  PASS  duplicates     no (gen_id, seq) appears twice
  PASS  contiguity     every seq accounted for as landed, rejected or failed
  PASS  watermark      watermark agrees with the table
```

Three hard gates: `row_count`, `duplicates`, `contiguity`. Any of them failing
makes the verdict `FAIL`, and **a performance number from a failed run is
reported only with the failure attached** — never on its own.

`watermark` is an independent witness: the watermark row is committed in the
same transaction as the file registrations, so if it disagrees with the table,
the two halves of that transaction diverged.

Note what is *not* a failure: `offered - accepted`. Records the collector
refused were never promised. Records it acked and then lost are a different
thing entirely.

---

## Resource utilisation

1 s samples per host. Read alongside the rates:

| pattern | means |
|---|---|
| collector CPU ~100%, accept rate flat | the collector is the constraint |
| **generator** CPU ~100%, large `stalled_ms` | **the generator is the constraint** — this run measured the load generator, not the pipeline |
| compactor CPU high, B2 still rising | compaction cannot keep up at this file-production rate |
| all CPU low, rate flat | look at the catalog: commits/s, connections, longest query |

`process_restarts` and `oom_kills` are results. A restart during a measured
level invalidates that level, and the report calls it out.

> These are burstable **t3.large** instances. CPU credit exhaustion throttles
> the vCPU and looks exactly like a software regression. Before concluding
> anything from a late-run slowdown, check `CPUCreditBalance`.

---

## Storage and compaction

The before/after table is where compaction becomes legible:

| | before | after | delta |
|---|---|---|---|
| Live data files | 12 | 47 | +35 |
| Live bytes | 190 MiB | 812 MiB | +622 MiB |
| Files below minor threshold | 2 | 31 | +29 |
| Mean live file size | 15.8 MiB | 17.3 MiB | |

Small files growing faster than total files means compaction is losing ground.
Mean file size should rise while merging is happening and there is a backlog to
merge; flat and small means merges are not running or not helping.

**S3 objects far above live files** is normal for a while: files superseded by a
merge are reclaimed by `ducklake_cleanup_old_files` on the housekeeping timer.
Growing across several cycles means housekeeping is not running.

---

## `NOT MEASURED`

The report writes `NOT MEASURED` and names the file it wanted, rather than
estimating or quietly omitting the section. A missing measurement and a
measurement of zero are different things, and only one of them is a result.

Common causes: host agents not started, the pipeline exporter down,
`collect-results.sh` not run, or — for the collector and compactor panels — the
security group blocking the health ports.

The report's closing **"what this run does not establish"** section lists these
explicitly, along with structural limits like "a single rate was offered, so the
ceiling was not searched for" and "the run was 60 s, too short to contain
several compaction cycles".

---

## Comparing two runs

Check `env_hash` in `environment.json` first. It is a hash of the whole captured
environment — hosts, instance types, effective collector and compactor config,
DuckDB and Java versions. **Different hash, different system**: the runs may not
be comparable, and the report will not tell you they are.

Then check the seed in `metadata.json`. The same seed produces a byte-identical
record stream, which is what makes compression ratios and file-size comparisons
valid.

For a compaction comparison, use the checksum gate:

```bash
./scripts/validate-correctness.sh <after> --baseline results/<before>/correctness.json
```

Adds a `checksums_stable` check — per-column `sum(hash(col))` before and after.
A merge that changes one value is a correctness failure no row count would
catch.

---

## Writing up

- No number without a source file.
- No Track A (engine) number presented as system capacity.
- No generator rate presented as throughput.
- Anything not run says `NOT TESTED`, with the reason.
- Failures are reported, not re-run until green. If a run was repeated, both are
  kept.
- Peak and sustainable are different claims. State which one.

The report generator already follows these. The risk is in the summary someone
writes on top of it.
