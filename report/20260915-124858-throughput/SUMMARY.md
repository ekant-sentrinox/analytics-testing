# Test Report — 20260915-124858-throughput

## Bottom line
**Every step passed cleanly** — zero rejected, zero lost. Highest rate tested: **25,000 rec/s**.

- Offered: 46,800,000 records
- Accepted: 46,800,000 records
- Rejected: 0 records
- Mean latency p50/p95/p99 (ms): 2,703.8 / 4,984.2 / 5,754.4

## Hardware

| Server | Instance type | CPU | RAM |
|---|---|---|---|
| Server 1 (generator) | t3.large | 2 vCPU | 7.6 GB |
| Server 2 (collector) | t3.2xlarge | 8 vCPU | 31.0 GB |
| Server 3 (compactor) | t3.xlarge | 4 vCPU | 15.4 GB |

## Real CPU / RAM usage during this run

| Server | CPU (min / avg / max) | Memory used % (min / avg / max) |
|---|---|---|
| Server 1 (generator) | 0% / **4.6%** / 60% | 10.3% / 28.6% / **31.1%** |
| Server 2 (collector) | 4% / **14.5%** / 57% | 4.6% / 5.5% / **7.1%** |
| Server 3 (compactor) | 7% / **20.0%** / 100% | 15.8% / 34.7% / **56.9%** |

![cpu.png](images/cpu.png)

![memory.png](images/memory.png)

![rss.png](images/rss.png)

![throughput.png](images/throughput.png)

![latency.png](images/latency.png)

![backlog.png](images/backlog.png)

![staircase.png](images/staircase.png)

## Per-step results

| Target rec/s | Offered | Accepted | Rejected | Ratio |
|---|---|---|---|---|
| 1,000 | 600,000 | 600,000 | 0 | 1.0000 |
| 2,000 | 1,200,000 | 1,200,000 | 0 | 1.0000 |
| 5,000 | 3,000,000 | 3,000,000 | 0 | 1.0000 |
| 10,000 | 6,000,000 | 6,000,000 | 0 | 1.0000 |
| 15,000 | 9,000,000 | 9,000,000 | 0 | 1.0000 |
| 20,000 | 12,000,000 | 12,000,000 | 0 | 1.0000 |
| 25,000 | 15,000,000 | 15,000,000 | 0 | 1.0000 |

## How big a generator VM do we need for 30k → 200k rec/s?

The generator is Python `threading`-based, so it's bound by the GIL: a single
process can't use more than about one core's worth of Python execution no
matter how many vCPUs the box has. A bigger single VM does **not**
proportionally increase throughput. The proven, low-risk path is horizontal:
run multiple generator hosts of **today's exact spec** (t3.large, 2
vCPU, 7.6 GB) in parallel, each generating up to the rate proven
clean in this run (**25,000 rec/s**), and aggregate their output.

| Target aggregate rate | Generator hosts needed (25,000 rec/s each) |
|---|---|
| 30,000 | 2 |
| 50,000 | 2 |
| 75,000 | 3 |
| 100,000 | 4 |
| 150,000 | 6 |
| 200,000 | 8 |

**Note:** the current generator instance (`t3.large`) is *burstable* (T-series) -- fine at low average CPU (this run averaged well under half a core), but sustained high-rate generation over a long test can exhaust CPU credits and throttle mid-run. For any per-host rate pushed meaningfully above what's proven here, use a non-burstable equivalent (e.g. `m5.large`) instead.

This sizing covers the **generator only**. It says nothing about whether the
collector or compactor can sustain that aggregate rate -- that is the actual
open question a multi-host campaign would answer, since both have shown
large unused headroom at every rate tested so far.


---
*Generated automatically by `bench/publish_report.py` from `results/20260915-124858-throughput/` — not hand-edited. Full harness-native report: `results/20260915-124858-throughput/report.md`.*
