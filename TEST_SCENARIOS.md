# Test scenarios

Every test, what it is for, how to run it, and what makes it pass.

---

## Smoke

**Question:** does the whole path work at all?

```bash
./tests/smoke/run.sh
```

10 records/s for 60 s, then read every acked record back out of the lake by
`bench.gen_id`. Cheap enough to run before anything else and after every config
change.

Exercises: JWT signing and verification, the `x-dd-ingestion-queue` claim, OTLP
conversion, Arrow IPC, the DuckDB `COPY`, the S3 write, the DuckLake catalog
commit, the watermark, and the read back.

**Pass:** generator exits 0 · accepted == offered · correctness `PASS` · report
generated.

At 10 rec/s nothing should ever be refused. A rejection here is a
misconfiguration, not a capacity limit.

---

## Functional

**Question:** are the collector's contracts actually enforced?

```bash
./tests/functional/run.sh
```

Against the running service, not the source:

| | expected |
|---|---|
| no `Authorization` header | `UNAUTHENTICATED` |
| malformed bearer token | `UNAUTHENTICATED` |
| token signed with the wrong key | `UNAUTHENTICATED` |
| valid token, no queue claim | `INVALID_ARGUMENT` — there is no default queue |
| valid token, unknown queue | `INVALID_ARGUMENT` / `NOT_FOUND` |
| valid token, correct queue | `OK` |
| **ack waits for persistence** | RPC takes non-trivial time |
| records land with identity intact | queryable by `bench.gen_id` |

The last two matter most. If the ack did not wait for durability, every latency
number in the whole harness would be meaningless and "accepted" would not imply
"persisted".

---

## Light / medium / high

**Question:** how does the pipeline behave at a fixed, known rate?

```bash
./scripts/run-test.sh light     # 1k rec/s, 5 min
./scripts/run-test.sh medium    # 5k rec/s, 10 min
./scripts/run-test.sh high      # 10k rec/s, 30 min
```

Fixed-rate runs. Useful as a background load while a fault is injected, and for
watching one level closely instead of inferring it from a staircase.

**Pass:** accepted ≈ offered · no rejections · backlog flat · correctness `PASS`.

---

## Throughput staircase

**Question:** what is the maximum *sustainable* rate?

```bash
./tests/throughput/run.sh
./tests/throughput/run.sh --steps "1000,5000,10000,15000" --step-duration 900
```

Levels run back to back. Each is judged on all of:

```
accepted / offered >= 0.99
backlog slope over the second half is not positive
zero RESOURCE_EXHAUSTED
no restart, no OOM
```

and the run as a whole on `correctness == PASS`.

Output:

```
  step  target rps   accepted/s   ratio    rejected   backlog slope   verdict
     0       1,000        1,000  1.0000           0          -0.10   SUSTAINED
     1       5,000        4,998  0.9996           0          +0.05   SUSTAINED
     2      10,000        8,210  0.8210      41,203          +7.30   FAILED
        └─ accepted/offered 0.8210 < 0.99
        └─ 41,203 records rejected
        └─ backlog +7.30 files/min

  MAXIMUM SUSTAINABLE RATE: 5,000 records/s
  Highest instantaneous accepted rate: 8,210 records/s — a different claim.
```

**Step duration is not a free parameter.** Below ~10 minutes a step may contain
no major compaction cycle, and the backlog slope becomes noise. Shortening steps
requires shortening `major_compaction_frequency` in proportion. The test warns
when a step is under 10 minutes.

---

## Soak

**Question:** does it still work in six hours?

```bash
./tests/soak/run.sh --hours 6  --rps 3000
./tests/soak/run.sh --hours 12 --rps 3000
./tests/soak/run.sh --hours 24 --rps 3000
```

Rate should be **70–80% of the measured sustainable figure**. Running a soak at
the ceiling tests the ceiling, not stability.

The failure this catches is slow: backlog, RSS, GC time, file count and catalog
size trending up over hours while every instantaneous metric looks fine. Hourly
checkpoints plus a first-hour vs last-hour comparison make it visible:

```
  metric                             first hour      last hour   drift
  ------------------------------------------------------------------------
  accepted rec/s                       2,998.40       2,997.10   stable
  latency p99 (ms)                       412.00         489.00   DRIFT +18.7%
  backlog files (B2)                      34.20          58.90   DRIFT +72.2%
  collector RSS (MiB)                  1,204.00       1,388.00   DRIFT +15.3%
```

**Pass:** every row `stable` · zero restarts · zero OOM · correctness `PASS`.

A monotonic drift in backlog, RSS or lag **is** the finding, even when
throughput held. That is what breaks on day three.

> On t3 instances, check CPU credit balance before attributing a late-run
> slowdown to software. Credit exhaustion throttles the vCPU and looks identical
> to a regression.

---

## Drain

**Question:** after a spike, how long until queries are fast again?

```bash
./tests/drain/run.sh [<test-id of the run that created the backlog>]
```

Stop the generator, leave the collector and compactor running, sample B2 every
10 s until steady state. Reports drain time and drain rate in files/min and
MiB/min.

