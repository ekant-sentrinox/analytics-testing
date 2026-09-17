# Soak campaign — `ai_txn` workload, 2026-09-16 → 17

**Status: IN PROGRESS.** Soak #2 is running and due to finish ~19:10 UTC
2026-09-17. Numbers below for soak #2 are a snapshot taken 03:47 UTC; the final
verdict comes from `scripts/validate-correctness.sh 20260916-190938-soak` after
the run settles, and this file should be updated (or replaced by the generated
report) then. Soak #1 is complete and final.

## What is being tested

24 hours at 5,000 rec/s of the new **`ai_txn`** payload
(`config/generator-ai-txn.yaml`, schema code `generator/src/payload_ai_txn.py`):
100 customers as distinct OTLP resources, LLM model traffic (token counts,
pricing for claude-opus-5 / claude-sonnet-5 / claude-haiku-4-5), HTTP status and
latency mixes, queue `ai_txn`, seed 42. Transport direct to `10.16.24.204:4317`,
64 MiB buckets, batch 2,500 × 80 workers over 8 channels. This is the first
long-duration run of the schema that `v_ai_txn_transform` consumes, and the
first 24 h soak on the upsized hardware (S1 t3.xlarge / S2 t3.2xlarge /
S3 t3.xlarge).

## Soak #1 — `20260916-134306-soak` — FAILED (availability), kept per protocol

| | |
|---|---|
| Window | 13:47:20Z → 19:09:28Z (interrupted after 5.4 h) |
| Offered | 96,640,000 |
| Accepted | 70,472,500 (73%) |
| Rejected | 0 |
| Failed requests | **10,467 of 38,656 — all `UNAVAILABLE`** |
| Latency (p50/p95/p99 ms) | 6,699 / 9,788 / 10,358 |
| Generator stall | ~0 ms |

The 26.2 M-record gap is **not backpressure** (zero `RESOURCE_EXHAUSTED`) and
**not acked loss** — it is the collector being unreachable for stretches of the
run; `bench-collector` was restarted at 19:03Z. Source files:
`results/20260916-134306-soak/{manifest.json,generator.json}`. The run was
stopped and relaunched rather than re-run silently.

## Soak #2 — `20260916-190938-soak` — RUNNING, clean so far

| | snapshot 03:47Z (8.6 h in) |
|---|---|
| Window | started 19:10:08Z, ends ~19:10Z 2026-09-17 |
| Offered | ~154.0 M |
| Accepted | ~153.98 M (gap = in-flight) |
| Rejected / failed | **0 / 0** |
| Generator stall | 0 |

Pipeline side during the run (checked 03:40Z): collector `/health` HEALTHY,
60,781 batches processed; compactor cycling minor compactions continuously
(46 merges / 253 files in the 48 min after its scheduled 02:52Z restart);
compactor auto-restarts every 4 h via the `10-credential-refresh.conf` drop-in,
so no ExpiredToken window should occur inside the soak.

## After the run finishes

```bash
./scripts/collect-results.sh 20260916-190938-soak
./scripts/validate-correctness.sh 20260916-190938-soak
./scripts/generate-report.sh 20260916-190938-soak
```

Only the correctness verdict from that reconciliation — not the generator
totals above — establishes zero loss / zero duplicates for the 24 h window.
