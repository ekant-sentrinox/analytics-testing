# Troubleshooting

Failures actually hit while building and running this, with the diagnosis and
the fix. Not a generic list.

Start with:

```bash
./scripts/status.sh          # everything on one screen
./scripts/health-check.sh    # stage by stage
./scripts/preflight.sh       # 40-odd checks, non-zero if mandatory ones fail
```

---

## Network

### Ports 4317 / 8080 / 8081 time out, but SSH works

**This is the big one on this environment.**

```
FAIL  10.16.24.204:4317   timed out after 6004ms (packets dropped: security group)
PASS  10.16.24.204:22     6ms
```

**Diagnose by timing**, not by the word "failed":

| symptom | meaning |
|---|---|
| connects in ms | fine |
| **refused**, immediately | route fine, nothing listening — a *service* problem |
| **timeout**, ~6 s | packets dropped — a *security group* problem |

Confirm the service is actually fine:

```bash
ssh ec2-user@10.16.24.204 'ss -tlnp | grep 4317'          # LISTEN on *:4317
ssh ec2-user@10.16.24.204 'curl -s localhost:8081/health' # HEALTHY
ssh ec2-user@10.16.24.204 'sudo systemctl is-active firewalld'   # inactive
```

If it listens on `*` and answers on loopback and there is no host firewall, it
is the security group. `sg-0ea9dd40012388877` allows port 22 only. Rules to add:
[NETWORK.md](NETWORK.md). The instance role has no `ec2:*` permissions, so it
cannot be fixed from the instances.

### `Permission denied (publickey)` from SERVER 1 to 2 or 3

SERVER 1 ships with an `authorized_keys` but no private key of its own.

```bash
./scripts/setup-ssh.sh            # shows exactly what is missing
./scripts/setup-ssh.sh --apply
```

Creates a dedicated keypair and appends it to `authorized_keys` on 2 and 3.
Append-only, idempotent.

---

## Deployment

### `/usr/bin/env: 'bash\r': No such file or directory`

CRLF line endings — a file was edited on Windows. Fix:

```bash
sed -i 's/\r$//' scripts/*.sh tests/*/run.sh
```

### `unresolved placeholders in ...tmpl: FOO`

A `@FOO@` in a template has no matching exported variable in the deploy script.
Either add the export, or — if it is prose in a comment — rephrase it: the
renderer scans the whole file, comments included, and refuses to emit a config
with an unresolved placeholder rather than shipping a literal `@FOO@`.

### `Invalid -Xlog option ... Error opening log file`

The GC log path points at a directory that does not exist. `jvm.opts` in
`config/{collector,compactor}.yaml` uses an absolute path under
`/opt/analytics-bench/<role>/logs/`; if you move `base_dir`, move this too.

### Compactor ignores its config

It has **no `-c` flag**. `CompactionConfig.rawConfig()` calls
`ConfigFactory.load("application")` — it reads `application.conf` off the
*classpath*. `bin/run.sh` puts `conf/` first so it shadows the copy inside the
module. Only `--conf key=value` works on the command line.

```bash
ssh ec2-user@10.16.25.10 'grep -o "^CP=.*" /opt/analytics-bench/compactor/bin/run.sh'
# conf/ must come first
```

### Service will not start: exit 78

`bin/run.sh` could not read the catalog password file.

```bash
ssh ec2-user@10.16.24.204 'ls -l ~/.config/sentrinox/analytics-perf-catalog.pw'
```

### `Could not resolve substitution: PG_PASSWORD`

The HOCON references `${PG_PASSWORD}` and the wrapper did not export it. Run
`bin/run.sh` by hand to see the failure; systemd invokes it as the unit's `User`,
so a file readable only by another account will fail here and not in your shell.

---

## Ingestion

### Every export returns `UNAUTHENTICATED`

The generator's JWT secret does not match the collector's.

```bash
grep OTEL_JWT_SECRET_B64 .env
ssh ec2-user@10.16.24.204 'sudo grep secret_key /opt/analytics-bench/collector/conf/application.conf'
```

Rotating the secret **requires redeploying the collector**:

```bash
./scripts/gen-secret.sh --force
./scripts/deploy-collector.sh --no-build
```

