# Test plan

The methodology. What counts as a result, what does not, and why.

---

## Two tracks, never mixed

| Track | Question | Runs on |
|---|---|---|
| **A** — engine | What can DuckDB 1.5.4 do on this hardware with no DazzleDuck code in the path? | one machine, standalone |
| **B** — pipeline | What can the OTLP-to-compacted-Parquet pipeline sustain? | all three together |

Track A is the **ceiling**. Track B is the **product**. A Track A number is
never reported as system capacity, and a Track B number is never reported as a
DuckDB number. The report states which track every figure came from.

This harness implements **Track B**. Track A belongs in a separate standalone
script; `config/duckdb.yaml → sweeps` holds the parameter matrix, sized for the
disk actually available.

---

## The definition everything hangs on

A rate is **sustainable** only if, for the whole level:

```
accepted_rps >= 0.99 * offered_rps
B2 backlog slope over the steady-state window is not positive
no RESOURCE_EXHAUSTED
zero data loss and zero duplication (validated after the fact)
no OOM, no process restart
CPU, memory and disk within the limits recorded in environment.json
```

Every clause earns its place:

- **accept ratio alone is not enough.** A pipeline can ack every record while
  accumulating small files it will never catch up on. It passes today and fails
  on Thursday.
- **backlog alone is not enough.** A pipeline that rejects half the offered load
  has a beautifully flat backlog.
- **correctness alone is not enough**, and neither is performance without it. A
  throughput figure from a run that lost rows is reported *with the failure
  attached*, never on its own.

The **maximum sustainable rate** is the highest level meeting all of these. It
is reported separately from the highest instantaneous accepted rate, because
those are different claims and only the first describes what the system can be
run at.

---

## Three rates, never conflated

```
offered    records the generator attempted to submit
accepted   records the collector acked
rejected   records in RPCs that returned RESOURCE_EXHAUSTED
```

`offered` is a property of the generator process. Reporting it as throughput is
the single most common way a pipeline benchmark ends up wrong.

```
offered - accepted  = rejected or failed   expected under backpressure; not loss
accepted - landed   = DATA LOSS            hard failure
landed - accepted   = DUPLICATION          hard failure
```

---

## Open loop, and why

The generator's pacer decides when a batch *should* be submitted from the target
rate and nothing else. It never waits for the server.

A closed-loop generator — submit, wait for the ack, submit again — cannot
measure saturation, because its offered rate is defined by the server's speed.
The server always appears to be keeping up, right up to the moment it isn't.

The open loop has exactly one bound, `max_inflight`, and it is accounted for
rather than hidden: when every slot is busy the pacer records `generator_stalled_ms`
and carries the deficit forward. Any sustained stall means the offered rate was
not actually offered, and the report says so instead of quietly reporting a
lower rate as if it had been requested.

---

## Step duration

A staircase step must contain the slowest periodic process, or the backlog slope
is noise:

```
step_duration >= max(10 * max_delay_ms, 5 * major_compaction_frequency, 10 min)
```

With `max_delay_ms = 5000` and `major_compaction_frequency = 10 minutes` that is
50 minutes per step, and an 8-level staircase is about 7 hours.

The default here is **10 minutes per step**, a deliberate compromise for a first
pass. It contains ten flush cycles and one major cycle — enough to see a trend,
not enough to be conclusive about slow drift. Runs at this duration are labelled
as indicative in the report.

**If you shorten steps, shorten `major_compaction_frequency` in proportion.**
Shortening the step alone changes what the backlog slope means and silently
breaks comparison with earlier runs.

### Warm-up

The first 25% of each level is discarded before fitting the backlog slope.
Buckets flush on a timer and compaction runs on its own schedule, so the start
of any level is transient by construction; fitting through it measures the
warm-up. The discard window is recorded in the report.

---

## Correctness

Enabled by per-record identity: every record carries `bench.gen_id` and
`bench.seq`.

After every Track B run, against rows in the run's time window:

1. **row count** — landed must equal acked, exactly
2. **duplicates** — no `(gen_id, seq)` may appear twice
3. **contiguity** — per generator, every seq in the attempted range is either
   landed or explained by a rejection/failure
4. **watermark agreement** — `sum(row_count)` from `ingest_watermark`, committed
   in the same transaction as the file registrations, must match the table. An
   independent witness: disagreement means the two halves of that transaction
   diverged.
