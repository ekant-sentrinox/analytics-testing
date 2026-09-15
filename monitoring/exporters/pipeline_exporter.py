#!/usr/bin/env python3
"""Pipeline exporter — the missing telemetry for the collector/compactor/lake.

Runs on SERVER 1 and polls everything the pipeline does not export itself:

    collector /health  (SERVER 2)  liveness + batchesProcessed
    compactor /health  (SERVER 3)  compaction counters + file-count gauges
    PostgreSQL catalog (RDS)       B2 backlog, snapshot growth, connections
    DuckLake watermark             B3 end-to-end visibility lag
    S3 prefix                      object count and bytes

Why this exists at all: the collector is built with a SimpleMeterRegistry and
the compactor with a LoggingMeterRegistry, so neither publishes Micrometer
metrics anywhere a scraper can reach. Their /health endpoints are the only
machine-readable surface, and neither exposes backlog. Without this process
there is no time series for the numbers the benchmark is actually about.

Three distinct backlogs are reported and never averaged together:

    B1  in-memory queue depth at the collector  (pending batches/buckets)
        -- NOT AVAILABLE: those gauges live only in the in-process registry.
           /health exposes batchesProcessed and nothing else. Reported as null.
    B2  uncompacted files registered in the catalog  -- this is what
        compaction drains, and it is the one that matters most
    B3  wall-clock lag between now and the newest committed watermark

Output: Prometheus on --port, and one JSON object per poll to --jsonl.
Every scrape records success/failure per source; a source that is down produces
a recorded failure, never a silently missing sample.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("pipeline-exporter")

try:
    from prometheus_client import Counter, Gauge, start_http_server
except ImportError:
    sys.exit("prometheus_client required — run scripts/setup.sh")

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    import boto3
except ImportError:
    boto3 = None

try:
    import duckdb
except ImportError:
    duckdb = None


# ---------------------------------------------------------------------------
# Catalog SQL. Column names verified against the live catalog
# (PostgreSQL 18.4, ducklake metadata as written by DuckDB 1.5.x):
#   ducklake_data_file(data_file_id, table_id, begin_snapshot, end_snapshot,
#                      path, record_count, file_size_bytes, ...)
# A row with end_snapshot IS NULL is live; anything else is superseded.
# This is the same predicate CompactionService.updateFileCounts uses.
# ---------------------------------------------------------------------------
SQL_LIVE_FILES = """
SELECT count(*)                                AS files,
       COALESCE(sum(file_size_bytes), 0)       AS bytes,
       COALESCE(sum(record_count), 0)          AS rows
FROM ducklake_data_file
WHERE end_snapshot IS NULL
"""

SQL_SMALL_FILES = """
SELECT count(*)                                AS files,
       COALESCE(sum(file_size_bytes), 0)       AS bytes,
       COALESCE(sum(record_count), 0)          AS rows
FROM ducklake_data_file
WHERE end_snapshot IS NULL
  AND file_size_bytes < %(threshold)s
"""

SQL_SNAPSHOTS = "SELECT count(*), COALESCE(max(snapshot_id), 0) FROM ducklake_snapshot"

SQL_PG_STATS = """
SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()),
       (SELECT count(*) FROM pg_stat_activity
          WHERE datname = current_database() AND state = 'active'),
       (SELECT COALESCE(EXTRACT(EPOCH FROM max(now() - query_start)), 0)
          FROM pg_stat_activity WHERE datname = current_database() AND state = 'active'),
       pg_database_size(current_database()),
       (SELECT xact_commit  FROM pg_stat_database WHERE datname = current_database()),
       (SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database()),
       (SELECT deadlocks    FROM pg_stat_database WHERE datname = current_database())
