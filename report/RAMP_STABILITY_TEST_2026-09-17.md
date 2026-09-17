# Ramp / Peak-Stable Stability Test — ai_txn workload — 2026-09-17

**Status: IN PROGRESS (autonomous).** Ramp phase (5k → 15k) complete and
analysed below. Knee probes (12k, 13k) and the 24-hour peak-stable soak are
running unattended via the `ramp-driver` systemd unit on SERVER 1; their results
are appended to this file and to `~/ramp-driver.log` as they complete.

## Goal

Find the **highest sustainable events-per-second** the pipeline can *ingest and
compact continuously* — not a short-term maximum — then prove that rate for 24 h.
Sustainable is defined (per this repo's README) as **accepted rate held with a
flat file backlog and healthy compaction**, never accept rate alone.

## What was tested, and the rule for changing nothing

The environment was frozen for the duration: no code, config, compactor setting,
resource limit, or infrastructure change during the test. The **one** change was
made *before* the test started and is required for the test to be valid at all —
see Findings, item F1 (major-tier credential drop-in).

- **Workload:** `ai_txn` schema (`config/generator-ai-txn.yaml`), 100 customers,
  model-traffic attributes, HTTP/latency mixes. This is a **heavier per-record
  payload** than the generic `logs` schema used in earlier campaigns — relevant
  to reading the numbers against the 2026-09-15 25k result.
- **Generator (soak profile):** workers 80, channels 8, batch 2500,
  max_inflight 160, transport direct → `10.16.24.204:4317`.
- **Pipeline:** collector on SERVER 2 (t3.2xlarge, 8 vCPU / 31 GiB), two-tier
  DuckLake compactor on SERVER 3 (t3.2xlarge, 8 vCPU / 30 GiB; minor [0,12 MB)
  health :8080, major [12,64 MB) health :8090), PostgreSQL RDS catalog, S3 data.
- **Method:** each level run for 1 h back-to-back (`run-test.sh soak --rps N
  --duration 3600`). A per-60s sampler (`ramp-sampler`, `~/ramp-samples.tsv`)
  recorded every required metric. Levels advance only if the completed level
  passed all stability gates.

## Stability gates (all four must pass)

| Gate | Threshold | Why |
|---|---|---|
| Accepted ratio | mean(accepted) ≥ 0.93 × target | can the pipeline even reach the rate |
| Watermark lag | 2nd-half max < 90 s | is end-to-end visibility keeping up |
| Failed compaction cycles | ≤ 2 (restart artefacts only) | compaction healthy |
| Backlog slope | files_total 2nd-half slope < ~250/h | backlog not running away |

## Results by level

| Load (eps) | Duration | Accepted (mean / ratio) | Files pending (small band) | Files total (start→end) | Watermark lag (mean / max) | Minor / major failed | Compactor CPU | Compactor mem | Spill | Errors/OOMs | Stable? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **5,000** | 1 h | 4,954 / **0.99** | 630 → 483 | 1,236 → 905 (**draining**) | 8.8 s / 20.6 s | 0 / 0 | 15–54 % | 10–21 GB | 2–5 % | none | **YES** |
| **10,000** | 1 h | 9,815 / **0.98** | 605 → 446 | 944 → 954 (**flat**) | 14.4 s / 60.8 s* | 0 / 0 | 10–26 % | 10–20 GB | 2 % | none | **YES** |
| **15,000** | ~1 h | 9,133 / **0.61** | 867 → 684 | 1,067 → 1,130 (flat) | 20.3 s / 27.0 s | 0 / 0 | 16–27 % | 11–21 GB | 2 % | none | **NO — capped** |

\* the 60.8 s max at 10k is a single transition-window spike (level start); the
steady 2nd-half stayed ~13–15 s.

### Reading the table

- **5,000 eps — fully sustainable.** Accepted 99 % of target; the backlog
  actively *drained* (total live files 1,236 → 905). Everything idle-comfortable.
- **10,000 eps — fully sustainable.** Accepted 98 %; total files **flat**
  (944 → 954); watermark lag **flat at ~13–15 s** the whole hour — the decisive
  "keeping up" signal. 18 M/18 M then 36 M/36 M records accepted across L1/L2,
  0 rejected, 100 % of requests OK.
- **15,000 eps — the pipeline cannot ingest it.** Accepted only **61 %** of
  target (~9–10 k of 15 k). The generator stalled ~37 % of the time and *offered*
  only ~9–10 k. Importantly the system stayed **healthy while capped** — lag
  bounded ~20 s, 0 failed cycles — it simply throttles to its ~10 k ceiling.

## Where compaction first fell behind, and the first instability metric

Compaction **never fell behind** across 5k–15k: minor and major both reported
**0 failed cycles** throughout, and the total live-file backlog stayed flat or
drained at every level. The medium band [12,64 MB) drifted up mildly at 10k
(232 → 352) — a watch item, not a failure.

**The first metric to indicate the ceiling was the accepted ratio**, not a
compaction metric: it fell from 0.98 (10k) to 0.61 (15k) while the generator
went from 0 stall to ~37 % stall. Compaction was never the limiter in this range.

## Why the ceiling is ~10 k, and what it is (and is not)

At the 15k target, **no host is CPU-saturated**:

| Host | CPU at 15k target |
|---|---|
| Generator (SERVER 1) | ~35 % of **one** core (idle-waiting, not GIL-bound) |
| Collector (SERVER 2) | ~2 of 8 cores, ~36 % host |
| Compactor (SERVER 3) | ~20–27 % |

The generator is not CPU-bound (contrast the 2026-09-15 generic-payload run,
where the generator's Python GIL was the limit at 25k). Here it *offers* only
~10 k because its `max_inflight` semaphore only releases as the collector
acknowledges durable writes — and **the collector's durable-commit rate for the
ai_txn payload tops out around ~10–11 k eps, limited by S3 flush/commit latency,
not CPU.** This matches the compactor handoff note that "the bottleneck is S3
latency, not this box." The heavier ai_txn record (larger Parquet, richer
transform) flushes to S3 at a lower record/s rate than the lighter generic
payload did — which is why ~10 k here is not a regression from the 25 k generic
figure; they are different workloads.

**Peak stable rate (ai_txn) ≈ 10,000 eps** — pending the 12k/13k knee probes to
confirm whether there is headroom between 10k (proven stable) and 15k (capped).

## Remaining phases (running autonomously)

1. **Knee probes:** 12,000 then 13,000 eps × 1 h each, gated. Refines the peak
   between the last-stable (10k) and first-capped (15k) levels.
2. **24-hour soak** at the confirmed peak, with a health snapshot every 30 min
   (accepted, lag, files_total, large band, failed cycles) in `~/ramp-driver.log`.

Success criterion for the soak: stable file counts, sustainable compaction
times, no accumulating backlog, no OOMs/crashes over 24 h. Note the two compactor
tiers **auto-restart every 4 h by design** (credential workaround, F1) — those
restarts are expected and are *not* instability.

## Artefacts on SERVER 1

- `~/ramp-samples.tsv` — every 60 s sample, all levels (source data for the table).
- `~/ramp-driver.log` — level launches, gate evaluations, decisions, soak health.
- `~/ramp-level.txt` — current target rate.
- `results/ramp-L1-5000/`, `ramp-L2-10000/`, `ramp-L3-15000/`, then knee/soak
  run dirs — per-run generator manifests and logs.
- `~/ramp_sampler.py`, `~/ramp_driver.py` — the harness (read-only against the
  system under test).