5. **checksums** — `sum(hash(col))` per column, for comparing across a merge

Checks 1–3 are hard gates. `VERDICT: FAIL` means no performance number from that
run stands on its own.

### Across a compaction

Re-run all of the above plus: row count unchanged, per-column checksums
unchanged, schema unchanged, every registered file readable. A merge that
changes one value is a correctness failure no row count would catch.

```bash
./scripts/validate-correctness.sh <test-id> --baseline <earlier>/correctness.json
```

---

## Repeats

- **Track A**: 1 warm-up (discarded) + 5 measured runs. Report min, max, mean,
  median, stddev. Never only the best run.
- **Track B**: staircase levels run once each — they are long. The soak is the
  repeat. Any Track B conclusion from a single short run is labelled as such,
  and the report's "what this run does not establish" section does that
  automatically.

---

## Same input

Datasets and payloads are deterministic from a seed. The same seed produces a
byte-identical record stream, which is what makes compression ratios and
file-size comparisons valid across runs. The seed is recorded in
`metadata.json`; the whole environment is hashed into `env_hash` so a reader can
tell whether two runs are comparable at all.

---

## Failures are results

Every OOM, timeout, crash, disk-full, query error, rejected ingestion, memory
breach and container restart is written into the run's output with a status and
an error. Nothing is rerun-until-green and silently replaced. If a run is
repeated, **both** the failed and the successful run are kept.

`collect-results.sh` extracts error and warning lines from both service logs
into `logs/<role>.errors.log` and reads `NRestarts` from systemd, so a restart
during a measured level cannot go unnoticed.

---

## Cold versus warm

- **Warm**: run the query twice, measure the second.
- **Cold**: needs root, `sync; echo 3 > /proc/sys/vm/drop_caches`, and must be
  *verified* by watching disk read bytes actually rise. If it cannot be
  verified the run is labelled `warm-unknown` — never claimed as cold.
- **Object storage**: there is no local page cache to drop for S3. Track B
  numbers are inherently warm-unknown with respect to S3.

---

## Phases

| Phase | Work | Status here |
|---|---|---|
| P0 | Close the instrumentation gaps; build the generator | **done** — generator, pipeline exporter (compactor scrape + backlog), host agents, per-record identity |
| P1 | Provision servers, storage, catalog; capture environment | **done** — catalog on RDS, data on S3, both services under systemd, `collect-env.sh` |
| P2 | Track A: engine sweeps | **not run** — separate harness; matrix in `config/duckdb.yaml` |
| P3 | Pick pipeline settings from P2 | using documented defaults |
| P4 | Track B: TEST 16, then the staircase | **blocked on the security group** |
| P5 | Soak 6 h → 12 h → 24 h; drain after each | blocked on P4 |
| P6 | Fault injection | implemented; blocked on P4 for the load half |
| P7 | Correctness validation and report | **done** — automated per run |

Of the source spec's five instrumentation gaps, four are closed by this harness
(compactor scrape, backlog metric, per-record identity, the standalone
generator). **G1 is not**: the collector's own Micrometer meters — including
`data_phase_ms` vs `post_ingest_phase_ms`, the one diagnostic that would say
whether DuckDB or the catalog is the constraint — need a change inside the
collector. See [IMPROVEMENTS.md](IMPROVEMENTS.md).

---

## What this hardware rules out

Three t3.large: 2 vCPU, 7.6 GiB, 8 GiB root with ~5 GiB free, no swap.

| Spec asks for | Here |
|---|---|
| 100M / 500M-row datasets | `NOT TESTED` — do not fit on an 8 GiB volume |
| 100 GB / 500 GB / 1 TB major compaction | `NOT TESTED` — free space is ~5 GiB |
| 10 000 × 1 MB fragmentation layout | reduced to 1 000 × 1 MB |
| `threads = 1|2|4|8|16` | capped at 2 |
| Non-burstable CPU | t3 burstable; credit exhaustion looks like a regression |
| Concurrent minor + major compaction | not possible with one compactor — a major run *replaces* the minor tick. Reported as sequential (variant C3). |

None of these are estimated. `NOT TESTED` with the reason is the correct entry,
and the report generator emits it automatically when the input file is absent.