Also check the key length — `scripts/preflight.sh` reports it. Below 64 bytes
the generator signs HS256; jjwt sizes the algorithm to the key, so a collector
configured with a longer key rejects those tokens.

### Every export returns `INVALID_ARGUMENT`

The `x-dd-ingestion-queue` claim does not match a configured queue. **There is
no default queue** — this is by design, not a bug.

```bash
grep OTEL_INGESTION_QUEUE .env
ssh ec2-user@10.16.24.204 'sudo grep ingestion_queue /opt/analytics-bench/collector/conf/application.conf'
```

`./scripts/health-check.sh` checks this explicitly.

### `RESOURCE_EXHAUSTED`

Not a bug. Pending write bytes exceeded `max_pending_write` (500 MiB) and the
queue raised `PendingWriteExceededException`. The collector is correctly
refusing work: the offered rate is above what the writer can drain.

It **disqualifies the level as sustainable**. Do not "fix" it by retrying — that
hides the measurement. Either lower the rate, or if you believe there is
headroom, look at `min_bucket_size` and DuckDB threads first.

### `DEADLINE_EXCEEDED`

The export RPC did not finish inside `rpc_timeout_seconds` (60 s). Because the
RPC acks on durability, this is a *slow flush*, and **the records may still have
landed** — check the correctness section before calling it loss. Do not lower
the timeout; that converts working-but-slow into failure and undercounts
accepted records.

### Records are acked but nothing appears in the lake

Almost always **data inlining**.

```bash
./scripts/check-postgres.sh          # "live files (B2): 0 files"
ssh ec2-user@10.16.21.20 'export PGPASSWORD=...; psql ... -c "\dt ducklake_inlined*"'
```

A `ducklake_inlined_data_*` table means DuckLake kept the commits as rows inside
the PostgreSQL catalog instead of writing Parquet to S3. Row counts still add
up, the compactor has nothing to merge, and the benchmark measures the wrong
system while looking correct.

The `ATTACH` must include `DATA_INLINING_ROW_LIMIT 0`. Both rendered configs do;
if you hand-edited one, redeploy.

---

## Catalog

### `password authentication failed for user "postgres"`

The master user on this instance is **`analytics`**, not `postgres`.

### `psycopg` not found in a script

The script is running the system Python (3.9), not the venv. Every script picks
the interpreter through `python_bin()`, which prefers `.venv/bin/python`. If the
venv is missing:

```bash
./scripts/setup.sh
```

### DuckDB version mismatch warning in preflight

```
WARN  duckdb version   python duckdb 1.5.2 vs expected 1.5.4
```

The DuckLake catalog metadata schema is version-dependent, and the servers write
with the 1.5.4.0 JDBC driver. Reading the catalog with a different DuckDB can
produce a confidently wrong answer.

The **host `duckdb` CLI is 1.5.2** on these machines. That is why every SQL path
here goes through `scripts/lib/duckdb_exec.py`, which uses the venv's pinned
`duckdb==1.5.4`. If you run `duckdb` by hand against the catalog, you are using
the wrong version.

### Snapshot count keeps dropping

Working as intended. Housekeeping runs `expire_snapshots` every 5 minutes with
`snapshot_retention = 15 minutes`.

---

## Compaction

### `totalMinorCompactions` climbs but `totalFilesCompacted` stays 0

The compactor is running and finding nothing to merge. Either there genuinely
are no files (check B2), or every file is already above
`minor_compaction_max_size` (8 MiB) — in which case nothing is wrong, but
`min_bucket_size` (16 MiB) is producing files larger than the minor threshold,
so minor compaction can never do anything by construction. Either raise
`minor_compaction_max_size` above `min_bucket_size`, or lower `min_bucket_size`.

### Compactor log has no timestamps

Its dependency tree has slf4j-simple and no logback, so `logback.xml` is
silently ignored. `compactor/config/simplelogger.properties` is what it reads.
Symptom: `parse_compactor_log.py` reports

```
WARNING: 164 records had no timestamp
```

Fix: `./scripts/deploy-compactor.sh --no-build`.

### S3 object count is far above live file count

