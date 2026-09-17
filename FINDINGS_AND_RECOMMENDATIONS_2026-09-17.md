# Findings & Recommendations — services and system — 2026-09-17

From the ai_txn ramp / peak-stable campaign. Ordered by how likely each is to
bite an operator. Severity: 🔴 critical · 🟠 warning · 🔵 context.

---

## F1 🔴 Major compactor tier had no credential-expiry mitigation

**Finding.** The known STS-credential-expiry bug (IMPROVEMENTS #16: DuckDB's
`credential_chain` S3 secret never refreshes; every S3 op fails ~6 h after start
while `/health` still reports UP) was mitigated on the **minor** tier only
(`bench-compactor` had the `10-credential-refresh.conf` drop-in: `RuntimeMaxSec=4h`
+ auto-restart). The **major** tier (`bench-compactor-major`) had
`RuntimeMaxSec=infinity` — no mitigation. Any run longer than ~6 h would have seen
the major tier silently stop doing S3 work while reporting healthy.

**Action taken (before the test, the one deliberate change).** Copied the same
drop-in to `/etc/systemd/system/bench-compactor-major.service.d/`,
`daemon-reload`, restarted the major tier. Both tiers now auto-restart every 4 h.
Restart count is a *measured* quantity — expect ~6 tier restarts per 24 h in soak
data; they are healthy, not instability.

**Recommendation.** This drop-in is a stopgap. The real fix is code-level:
re-issue the S3 secret on a timer inside the startup-script provider (or move to
a provider that honours REFRESH), so neither tier needs a scheduled restart.
Until then, **persist the drop-in** — a bare `systemd-run`/manual unit edit is
lost on redeploy; fold both drop-ins into `deploy-compactor.sh` so a fresh deploy
ships them.

## F2 🔴 Nothing compacts files ≥ 64 MB (the large band grows forever)

**Finding.** The two tiers cover [0,12 MB) and [12,64 MB). Major's own merged
output frequently exceeds 64 MB, leaves every band, and is never touched again.
The catalog "large" band grew monotonically during the test (e.g. 84 → 156 live
files across the ramp) and never drains. At current rates it grows slowly and
total live files still stay flat/drain, so it is not yet the binding constraint —
but it is an unbounded accumulation by design.

**Recommendation.** Either (a) add a third tier / raise major's upper bound so
its output re-enters a compactable band, or (b) replace the two-tier split with a
**single continuous loop bounded only by `target_file_size`** (e.g. 512 MB), which
the handoff already floats as the preferred direction. Track the large-band slope
as a first-class metric (now on the Grafana compactor dashboard) and alert if it
ever begins to dominate total-file growth.

## F3 🟠 `/health` reports UP even when every compaction cycle fails

**Finding.** Both tiers' `/health` reflect **process liveness, not compaction
success**. During the credential-expiry failure mode a tier reports UP while
doing zero S3 work. The only true signals are `totalFailedCycles` and time since
last successful cycle.

**Actions taken (monitoring, no change to the system under test).**
- Fixed the pipeline exporter (`monitoring/exporters/pipeline_exporter.py`) which
  had silently broken: it queried a **renamed watermark table** (`ingest_watermark`
  → `main.ai_txn_watermark`, column `max_time`) and could not parse the **new
  nested per-tier `/health` format** (`databases.<db>.tiers.{minor,major}`). Both
  now work; the exporter also scrapes the major tier's :8090 over the existing SSH
  channel.
- Added per-tier metrics and a rebuilt **Grafana compactor dashboard** showing
  compaction *by type*: cycles, files merged, **failed cycles**, **time since last
  successful cycle**, and the small/medium/large band populations.

**Recommendation.** Add a Prometheus/Grafana **alert** on: `failed cycles > 0`
(sustained), `time-since-last-success > 2× cadence` per tier, and
`tier_up == 0`. These catch the "UP but dead" state that `/health` alone hides.

## F4 🟠 ai_txn sustainable ceiling ~10 k eps is S3-latency-bound, unverified for throttling

**Finding.** Peak sustainable ≈ 10 k eps for ai_txn. At 15 k the shortfall is
**not** CPU on any host (generator ~35 % of one core, collector ~2/8 cores,
compactor ~25 %) — it is the collector's durable-commit rate, gated by S3
flush/commit latency for the heavier payload. Whether some of that latency is S3
**request throttling** is still unmeasured: `httpfs` retries 503s internally
(`http_retries=3`, backoff ×4), so throttled requests never reach a log, and the
bucket has no CloudWatch request metrics configured.

**Recommendation.** To settle throttling vs. raw latency, point
`http_logging_output` at a file for a cycle or two, or enable S3 request metrics
on the bucket. If throttling is present, spreading writes across more key
prefixes (S3 scales per-prefix) or requesting higher limits would lift the
ceiling without more compute. Independently, if a rate above ~10 k for ai_txn is
required, validate with a **second generator host** to rule out any residual
test-tool concurrency limit — though current evidence points downstream, not at
the generator.

## F5 🟠 Two unexplained JVM SIGSEGVs remain on record

**Finding.** Three `hs_err_pid*.log` crash logs predate this test (minor tier
2026-09-16 15:03 and 21:11, one inside `__pthread_rwlock_rdlock`; the
experimental major-only unit's shutdown SIGSEGV `hs_err_pid502076.log`). **None
recurred during this campaign** (0 new crash files 07:40–now). Cause never found.

**Recommendation.** Keep the crash logs; treat any *new* `hs_err` during the soak
as a fresh finding to file against Corretto/DuckDB JNI. The native frame suggests
a DuckDB/JNI locking interaction worth a targeted reproduction.

## F6 🔵 Generator warmup looks like a hang (operator trap)

**Finding.** At higher rates the generator spends ~3–4 min pre-building its
deterministic payload template pool (RSS ~5 GB, one core busy) **before** it emits
anything — no throughput, no metrics on :9102 yet. Easy to misread as a stall.

**Recommendation.** Log a one-line "pre-generating payload pool, N s" heartbeat at
startup so operators don't kill a healthy warmup. Purely cosmetic.

## F7 🔵 Long-running observers must be daemonized on the host

**Finding (harness).** `nohup … &` launched over an SSH session is killed when the
session closes; operator-side background watchers were also killed periodically by
the local environment. Only **systemd transient units on SERVER 1** survived.

**Recommendation.** Any unattended monitor/driver for multi-hour runs should be a
`systemd-run --unit=… --collect --uid=ec2-user` unit (as `ramp-sampler` and
`ramp-driver` are here), never a backgrounded SSH child. Consider committing a
small `scripts/observe.sh` that wraps this pattern.

---

## Summary of the only change made to the environment

| Change | Where | Reversible | Why |
|---|---|---|---|
| `10-credential-refresh.conf` drop-in (RuntimeMaxSec=4h) | `bench-compactor-major` on SERVER 3 | yes (`rm` drop-in + daemon-reload) | F1 — without it the test is invalid past ~6 h |

Everything else (collector, minor compactor, generator config, resource limits,
infrastructure) was left untouched for the duration. The exporter/Grafana edits
are on SERVER 1's monitoring stack only and do not touch the system under test.