Steady state means the backlog **stopped falling** — not that it reached zero.
It never reaches zero: a merge produces a file, and residual traffic registers
more. Waiting for zero would hang forever.

A backlog that never falls means either there was nothing to compact (all files
already above `minor_compaction_max_size`) or the compactor is not merging.
Check `totalFilesCompacted`.

---

## Stress

**Question:** where does it break, and how?

```bash
./tests/stress/run.sh --start 10000 --step 10000 --max 60000 --hold 180
```

Different question from the staircase. That one finds the highest rate the
system can *hold*; this one finds where it *breaks*. Stops at the first of:

- throughput plateau — accepted stops rising while offered does
- error rate above 5%
- p99 above 60 s
- any `RESOURCE_EXHAUSTED`
- a component going down

**The result must be attributed correctly.** SERVER 1 is a 2-vCPU t3.large
running a Python generator; somewhere in the tens of thousands of records/s it
is plausible that the *generator* saturates first. The test records generator
CPU and pacer stall at every level and says so explicitly:

```
  CAUTION: at the final level the generator host was at 94% CPU with 31% pacer
  stall. This level measured the LOAD GENERATOR, not the pipeline.
```

To push past it: raise `batch_size` so fewer, larger RPCs carry the same record
rate, or run generators on more than one host.

---

## Recovery

**Question:** what does a component failure cost?

```bash
./tests/recovery/run.sh collector-sigterm
./tests/recovery/run.sh collector-sigkill
./tests/recovery/run.sh compactor-sigkill
```

Steady load, fault injected mid-run, generator keeps running through it.
Measures failure time, detection time, recovery time, whether systemd
self-healed, records lost, duplicates, and throughput after recovery.

| Fault | Expected |
|---|---|
| `collector-sigterm` | **zero loss.** SIGTERM enters MAINTENANCE, serves 503 on `/health` for the LB-drain window, then flushes in-flight batches before stopping. |
| `collector-sigkill` | The drain is skipped, so un-acked in-memory work is gone. **Acked-but-missing must be zero** — the collector must never ack a record it has not persisted. Any loss here is a protocol bug, not an artefact of the kill. |
| `compactor-sigkill` | **Zero loss, zero duplication.** A merge commits atomically, so an interrupted one leaves orphaned Parquet on S3, not a broken catalog. Orphans are reclaimed by `ducklake_cleanup_old_files`; compare S3 object count against live file count to see them. |

`retry_mode: none` throughout — a fault must show up as failed RPCs, not be
smoothed over by a polite client.

---

## Failure matrix

```bash
./tests/failure/run.sh --list
./tests/failure/run.sh --all
./tests/failure/run.sh memory-pressure
./tests/failure/run.sh disk-pressure
```

**Implemented**

| Fault | Method | Measures |
|---|---|---|
| collector SIGTERM / SIGKILL | `systemctl kill` | loss, duplicates, recovery |
| compactor SIGKILL | `systemctl kill` | orphans, catalog consistency, resumption |
| memory pressure | systemd `MemoryMax` drop-in, then load until OOM | OOM kill, restart, **whether acked data survived** |
| disk pressure | `fallocate` ballast to 95% on SERVER 2 | error class, and whether the failure is clean or acks un-persisted writes |

Both pressure tests restore the original state on exit, including on Ctrl-C.

**Not implemented here, with reasons**

| Fault | Why not |
|---|---|
| catalog unavailable | The catalog is a **shared** RDS instance with other databases on it (`bench_150k`, `bench_fixture`, `bench_meta`, `ollylake_meta`). Stopping it would affect work that is not ours. Run against a dedicated instance. |
| storage latency (`tc netem`) | S3 is reached directly over the AWS network; there is no proxy or route we control. Feasible against a local MinIO, not against real S3. |
| network partition | The route table and security group are not writable from the instance role. |

These are recorded as `NOT TESTED` with the reason, not omitted.

---

## Ingestion plus compaction

Covered by any Track B run: the compactor is always running. What this
environment **cannot** produce is genuinely *concurrent* minor and major
compaction — in `CompactionService.runCompaction` a due major run **replaces**
that tick's minor run.

| Variant | Description | Here |
|---|---|---|
| C1 | Two catalogs, one compactor each, staggered | not deployed |
| C2 | Two compactors, one catalog, different sizes/frequencies — also exercises concurrent-writer conflicts | not deployed |
| **C3** | Single compactor, sequential minor/major | **what runs here** |

Any claim of "concurrent compaction" from this deployment would be wrong. The
report records C3.

---

## Order for a campaign

```bash
./scripts/preflight.sh
./tests/functional/run.sh
./tests/smoke/run.sh
./tests/throughput/run.sh                 # gives the sustainable rate
./tests/soak/run.sh --hours 6 --rps <75% of it>
./tests/drain/run.sh
./tests/recovery/run.sh collector-sigterm
./tests/recovery/run.sh collector-sigkill
./tests/recovery/run.sh compactor-sigkill
./tests/stress/run.sh
./scripts/generate-report.sh --all
```

Faults last, and never during a soak that is producing a headline number.
