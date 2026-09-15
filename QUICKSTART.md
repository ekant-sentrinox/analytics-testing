# Quickstart

Fresh SERVER 1 to a report, in order. Roughly 20 minutes, most of it Maven.

Everything runs from `~/analytics-distributed-test` on **SERVER 1**
(`10.16.21.20`). SERVER 1 is the control node: it drives the other two over SSH.

---

## 1. Setup — once

```bash
cd ~/analytics-distributed-test
./scripts/setup.sh
```

Creates `.venv`, installs pinned dependencies, creates `.env` from
`.env.example`, generates the 64-byte JWT secret, and initialises git. Safe to
re-run.

Then check `.env`. The defaults are correct for this environment; the one to
confirm is that the catalog password file exists:

```bash
ls -l "$(grep ^PG_PASSWORD_FILE .env | cut -d= -f2)"
```

## 2. SSH to servers 2 and 3

```bash
./scripts/setup-ssh.sh            # shows what is missing, changes nothing
./scripts/setup-ssh.sh --apply
```

Creates a dedicated keypair and appends it to `authorized_keys` on 2 and 3.
Append-only and idempotent.

## 3. Catalog

```bash
./scripts/bootstrap-catalog.sh
```

Creates `bench.main.logs` and `bench.main.ingest_watermark` in the DuckLake
catalog on RDS, with data on S3. `CREATE TABLE IF NOT EXISTS` throughout —
re-running changes nothing.

Expected:

```
PASS  catalog bootstrap   bench.main.{logs,ingest_watermark}
```

## 4. Deploy the pipeline

```bash
./scripts/deploy-collector.sh     # SERVER 2 — builds, ~2 min the first time
./scripts/deploy-compactor.sh     # SERVER 3
```

Each builds the Maven module, installs into `/opt/analytics-bench/<role>/`,
registers a systemd unit and starts it. Expected:

```
PASS  collector health (local)   {"status":"HEALTHY","grpcPort":4317,"knownQueues":1,...}
PASS  compactor health (local)   {"status":"UP","databases":{"bench":{...}}}
```

Use `--no-build` on later deploys to skip Maven when only config changed.

## 5. Preflight

```bash
./scripts/preflight.sh
```

Checks host, runtimes, config, connectivity, S3, catalog, both services and
monitoring. Non-zero exit if anything mandatory fails.

> **If it fails on connectivity to 4317 / 8081 / 8080, stop here.** The security
> group allows port 22 only, and no test can run until that is fixed. The exact
> rules are in [NETWORK.md](NETWORK.md). The services themselves are fine —
> `./scripts/health-check.sh` will show them HEALTHY on their own loopbacks.

## 6. Monitoring — optional

```bash
./scripts/start.sh --monitoring
```

Prometheus `:9090`, Grafana `:3000` (admin/admin), seven dashboards already
provisioned. Not required for a measurement: every run writes its own JSONL and
generates its own report.

## 7. Smoke test

```bash
./tests/smoke/run.sh
```

10 records/s for 60 s, then every acked record is read back out of the lake.
Expected:

```
PASS  generator      exited 0
PASS  delivery       600/600 records accepted, none refused
PASS  correctness    no loss, no duplication
PASS  report         results/20260910-.../report.md
```

If this passes, the whole path works: JWT auth, queue routing, Arrow, the
Parquet COPY, the S3 write, the catalog commit, the watermark, and the read
back.

## 8. Throughput

```bash
./tests/throughput/run.sh
```

The default staircase is 1k → 25k records/s, 10 minutes per level: about 70
minutes plus settle. For a first look:

```bash
./tests/throughput/run.sh --steps "1000,5000,10000" --step-duration 600
```

Ends with a per-level verdict:

```
  step  target rps   accepted/s   ratio    rejected   backlog slope   verdict
     0       1,000        1,000  1.0000           0          -0.10   SUSTAINED
     1       5,000        4,998  0.9996           0          +0.05   SUSTAINED
     2      10,000        8,210  0.8210      41,203          +7.30   FAILED
        └─ accepted/offered 0.8210 < 0.99
        └─ 41,203 records rejected
        └─ backlog +7.30 files/min

  MAXIMUM SUSTAINABLE RATE: 5,000 records/s
```

**Do not shorten the steps below ~10 minutes** without also shortening
`major_compaction_frequency` in `config/compactor.yaml`. A step that contains no
compaction cycle produces a backlog slope that is pure noise, and the
sustainability verdict becomes meaningless.

## 9. Read the report

```bash
less results/<test-id>/report.md
ls  results/<test-id>/charts/
```

Generated from files. Any section whose input is missing says `NOT MEASURED` and
names the file it wanted. How to read one without over-claiming:
[RESULTS.md](RESULTS.md).

---

## After that

```bash
./tests/soak/run.sh --hours 6 --rps <75% of sustainable>
./tests/drain/run.sh                        # how fast the backlog clears
./tests/recovery/run.sh collector-sigterm   # should lose nothing
./tests/recovery/run.sh collector-sigkill   # measures what a hard kill costs
./tests/functional/run.sh                   # auth and queue-routing contracts
./tests/stress/run.sh                       # push until something gives
```

## Day to day

```bash
./scripts/status.sh                  # everything on one screen
./scripts/health-check.sh            # stage by stage
./scripts/generate-report.sh --all   # rebuild every report
./scripts/stop.sh                    # agents + exporter; services keep running
```

`stop.sh` deliberately leaves the collector and compactor up: stopping the
collector mid-drain is a data-loss event, and after a soak you usually want the
compactor still draining — that is what the drain test measures.