"""


class Prom:
    def __init__(self) -> None:
        g = Gauge
        self.scrape_ok = g("bench_scrape_ok", "1 if the last scrape of a source succeeded", ["source"])
        self.scrape_seconds = g("bench_scrape_duration_seconds", "Scrape duration", ["source"])
        self.scrape_errors = Counter("bench_scrape_errors_total", "Failed scrapes", ["source"])

        self.collector_up = g("bench_collector_up", "Collector /health reachable and HEALTHY")
        self.collector_uptime = g("bench_collector_uptime_seconds", "Collector uptime")
        self.collector_batches = g("bench_collector_batches_processed",
                                   "Batches processed (cumulative, from /health)")
        self.collector_queues = g("bench_collector_known_queues", "Configured ingestion queues")

        self.compactor_up = g("bench_compactor_up", "Compactor /health reachable and UP")
        self.compactor_uptime = g("bench_compactor_uptime_seconds", "Compactor uptime")
        self.minor = g("bench_compaction_minor_total", "Minor compactions run", ["database"])
        self.major = g("bench_compaction_major_total", "Major compactions run", ["database"])
        self.files_compacted = g("bench_compaction_files_compacted_total",
                                 "Files merged", ["database"])
        self.files_small = g("bench_compactor_files_small", "Small files seen by the compactor",
                             ["database"])
        self.files_medium = g("bench_compactor_files_medium", "Medium files", ["database"])
        self.files_total = g("bench_compactor_files_total", "Total live files", ["database"])

        # B2 — the backlog compaction drains.
        self.backlog_files = g("bench_backlog_files", "Live data files in the catalog")
        self.backlog_bytes = g("bench_backlog_bytes", "Live data file bytes")
        self.backlog_rows = g("bench_backlog_rows", "Live data file rows")
        self.small_files = g("bench_backlog_small_files",
                             "Live files below the minor-compaction threshold")
        self.small_bytes = g("bench_backlog_small_bytes", "Bytes in those small files")
        self.small_rows = g("bench_backlog_small_rows", "Rows in those small files")

        # B3 — visibility lag.
        self.lag_seconds = g("bench_watermark_lag_seconds",
                             "now() - max(max_timestamp) from the ingest watermark table")
        self.watermark_rows = g("bench_watermark_committed_rows",
                                "Sum of row_count across watermark rows")

        self.snapshots = g("bench_catalog_snapshots", "DuckLake snapshots")
        self.snapshot_id = g("bench_catalog_max_snapshot_id", "Newest snapshot id")

        self.pg_connections = g("bench_pg_connections", "Connections to the catalog database")
        self.pg_active = g("bench_pg_active_queries", "Active queries")
        self.pg_longest = g("bench_pg_longest_query_seconds", "Longest running active query")
        self.pg_size = g("bench_pg_database_size_bytes", "Catalog database size")
        self.pg_commit = g("bench_pg_xact_commit_total", "Committed transactions")
        self.pg_rollback = g("bench_pg_xact_rollback_total", "Rolled back transactions")
        self.pg_deadlocks = g("bench_pg_deadlocks_total", "Deadlocks")

        self.s3_objects = g("bench_s3_objects", "Objects under the test prefix")
        self.s3_bytes = g("bench_s3_bytes", "Bytes under the test prefix")

        # --- host-local agent (SSH-pull health mode) -------------------------
        self.agent_age = g("bench_agent_snapshot_age_seconds",
                           "Age of the host agent's latest snapshot. A frozen agent "
                           "leaves a snapshot that reads healthy forever; this is what "
                           "distinguishes fresh from stale.", ["role"])
        self.health_latency = g("bench_health_check_latency_ms",
                                "Loopback /health response time, measured on the host", ["role"])
        self.unit_restarts = g("bench_service_restarts_total",
                               "systemd NRestarts for the service unit", ["role"])
        self.unit_active = g("bench_service_unit_active",
                             "1 if the systemd unit is active", ["role"])
        self.oom_kills = g("bench_oom_kills_total", "OOM kills seen in the kernel log", ["role"])
        self.process_rss = g("bench_process_rss_bytes", "Watched process RSS", ["role"])
        self.process_cpu = g("bench_process_cpu_percent", "Watched process CPU", ["role"])
        self.dependency_up = g("bench_dependency_up",
                               "1 if the dependency is reachable FROM that host",
                               ["role", "dependency"])
        self.dependency_latency = g("bench_dependency_latency_ms",
                                    "Dependency check latency from that host",
                                    ["role", "dependency"])
        self.host = g("bench_host_metric",
                      "Host system metric reported by the local agent", ["role", "metric"])


def http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def parse_iso_duration(text: str) -> float:
    """PT18.69S / PT1M3S -> seconds. The compactor reports java.time.Duration."""
    if not text or not text.startswith("PT"):
        return 0.0
    total, num = 0.0, ""
    for ch in text[2:]:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in "HMS" and num:
            total += float(num) * {"H": 3600, "M": 60, "S": 1}[ch]
            num = ""
    return total


class PipelineExporter:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.prom = Prom()
        self._stop = False
        self._duck = None
        self._last_s3 = 0.0
        self._last_lag = 0.0
        self._s3_cache: dict = {"objects": None, "bytes": None}
        self._lag_cache: dict = {"lag_seconds": None, "committed_rows": None}

        self._jsonl = open(args.jsonl, "a", buffering=1) if args.jsonl else None
        self._pg_dsn = (
            f"host={args.pg_host} port={args.pg_port} dbname={args.pg_database} "
            f"user={args.pg_user} password={args.pg_password} connect_timeout=5"
        )
        self._s3 = boto3.client("s3", region_name=args.aws_region) if boto3 else None

    # -- sources --------------------------------------------------------------

    def _agent_state(self, ssh_host: str) -> dict:
        """Read a host's local agent snapshot over SSH.

        This is the default and it is a deliberate architectural choice. The
        alternative — scraping http://<host>:8081/health across the network —
        would require an inbound security-group rule on every service port just
        to answer "are you alive". Port 22 is already open for deploy and
        result collection, so aggregating over it adds no attack surface.

        ControlMaster keeps one multiplexed connection per host, so a scrape is
        a channel open on an existing TCP session, not a fresh SSH handshake
        every 10 seconds.
        """
        ctl = f"/tmp/bench-ssh-{ssh_host}.sock"
        cmd = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}",
               "-o", "ControlPersist=300"]
        if self.args.ssh_key:
            cmd += ["-i", os.path.expanduser(self.args.ssh_key)]
        cmd += [f"{self.args.ssh_user}@{ssh_host}", f"cat {self.args.agent_state_path}"]
        out = subprocess.run(cmd, capture_output=True, timeout=self.args.http_timeout + 5)
        if out.returncode != 0:
            raise RuntimeError(
                (out.stderr.decode(errors="replace").strip() or "ssh failed")[:160])
        text = out.stdout.decode(errors="replace").strip()
        if not text:
            raise RuntimeError(f"{self.args.agent_state_path} is empty — is bench-agent running?")
        return json.loads(text)

    def scrape_collector(self) -> dict:
        if self.args.health_mode == "ssh":
            return self._scrape_agent("collector", self.args.collector_ssh_host,
                                      self.prom.collector_up, self.prom.collector_uptime)
        url = f"http://{self.args.collector_host}:{self.args.collector_health_port}/health"
        data = http_json(url, self.args.http_timeout)
        healthy = data.get("status") == "HEALTHY"
        self.prom.collector_up.set(1 if healthy else 0)
        self.prom.collector_uptime.set(float(data.get("uptimeSeconds") or 0))
        self.prom.collector_batches.set(float(data.get("batchesProcessed") or 0))
        self.prom.collector_queues.set(float(data.get("knownQueues") or 0))
        return {
            "status": data.get("status"),
            "uptime_seconds": data.get("uptimeSeconds"),
            "batches_processed": data.get("batchesProcessed"),
            "known_queues": data.get("knownQueues"),
            # B1 is genuinely unavailable from this surface. Say so explicitly
            # rather than emitting 0, which would read as "queue is empty".
            "pending_batches": None,
            "pending_buckets": None,
        }

    def _scrape_agent(self, role: str, ssh_host: str, up_gauge, uptime_gauge) -> dict:
        """Turn one host's local agent snapshot into metrics."""
        state = self._agent_state(ssh_host)
        health = (state.get("service") or {}).get("health") or {}
        unit = (state.get("service") or {}).get("unit") or {}
        svc = state.get("service") or {}
        sysm = state.get("system") or {}
        deps = state.get("dependencies") or {}
        body = health.get("body") if isinstance(health.get("body"), dict) else {}

        up = 1 if health.get("ok") else 0
        up_gauge.set(up)

        # Staleness: an agent that died leaves a snapshot that stays "healthy"
        # forever. Age is the only thing that distinguishes fresh from frozen.
        age = max(0.0, time.time() - float(state.get("ts") or 0))
        self.prom.agent_age.labels(role=role).set(age)
        if age > self.args.max_state_age:
            raise RuntimeError(
                f"{role} agent snapshot is {age:.0f}s old (limit {self.args.max_state_age:.0f}s) "
                "— the agent is not running or the clock is skewed")

        if role == "collector":
            uptime_gauge.set(float(body.get("uptimeSeconds") or 0))
            self.prom.collector_batches.set(float(body.get("batchesProcessed") or 0))
            self.prom.collector_queues.set(float(body.get("knownQueues") or 0))
        else:
            uptime_gauge.set(parse_iso_duration(body.get("uptime", "")))
            for db, s in (body.get("databases") or {}).items():
                self.prom.minor.labels(database=db).set(s.get("totalMinorCompactions", 0))
                self.prom.major.labels(database=db).set(s.get("totalMajorCompactions", 0))
                self.prom.files_compacted.labels(database=db).set(s.get("totalFilesCompacted", 0))
                self.prom.files_small.labels(database=db).set(s.get("currentSmallFiles", 0))
                self.prom.files_medium.labels(database=db).set(s.get("currentMediumFiles", 0))
                self.prom.files_total.labels(database=db).set(s.get("currentTotalFiles", 0))

        self.prom.health_latency.labels(role=role).set(float(health.get("latency_ms") or 0))
        self.prom.unit_restarts.labels(role=role).set(float(unit.get("restarts") or 0))
        self.prom.unit_active.labels(role=role).set(1 if unit.get("ok") else 0)
        self.prom.oom_kills.labels(role=role).set(float(svc.get("oom_kills") or 0))
        if svc.get("rss_bytes") is not None:
            self.prom.process_rss.labels(role=role).set(float(svc["rss_bytes"]))
        if svc.get("cpu_percent") is not None:
            self.prom.process_cpu.labels(role=role).set(float(svc["cpu_percent"]))

        # Dependency reachability measured FROM that host — the only place the
        # answer means anything.
        for dep in ("s3", "rds"):
            d = deps.get(dep) or {}
            if d.get("configured"):
                self.prom.dependency_up.labels(role=role, dependency=dep).set(
                    1 if d.get("ok") else 0)
                if d.get("latency_ms") is not None:
                    self.prom.dependency_latency.labels(role=role, dependency=dep).set(
                        float(d["latency_ms"]))

        for name, key in (("cpu_percent", "cpu_percent"),
                          ("mem_used_bytes", "mem_used_bytes"),
                          ("mem_available_bytes", "mem_available_bytes"),
                          ("root_free_bytes", "root_free_bytes"),
                          ("disk_write_bytes_per_s", "disk_write_bytes_per_s"),
                          ("disk_read_bytes_per_s", "disk_read_bytes_per_s"),
                          ("net_rx_bytes_per_s", "net_rx_bytes_per_s"),
                          ("net_tx_bytes_per_s", "net_tx_bytes_per_s")):
            if sysm.get(key) is not None:
                self.prom.host.labels(role=role, metric=name).set(float(sysm[key]))

        state["_snapshot_age_seconds"] = round(age, 2)
        return state

    def scrape_compactor(self) -> dict:
        if self.args.health_mode == "ssh":
            return self._scrape_agent("compactor", self.args.compactor_ssh_host,
                                      self.prom.compactor_up, self.prom.compactor_uptime)
        url = f"http://{self.args.compactor_host}:{self.args.compactor_health_port}/health"
        data = http_json(url, self.args.http_timeout)
        self.prom.compactor_up.set(1 if data.get("status") == "UP" else 0)
        self.prom.compactor_uptime.set(parse_iso_duration(data.get("uptime", "")))
        for db, s in (data.get("databases") or {}).items():
            self.prom.minor.labels(database=db).set(s.get("totalMinorCompactions", 0))
            self.prom.major.labels(database=db).set(s.get("totalMajorCompactions", 0))
            self.prom.files_compacted.labels(database=db).set(s.get("totalFilesCompacted", 0))
            self.prom.files_small.labels(database=db).set(s.get("currentSmallFiles", 0))
            self.prom.files_medium.labels(database=db).set(s.get("currentMediumFiles", 0))
            self.prom.files_total.labels(database=db).set(s.get("currentTotalFiles", 0))
        return data

    def scrape_catalog(self) -> dict:
        if psycopg is None:
            raise RuntimeError("psycopg not installed")
        out: dict[str, Any] = {}
        with psycopg.connect(self._pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(SQL_LIVE_FILES)
            files, nbytes, rows = cur.fetchone()
            out.update(backlog_files=files, backlog_bytes=int(nbytes), backlog_rows=int(rows))
            self.prom.backlog_files.set(files)
            self.prom.backlog_bytes.set(int(nbytes))
            self.prom.backlog_rows.set(int(rows))

            cur.execute(SQL_SMALL_FILES, {"threshold": self.args.small_file_threshold})
            sf, sb, sr = cur.fetchone()
            out.update(small_files=sf, small_bytes=int(sb), small_rows=int(sr))
            self.prom.small_files.set(sf)
            self.prom.small_bytes.set(int(sb))
            self.prom.small_rows.set(int(sr))

            cur.execute(SQL_SNAPSHOTS)
            snaps, max_snap = cur.fetchone()
            out.update(snapshots=snaps, max_snapshot_id=int(max_snap))
            self.prom.snapshots.set(snaps)
            self.prom.snapshot_id.set(int(max_snap))

            cur.execute(SQL_PG_STATS)
            conns, active, longest, size, commit, rollback, deadlocks = cur.fetchone()
            out.update(pg_connections=conns, pg_active_queries=active,
                       pg_longest_query_seconds=float(longest), pg_database_size_bytes=int(size),
                       pg_xact_commit=int(commit), pg_xact_rollback=int(rollback),
                       pg_deadlocks=int(deadlocks))
            self.prom.pg_connections.set(conns)
            self.prom.pg_active.set(active)
            self.prom.pg_longest.set(float(longest))
            self.prom.pg_size.set(int(size))
            self.prom.pg_commit.set(int(commit))
            self.prom.pg_rollback.set(int(rollback))
            self.prom.pg_deadlocks.set(int(deadlocks))
        return out

    def _duck_conn(self):
        """Attach the lake lazily; the watermark lives in Parquet, not Postgres."""
        if self._duck is not None:
            return self._duck
        if duckdb is None:
            raise RuntimeError("duckdb not installed")
        con = duckdb.connect(":memory:")
        for ext in ("httpfs", "aws", "ducklake", "postgres"):
            con.execute(f"INSTALL {ext}")
            con.execute(f"LOAD {ext}")
        con.execute("CREATE OR REPLACE SECRET s3_role "
                    f"(TYPE S3, PROVIDER credential_chain, REGION '{self.args.aws_region}')")
        con.execute(
            "ATTACH 'ducklake:postgres:host={h} port={p} dbname={d} user={u} password={w}' "
            "AS {cat} (DATA_PATH 's3://{b}/{pre}/', DATA_INLINING_ROW_LIMIT 0)".format(
                h=self.args.pg_host, p=self.args.pg_port, d=self.args.pg_database,
                u=self.args.pg_user, w=self.args.pg_password,
                cat=self.args.catalog, b=self.args.s3_bucket, pre=self.args.s3_prefix))
        self._duck = con
        return con

    def scrape_watermark(self) -> dict:
        con = self._duck_conn()
        row = con.execute(
            "SELECT COALESCE(EXTRACT(EPOCH FROM (now()::TIMESTAMP - max(max_timestamp))), -1), "
            "       COALESCE(sum(row_count), 0) "
            f"FROM {self.args.catalog}.{self.args.schema}.{self.args.watermark_table}").fetchone()
        lag = float(row[0])
        committed = int(row[1])
        # -1 means the table is empty: no batch has committed yet. That is not a
        # lag of zero and must not be plotted as one.
        self.prom.lag_seconds.set(lag)
        self.prom.watermark_rows.set(committed)
        return {"lag_seconds": None if lag < 0 else lag, "committed_rows": committed}

    def scrape_s3(self) -> dict:
        if self._s3 is None:
            raise RuntimeError("boto3 not installed")
        objects = total = 0
        token = None
        prefix = f"{self.args.s3_prefix.rstrip('/')}/"
        while True:
            kw = {"Bucket": self.args.s3_bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            for obj in resp.get("Contents", []):
                objects += 1
                total += obj["Size"]
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        self.prom.s3_objects.set(objects)
        self.prom.s3_bytes.set(total)
        return {"objects": objects, "bytes": total}

    # -- loop -----------------------------------------------------------------

    def _run_source(self, name: str, fn, sample: dict) -> None:
        started = time.perf_counter()
        try:
            sample[name] = fn()
            sample[f"{name}_ok"] = True
            self.prom.scrape_ok.labels(source=name).set(1)
        except Exception as exc:                                  # noqa: BLE001
            msg = str(exc).split("\n")[0][:200]
            sample[name] = None
            sample[f"{name}_ok"] = False
            sample[f"{name}_error"] = msg
            self.prom.scrape_ok.labels(source=name).set(0)
            self.prom.scrape_errors.labels(source=name).inc()
            log.warning("%s scrape failed: %s", name, msg)
        finally:
            self.prom.scrape_seconds.labels(source=name).set(time.perf_counter() - started)

    def poll_once(self) -> dict:
        now = time.time()
        sample: dict[str, Any] = {
            "ts": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        }
        self._run_source("collector", self.scrape_collector, sample)
        self._run_source("compactor", self.scrape_compactor, sample)
        self._run_source("catalog", self.scrape_catalog, sample)

        # Expensive sources on their own, slower cadence. A full ListObjectsV2
        # over a large prefix is the costliest thing this process does; running
        # it every 10s would make the monitoring a load source of its own.
        if now - self._last_lag >= self.args.watermark_interval:
            self._run_source("watermark", self.scrape_watermark, sample)
            self._lag_cache = sample.get("watermark") or self._lag_cache
            self._last_lag = now
        else:
            sample["watermark"] = self._lag_cache
            sample["watermark_cached"] = True

        if now - self._last_s3 >= self.args.s3_interval:
            self._run_source("s3", self.scrape_s3, sample)
            self._s3_cache = sample.get("s3") or self._s3_cache
            self._last_s3 = now
        else:
            sample["s3"] = self._s3_cache
            sample["s3_cached"] = True

        return sample

    def run(self) -> int:
        start_http_server(self.args.port)
        log.info("prometheus endpoint on :%d, polling every %.1fs",
                 self.args.port, self.args.interval)
        next_tick = time.monotonic()
        while not self._stop:
            sample = self.poll_once()
            if self._jsonl:
                self._jsonl.write(json.dumps(sample, default=str) + "\n")
            if self.args.once:
                print(json.dumps(sample, indent=2, default=str))
                return 0
            next_tick += self.args.interval
            sleep = next_tick - time.monotonic()
            if sleep < 0:
                next_tick = time.monotonic()
                sleep = 0
            time.sleep(sleep)
        return 0

    def stop(self, *_a) -> None:
        log.info("stopping")
        self._stop = True


def parse_args(argv=None) -> argparse.Namespace:
    e = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(e("PIPELINE_EXPORTER_PORT", 9101)))
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--watermark-interval", type=float, default=60.0)
    ap.add_argument("--s3-interval", type=float, default=60.0)
    ap.add_argument("--http-timeout", type=float, default=5.0)
    ap.add_argument("--jsonl", default=None)
    ap.add_argument("--once", action="store_true", help="one poll, print it, exit")

    # ssh   — pull each host's local agent snapshot over port 22 (DEFAULT).
    #         Health checks run on the host itself against its loopback, so
    #         8080/8081 need no inbound security-group rule at all.
    # http   — scrape the health ports directly across the network. Only for an
    #         environment that has deliberately opened them.
    ap.add_argument("--health-mode", choices=["ssh", "http"],
                    default=e("BENCH_HEALTH_MODE", "ssh"))
    ap.add_argument("--ssh-user", default=e("SSH_USER", "ec2-user"))
    ap.add_argument("--ssh-key", default=e("SSH_KEY", ""))
    ap.add_argument("--agent-state-path",
                    default=e("AGENT_STATE_PATH", "/opt/analytics-bench/agent/state/health.json"))
    ap.add_argument("--max-state-age", type=float, default=90.0,
                    help="treat a snapshot older than this as a scrape failure")
    ap.add_argument("--collector-ssh-host", default=e("COLLECTOR_SSH_HOST", e("COLLECTOR_HOST", "")))
    ap.add_argument("--compactor-ssh-host", default=e("COMPACTOR_SSH_HOST", e("COMPACTOR_HOST", "")))

    ap.add_argument("--collector-host", default=e("COLLECTOR_HOST", "127.0.0.1"))
    ap.add_argument("--collector-health-port", type=int, default=int(e("COLLECTOR_HEALTH_PORT", 8081)))
    ap.add_argument("--compactor-host", default=e("COMPACTOR_HOST", "127.0.0.1"))
    ap.add_argument("--compactor-health-port", type=int, default=int(e("COMPACTOR_HEALTH_PORT", 8080)))

    ap.add_argument("--pg-host", default=e("PG_HOST", ""))
    ap.add_argument("--pg-port", type=int, default=int(e("PG_PORT", 5432)))
    ap.add_argument("--pg-database", default=e("PG_DATABASE", "bench_catalog"))
    ap.add_argument("--pg-user", default=e("PG_USER", "analytics"))
    ap.add_argument("--pg-password", default=e("PG_PASSWORD", ""))

    ap.add_argument("--s3-bucket", default=e("S3_BUCKET", ""))
    ap.add_argument("--s3-prefix", default=e("S3_PREFIX", "bench"))
    ap.add_argument("--aws-region", default=e("AWS_REGION", "us-west-2"))

    ap.add_argument("--catalog", default=e("DUCKLAKE_CATALOG", "bench"))
    ap.add_argument("--schema", default=e("DUCKLAKE_SCHEMA", "main"))
    ap.add_argument("--watermark-table", default=e("DUCKLAKE_WATERMARK_TABLE", "ingest_watermark"))
    # Default 8 MiB = the compactor's minor_compaction_max_size.
    ap.add_argument("--small-file-threshold", type=int, default=8 * 1024 * 1024)
    ap.add_argument("--log-level", default="INFO")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)sZ %(levelname)-5s %(name)s %(message)s")
    logging.Formatter.converter = time.gmtime
    if not args.pg_password:
        log.warning("no PG_PASSWORD — catalog and watermark scrapes will fail")
    exporter = PipelineExporter(args)
    signal.signal(signal.SIGINT, exporter.stop)
    signal.signal(signal.SIGTERM, exporter.stop)
    return exporter.run()


if __name__ == "__main__":
    sys.exit(main())
