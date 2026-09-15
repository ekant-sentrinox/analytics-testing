# Generator (SERVER 1)

An **open-loop, rate-controlled OTLP/gRPC log generator** with per-record
identity, three-way rate accounting and a Prometheus endpoint.

This is the missing piece the benchmark spec calls G3: the repo's
`OtelCollectorBenchmark` is in-process, test-scope and **closed-loop** — its
in-flight count is capped at the client count, so its offered rate is defined by
the server's speed. That cannot measure saturation.

---

## Why open loop

The pacer decides when a batch *should* be submitted from the target rate and
nothing else. It never waits for the server.

A closed-loop generator — submit, wait for the ack, submit again — always makes
the server look like it is keeping up, right up to the moment it isn't, because
the client slows down in lockstep with it.

The open loop has exactly one bound, `max_inflight`, and it is **accounted for
rather than hidden**: when every slot is busy the pacer records
`generator_stalled_ms` and carries the deficit forward on the ideal timeline.
A sustained stall means the offered rate was not actually offered, and that
appears in the results instead of quietly becoming a lower rate reported as if
it had been requested.

---

## Modules

```
generator/src/
├── config.py       YAML + profile overlay + validation. CLI > profile > defaults.
├── payload.py      Deterministic OTLP records; template pool; seq stamping.
├── otlp_client.py  JWT minting, gRPC channels, RetryInfo extraction.
├── metrics.py      Counters, latency, 1 Hz JSONL sampler, Prometheus.
├── loadgen.py      The pacer, the worker pool, staircase execution.
└── main.py         CLI, metadata, manifest, summary.
```

Deliberately not one script: `loadgen.py`'s pacing logic is the part that has to
be right, and it should be readable without scrolling past payload construction.

---

## Running

```bash
python -m generator.src.main --config config/generator.yaml --profile smoke

# overrides win over the profile
python -m generator.src.main --profile medium --rps 8000 --duration 1200

# validate config and connectivity, send nothing
python -m generator.src.main --profile smoke --dry-run
```

Normally driven by `scripts/run-test.sh`, which adds preflight, state snapshots,
result collection, correctness validation and the report.

---

## Per-record identity

Every record carries:

- `bench.gen_id` — UUID per generator process, also on the Resource
- `bench.seq` — monotonic within that generator, contiguous across the run

Without these, "zero data loss" is an assertion. With them it is a query. See
[../TEST_PLAN.md](../TEST_PLAN.md#correctness).

---

## Performance notes

Two decisions keep a Python generator from becoming the bottleneck on 2 vCPUs:

**A prebuilt template pool.** Constructing a fresh protobuf with ~20 attributes
per record, tens of thousands of times a second, would make the benchmark
measure Python. Instead a small pool of complete `ExportLogsServiceRequest`
objects is built once, and each send mutates only what must be unique —
`bench.seq` and the two timestamps. References to those exact submessages are
captured at build time, so mutation is a field assignment, not a search.

**Per-worker template ownership.** `stamp()` mutates in place, so worker `w`
owns `templates[w*variants : (w+1)*variants]` and no other thread touches them.
Sharing one pool across workers would let two threads stamp the same protobuf
concurrently and ship records tagged with each other's sequence numbers —
silently breaking the exact check the sequence numbers exist for. There is a
unit test for this.

If the generator still binds first, raise `batch_size` (fewer, larger RPCs carry
the same record rate) or run generators on more than one host.
`tests/stress/run.sh` detects the condition and says so rather than reporting it
as a pipeline ceiling.

---

## Output

```
results/<test-id>/
├── metadata.json    config echo, host facts, clock offset, git commit
├── generator.jsonl  per-second: offered, accepted, rejected, inflight,
│                    stalled_ms, latency p50/p95/p99/max
├── generator.csv    the same, flat
├── generator.json   final totals + per-step results
└── manifest.json    per-gen_id seq range, totals, UTC start/stop, retry mode
```

`manifest.json` is the reconciliation contract — `validate_correctness.py`
compares the landed rows against it and nothing else.

Prometheus on `GENERATOR_METRICS_PORT` (9102) while running:
`bench_generator_records_{offered,accepted}_total`, `bench_generator_requests_total{result}`,
`bench_generator_errors_total{code}`, `bench_generator_inflight`,
`bench_generator_stalled_ms_total`, `bench_generator_export_latency_seconds`.

---

## Rate accounting

```
offered    attempted            a property of this process — NOT throughput
accepted   acked by the server  persisted, because the RPC waits for durability
rejected   RESOURCE_EXHAUSTED   the collector explicitly refusing work
```

`RESOURCE_EXHAUSTED` is **never** silently retried. `retry_mode: polite` exists
only because a soak's definition of "sustainable" may require a well-behaved
client; the mode used is recorded in every manifest either way.

---

## Payload

Deterministic from `data.seed`: the same seed produces a byte-identical record
stream, which is what makes compression and file-size comparisons valid across
runs.

Bodies are padded with random hex to `body_bytes`, not a repeated character — a
run of `x` compresses to nothing and would flatter every ZSTD ratio.

`model_traffic` adds `gen_ai.*` attributes — model, input/output/total tokens,
and per-request cost derived from the configured price table.

`event_name` is **not** set. The field does not exist in the proto version
either side is built against, and the collector hardcodes the column to NULL
(`LogRecordConverter`: *"field added in proto > 1.3.2"*).

---

## Tests

```bash
python generator/tests/test_generator.py       # 19 tests, no network
python -m pytest generator/tests/ -q
```

Covers config precedence and validation, seq stamping and its visibility in the
serialized request, determinism, per-worker template isolation, percentile
arithmetic, and the three rates staying separate.

---

## Docker

Optional; `scripts/run-test.sh` runs it in the venv, one less layer between the
pacer and the network.

```bash
docker compose run --rm generator --profile smoke
```