Expected for a while. Files superseded by a merge are reclaimed by
`ducklake_cleanup_old_files` on the housekeeping timer (5 min). If the gap keeps
growing across several cycles, housekeeping is not running — check the compactor
log for `Housekeeping completed`.

---

## Generator

### `gRPC channel never became ready`

The collector is unreachable. Ninety per cent of the time this is the security
group; see the top of this page.

### `stalled_ms` is large and offered never reaches the target

The pacer could not submit on schedule — every in-flight slot was busy.

```bash
tail -5 logs/host-generator.jsonl | python3 -c 'import json,sys;
[print(json.loads(l)["cpu_percent"]) for l in sys.stdin]'
```

- generator CPU near 100% → **the generator is the bottleneck, not the
  pipeline.** Raise `batch_size` so fewer, larger RPCs carry the same record
  rate, or run generators on more than one host. Do not report this as a
  pipeline ceiling; `tests/stress/run.sh` calls it out explicitly.
- generator CPU low, in-flight pinned at `max_inflight` → the pipeline is slow
  and the bound is masking how far behind. Raise `max_inflight`.

### `workload.workers (32) exceeds max_inflight (8)`

Deliberate rejection: with more workers than in-flight slots the bound is never
observable, so stalls could never be attributed. Raise `max_inflight`.

---

## Resources

### Collector OOM-killed

7.6 GiB, **no swap**, so memory pressure is a kill, not a slowdown.

```bash
ssh ec2-user@10.16.24.204 'sudo dmesg -T | grep -i "out of memory"'
ssh ec2-user@10.16.24.204 'systemctl show bench-collector -p NRestarts --value'
```

Heap is only part of it: Arrow buffers and DuckDB are **off-heap**. Total is
roughly `max_heap` + DuckDB `memory_limit` + Arrow + JVM overhead. Defaults are
2 GiB + 2 GiB, leaving room; if you raise either, check the sum.

Check `temp_directory` is not `/tmp`. On Amazon Linux 2023 `/tmp` is **tmpfs**,
so a DuckDB spill there consumes RAM and turns a disk spill into an OOM kill.
Both configs use `/var/tmp/duckdb-*`.

### Disk fills up

8 GiB root, ~5 GiB free.

```bash
df -h /
du -sh results/* logs/* | sort -h | tail
```

A 1 s-sampling soak produces real volume. `logs/*.jsonl` grow continuously while
agents run; `collect-results.sh` slices only the run's own window into
`results/`. Clear old runs with `./scripts/cleanup.sh --results --yes`.

### Everything slows down late in a long run, for no reason

**Check CPU credits.** All three are burstable t3.large. Credit exhaustion
throttles the vCPU and is indistinguishable from a software regression on a
graph. Look at `CPUCreditBalance` in CloudWatch before blaming code.

---

## Docker

### `permission denied ... /var/run/docker.sock`

`setup.sh` adds `ec2-user` to the `docker` group but the current shell predates
it:

```bash
newgrp docker      # or log out and back in
```

Docker is only needed for Prometheus and Grafana. Nothing about a measurement
depends on it.

---

## Reports

### A section says `NOT MEASURED`

Working as designed. The input file was missing, and the report names it rather
than estimating. Usual causes: agents not running (`./scripts/start.sh`), the
exporter down, or `collect-results.sh` not run.

```bash
./scripts/collect-results.sh <test-id>
./scripts/generate-report.sh <test-id>
```

### No charts

matplotlib missing, or no samples. `./scripts/setup.sh` installs it; the report
still generates without it.

### `SyntaxError: f-string expression part cannot include a backslash`

A helper ran under Python 3.9 (the system interpreter) instead of the venv's
3.11. Use `python_bin()` / `./.venv/bin/python`.

---

## Getting a clean slate

```bash
./scripts/stop.sh --all
./scripts/reset.sh                 # restart services, redeploy config, verify
./scripts/reset.sh --with-data --yes   # also wipe the lake (guard-railed)
```

`cleanup.sh --data` drops only this project's tables in its own database, and
deletes only under `s3://$S3_BUCKET/$S3_PREFIX/`. It refuses to run if the
prefix is empty, `/` or `.`, and requires typing the bucket name. It never
deletes the bucket or the database.
