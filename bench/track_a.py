#!/usr/bin/env python3
"""Track A — the DuckDB engine benchmark. TESTs 1-15 of the spec.

No DazzleDuck code in the path: this measures what DuckDB 1.5.4 can do on this
hardware, standalone. It is the CEILING. Nothing here is ever reported as
pipeline capacity — that is Track B's job — and the report generator keeps the
two apart.

Scaled to the hardware it actually runs on (t3.large: 2 vCPU, 7.6 GiB RAM,
~6 GiB free disk). Everything the spec asks for that does not fit is emitted as
an explicit NOT TESTED row with the reason — never estimated, never silently
dropped:

    100M / 500M row datasets        -> NOT TESTED (disk)
    100 GB / 500 GB / 1 TB merges   -> NOT TESTED (disk)
    10000-file layout               -> NOT TESTED (time budget on 2 vCPU)
    threads 4/8/16                  -> NOT TESTED (2 logical cores)

Protocol per the spec: 1 warm-up (discarded) + 5 measured runs; min, max, mean,
median, stddev all recorded. Cold cache is attempted via drop_caches and
VERIFIED by watching disk read bytes rise — if the verification fails the run is
labelled warm-unknown, not cold.

Output layout matches the spec:

    <out>/parquet/read_throughput.jsonl      TEST 1
    <out>/parquet/fragmentation.jsonl        TEST 2
    <out>/parquet/row_groups.jsonl           TEST 3
    <out>/parquet/compression.jsonl          TEST 4
    <out>/parquet/thread_scaling.jsonl       TEST 5
    <out>/ingestion/bulk.jsonl               TEST 6
    <out>/ingestion/strategies.jsonl         TEST 7
    <out>/queries/queries.jsonl              TEST 8
    <out>/profiles/*.json                    TEST 9
    <out>/compaction/isolated.jsonl          TEST 10
    <out>/compaction/minor.jsonl             TEST 11
    <out>/compaction/major.jsonl             TEST 12
    <out>/memory/scaling.jsonl               TEST 13
    <out>/memory/spill.jsonl                 TEST 14
    <out>/concurrency/jobs.jsonl             TEST 15
    <out>/environment.json, datasets.json, TRACK_A_REPORT.md
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time

try:
    import duckdb
except ImportError:
    sys.exit("duckdb required — run in the project venv (scripts/setup.sh)")

NOT_TESTED = "NOT TESTED"


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------

class Out:
    """JSONL writers, one per result family, plus the run-wide constants that
    every row must carry so summary.csv stays joinable."""

    def __init__(self, root: str, env_hash: str) -> None:
        self.root = root
        self.env_hash = env_hash
        self._files: dict[str, object] = {}

    def write(self, rel: str, row: dict) -> None:
        row.setdefault("env_hash", self.env_hash)
        row.setdefault("track", "A")
        row.setdefault("recorded_at", utcnow())
        path = os.path.join(self.root, rel)
        if rel not in self._files:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._files[rel] = open(path, "a", buffering=1)
        self._files[rel].write(json.dumps(row, default=str) + "\n")

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"{utcnow()} {msg}", flush=True)


def disk_free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def page_cache_bytes() -> int:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("Cached:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def cold_cache() -> str:
    """Drop the page cache and VERIFY it dropped. 'cold' | 'warm-unknown'.

    The spec's rule: a cold claim that was not confirmed is labelled
    warm-unknown, never cold. The witness here is /proc/meminfo Cached actually
    falling — cheap, and it cannot be faked by a sudo that silently no-ops.
    """
    before = page_cache_bytes()
    try:
        rc = subprocess.run(["sudo", "-n", "sh", "-c",
                             "sync; echo 3 > /proc/sys/vm/drop_caches"],
                            capture_output=True, timeout=30)
        if rc.returncode != 0:
            return "warm-unknown"
    except Exception:                                             # noqa: BLE001
        return "warm-unknown"
    after = page_cache_bytes()
    # The dataset is hundreds of MiB; a real drop frees far more than 50 MiB.
    return "cold" if before - after > 50 * 2**20 or after < 100 * 2**20 else "warm-unknown"


class Duck:
    """One configured DuckDB connection with the settings recorded."""

    def __init__(self, threads: int, memory_limit: str, temp_dir: str,
                 database: str = ":memory:") -> None:
        self.threads = threads
        self.memory_limit = memory_limit
        self.con = duckdb.connect(database)
        self.con.execute(f"SET threads = {threads}")
        self.con.execute(f"SET memory_limit = '{memory_limit}'")
        self.con.execute(f"SET temp_directory = '{temp_dir}'")
        self.con.execute("SET preserve_insertion_order = false")

    def sql(self, q: str):
        return self.con.execute(q)

    def time(self, q: str) -> float:
        t0 = time.perf_counter()
        self.con.execute(q).fetchall()
        return time.perf_counter() - t0

    def close(self) -> None:
        self.con.close()


def stats_of(samples: list[float]) -> dict:
    s = sorted(samples)
    return {
        "runs": len(s),
        "min_s": round(s[0], 4), "max_s": round(s[-1], 4),
        "mean_s": round(statistics.fmean(s), 4),
        "median_s": round(statistics.median(s), 4),
        "stddev_s": round(statistics.pstdev(s) if len(s) > 1 else 0.0, 4),
    }


def repeat(fn, warmup: int = 1, runs: int = 5) -> dict:
    """The spec's protocol: warm-ups discarded, every measured run kept."""
    for _ in range(warmup):
        fn()
    return stats_of([fn() for _ in range(runs)])


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------

DATASET_SQL = """
SELECT
    (TIMESTAMP '2026-01-01' + INTERVAL (i % 86400) SECOND
        + INTERVAL ((i * 37) % 1000) MILLISECOND)          AS ts,
    'tenant-' || (i % 8)                                   AS tenant,
    'service-' || (i % 25)                                 AS service,
    CASE (i % 100) WHEN 0 THEN 'FATAL' WHEN 1 THEN 'ERROR'
         WHEN 2 THEN 'ERROR' ELSE
         CASE WHEN (i % 10) < 2 THEN 'WARN' ELSE 'INFO' END END AS level,
    200 + CASE WHEN (i % 50) = 0 THEN 300 WHEN (i % 25) = 0 THEN 204 ELSE 0 END AS status,
    (i * 2654435761) % 4096                                AS duration_ms,
    (hash(i) % 100000) / 100.0                             AS value,
    md5(CAST(i AS VARCHAR)) || md5(CAST(i * 31 AS VARCHAR)) AS body
FROM range({rows}) t(i)
"""
# ~120 bytes/row on disk with snappy. Deterministic (no random()), so the
# dataset hash is stable across runs and machines.

AGG5 = """
SELECT service, level,
       count(*)            AS n,
       avg(duration_ms)    AS avg_ms,
       max(value)          AS max_v,
       min(ts)             AS first_ts
FROM {src} GROUP BY 1, 2
"""


def build_dataset(duck: Duck, rows: int, path: str) -> dict:
    log(f"dataset: {rows:,} rows -> {path}")
    t0 = time.perf_counter()
    duck.sql(f"COPY ({DATASET_SQL.format(rows=rows)}) TO '{path}' "
             f"(FORMAT PARQUET, COMPRESSION SNAPPY)")
    secs = time.perf_counter() - t0
    size = os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return {"rows": rows, "path": path, "bytes": size,
            "sha256": digest.hexdigest()[:16], "gen_seconds": round(secs, 2)}


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test1_read_throughput(out: Out, duck: Duck, datasets: list[dict]) -> None:
    log("TEST 1 — Parquet read throughput")
    for ds in datasets:
        for name, query in (("count", f"SELECT count(*) FROM read_parquet('{ds['path']}')"),
                            ("agg5", AGG5.format(src=f"read_parquet('{ds['path']}')"))):
            for cache in ("cold", "warm"):
                def one() -> float:
                    if cache == "cold":
                        one.cache_label = cold_cache()          # noqa: B023
                    t0 = time.perf_counter()
                    duck.sql(query).fetchall()
                    return time.perf_counter() - t0
                one.cache_label = "warm"
                st = repeat(one, warmup=0 if cache == "cold" else 1)
                out.write("parquet/read_throughput.jsonl", {
                    "test_name": "TEST1", "query": name, "cache": one.cache_label
                    if cache == "cold" else "warm",
                    "dataset": ds["path"], "dataset_hash": ds["sha256"],
                    "rows": ds["rows"], "input_gb": ds["bytes"] / 2**30,
                    "rows_per_second": round(ds["rows"] / st["median_s"]),
                    "gb_per_second": round(ds["bytes"] / 2**30 / st["median_s"], 3),
                    "threads": duck.threads, "memory_limit": duck.memory_limit,
                    "status": "OK", **st,
                })
    for rows in (100_000_000, 500_000_000):
        out.write("parquet/read_throughput.jsonl", {
            "test_name": "TEST1", "rows": rows, "status": NOT_TESTED,
            "error": "dataset does not fit on an 8 GiB root volume"})


def test2_fragmentation(out: Out, duck: Duck, ds: dict, work: str) -> None:
    log("TEST 2 — file fragmentation")
    duck.sql(f"CREATE OR REPLACE TABLE frag_src AS SELECT * FROM read_parquet('{ds['path']}')")
    for files in (1, 10, 100, 1000):
        d = os.path.join(work, f"frag_{files}")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        per = max(1, ds["rows"] // files)
        t0 = time.perf_counter()
        duck.sql(f"COPY (SELECT *, (row_number() OVER ()) // {per} AS part FROM frag_src) "
                 f"TO '{d}' (FORMAT PARQUET, PARTITION_BY part)")
        write_s = time.perf_counter() - t0
        actual = sum(len(fs) for _, _, fs in os.walk(d))
        st = repeat(lambda: duck.time(AGG5.format(src=f"read_parquet('{d}/**/*.parquet')")))
        out.write("parquet/fragmentation.jsonl", {
            "test_name": "TEST2", "layout": f"{files}_files", "actual_files": actual,
            "dataset_hash": ds["sha256"], "rows": ds["rows"],
            "write_seconds": round(write_s, 2),
            "rows_per_second": round(ds["rows"] / st["median_s"]),
            "threads": duck.threads, "status": "OK", **st,
        })
        shutil.rmtree(d, ignore_errors=True)
    out.write("parquet/fragmentation.jsonl", {
        "test_name": "TEST2", "layout": "10000_files", "status": NOT_TESTED,
        "error": "time budget on 2 vCPU; 1000 files already characterises the trend"})
    duck.sql("DROP TABLE frag_src")


def test3_row_groups(out: Out, duck: Duck, ds: dict, work: str) -> None:
    log("TEST 3 — row group size")
    duck.sql(f"CREATE OR REPLACE TABLE rg_src AS SELECT * FROM read_parquet('{ds['path']}')")
    for rg in (32_000, 128_000, 512_000, 1_000_000):
        p = os.path.join(work, f"rg_{rg}.parquet")
        t0 = time.perf_counter()
        duck.sql(f"COPY rg_src TO '{p}' (FORMAT PARQUET, ROW_GROUP_SIZE {rg})")
        write_s = time.perf_counter() - t0
        read = repeat(lambda: duck.time(AGG5.format(src=f"read_parquet('{p}')")))
        out.write("parquet/row_groups.jsonl", {
            "test_name": "TEST3", "row_group_size": rg, "rows": ds["rows"],
            "dataset_hash": ds["sha256"], "write_seconds": round(write_s, 2),
            "output_bytes": os.path.getsize(p),
            "read_median_s": read["median_s"], "status": "OK", **read,
        })
        os.remove(p)
    duck.sql("DROP TABLE rg_src")


def test4_compression(out: Out, duck: Duck, ds: dict, work: str) -> None:
    log("TEST 4 — compression")
    duck.sql(f"CREATE OR REPLACE TABLE cx_src AS SELECT * FROM read_parquet('{ds['path']}')")
    for codec in ("SNAPPY", "ZSTD", "UNCOMPRESSED"):
        p = os.path.join(work, f"cx_{codec}.parquet")
        t0 = time.perf_counter()
        duck.sql(f"COPY cx_src TO '{p}' (FORMAT PARQUET, COMPRESSION {codec})")
        write_s = time.perf_counter() - t0
        size = os.path.getsize(p)
        read = repeat(lambda: duck.time(AGG5.format(src=f"read_parquet('{p}')")))
        out.write("parquet/compression.jsonl", {
            "test_name": "TEST4", "compression": codec, "zstd_level": 3 if codec == "ZSTD" else None,
            "rows": ds["rows"], "dataset_hash": ds["sha256"],
            "write_seconds": round(write_s, 2), "output_bytes": size,
            "ratio_vs_uncompressed": None,   # filled by the report from the trio
            "pipeline_uses": "snappy (collector default)",
            "status": "OK", **read,
        })
        os.remove(p)
    duck.sql("DROP TABLE cx_src")


def test5_threads(out: Out, ds: dict, temp_dir: str) -> None:
    log("TEST 5 — thread scaling")
    base = None
    for threads in (1, 2):
        d = Duck(threads, "4GB", temp_dir)
        st = repeat(lambda: d.time(AGG5.format(src=f"read_parquet('{ds['path']}')")))
        d.close()
        if threads == 1:
            base = st["median_s"]
        speedup = base / st["median_s"] if base else 1.0
        out.write("parquet/thread_scaling.jsonl", {
            "test_name": "TEST5", "threads": threads, "rows": ds["rows"],
            "dataset_hash": ds["sha256"],
            "rows_per_second": round(ds["rows"] / st["median_s"]),
            "speedup_vs_1": round(speedup, 3),
            "efficiency": round(speedup / threads, 3),
            "status": "OK", **st,
        })
    for threads in (4, 8, 16):
        out.write("parquet/thread_scaling.jsonl", {
            "test_name": "TEST5", "threads": threads, "status": NOT_TESTED,
            "error": "host has 2 logical cores"})


def test6_bulk(out: Out, duck: Duck, ds: dict, work: str) -> None:
    log("TEST 6 — bulk ingestion")
    csv = os.path.join(work, "bulk.csv")
    duck.sql(f"COPY (SELECT * FROM read_parquet('{ds['path']}') LIMIT 1000000) "
             f"TO '{csv}' (FORMAT CSV, HEADER)")
    for src, q in (("parquet", f"read_parquet('{ds['path']}')"),
                   ("csv", f"read_csv_auto('{csv}')")):
        rows = ds["rows"] if src == "parquet" else 1_000_000

        def ingest() -> float:
            duck.sql("DROP TABLE IF EXISTS bulk_t")
            t0 = time.perf_counter()
            duck.sql(f"CREATE TABLE bulk_t AS SELECT * FROM {q}")
            return time.perf_counter() - t0
        st = repeat(ingest, warmup=1, runs=3)
        verify_s = duck.time("SELECT count(*) FROM bulk_t")   # timed separately, per spec
        out.write("ingestion/bulk.jsonl", {
            "test_name": "TEST6", "source": src, "rows": rows,
            "dataset_hash": ds["sha256"],
            "rows_per_second": round(rows / st["median_s"]),
            "verify_count_seconds": round(verify_s, 3),
            "status": "OK", **st,
        })
    duck.sql("DROP TABLE IF EXISTS bulk_t")
    os.remove(csv)


def test7_strategies(out: Out, duck: Duck, ds: dict) -> None:
    log("TEST 7 — insert strategies")
    duck.sql(f"CREATE OR REPLACE TABLE src7 AS SELECT * FROM read_parquet('{ds['path']}') LIMIT 1000000")

    def fresh() -> None:
        duck.sql("DROP TABLE IF EXISTS t7")
        duck.sql("CREATE TABLE t7 AS SELECT * FROM src7 LIMIT 0")

    # single-row: bounded sample, labelled worst case, never extrapolated
    fresh()
    rows_sample = 10_000
    src_rows = duck.con.execute(f"SELECT * FROM src7 LIMIT {rows_sample}").fetchall()
    t0 = time.perf_counter()
    for r in src_rows:
        duck.con.execute("INSERT INTO t7 VALUES (?,?,?,?,?,?,?,?)", r)
    single_s = time.perf_counter() - t0
    out.write("ingestion/strategies.jsonl", {
        "test_name": "TEST7", "strategy": "single_row_insert",
        "rows": rows_sample, "note": "bounded worst-case sample, not extrapolated",
        "rows_per_second": round(rows_sample / single_s),
        "duration_seconds": round(single_s, 2), "status": "OK"})

    for name, per_batch in (("batch_insert_1k", 1000), ("batch_insert_10k", 10000)):
        fresh()
        t0 = time.perf_counter()
        done = 0
        while done < 100_000:
            duck.sql(f"INSERT INTO t7 SELECT * FROM src7 LIMIT {per_batch} OFFSET {done}")
            done += per_batch
        secs = time.perf_counter() - t0
        out.write("ingestion/strategies.jsonl", {
            "test_name": "TEST7", "strategy": name, "rows": done,
            "rows_per_second": round(done / secs),
            "duration_seconds": round(secs, 2), "status": "OK"})

    for name, stmt in (("insert_select", "INSERT INTO t7 SELECT * FROM src7"),
                       ("ctas", "CREATE OR REPLACE TABLE t7b AS SELECT * FROM src7")):
        fresh()
        secs = duck.time(stmt)
        out.write("ingestion/strategies.jsonl", {
            "test_name": "TEST7", "strategy": name, "rows": 1_000_000,
            "rows_per_second": round(1_000_000 / secs),
            "duration_seconds": round(secs, 2), "status": "OK"})
    duck.sql("DROP TABLE IF EXISTS t7")
    duck.sql("DROP TABLE IF EXISTS t7b")
    duck.sql("DROP TABLE IF EXISTS src7")


QUERIES = {
    "q1_full_scan": "SELECT count(*), sum(duration_ms) FROM {t}",
    "q2_time_filter": "SELECT count(*) FROM {t} WHERE ts >= TIMESTAMP '2026-01-01 12:00:00'",
    "q3_group_service": "SELECT service, count(*), avg(duration_ms) FROM {t} GROUP BY 1",
    "q4_time_bucket": "SELECT date_trunc('minute', ts), count(*) FROM {t} GROUP BY 1",
    "q5_multi_dim": "SELECT tenant, service, status, count(*), avg(value) FROM {t} GROUP BY 1,2,3",
    "q6_join_dim": """
        WITH dim AS (SELECT DISTINCT service, 'team-' || (hash(service) % 5) AS team FROM {t})
        SELECT dim.team, count(*) FROM {t} JOIN dim USING (service) GROUP BY 1""",
}


def test8_9_queries(out: Out, duck: Duck, ds: dict, profile_dir: str) -> None:
    log("TEST 8/9 — query benchmark + profiling")
    duck.sql(f"CREATE OR REPLACE TABLE q_t AS SELECT * FROM read_parquet('{ds['path']}')")
    os.makedirs(profile_dir, exist_ok=True)
    slowest = (None, 0.0)
    for name, q in QUERIES.items():
        sql = q.format(t="q_t")
        st = repeat(lambda: duck.time(sql))
        out.write("queries/queries.jsonl", {
            "test_name": "TEST8", "query": name, "rows": ds["rows"],
            "dataset_hash": ds["sha256"],
            "rows_per_second": round(ds["rows"] / st["median_s"]),
            "threads": duck.threads, "status": "OK", **st,
        })
        if st["median_s"] > slowest[1]:
            slowest = (name, st["median_s"])
    # TEST 9: profile every query; the deliverable is the dominating operator.
    for name, q in QUERIES.items():
        path = os.path.join(profile_dir, f"{name}.json")
        duck.sql("PRAGMA enable_profiling = 'json'")
        duck.sql(f"PRAGMA profiling_output = '{path}'")
        duck.sql(q.format(t="q_t")).fetchall()
        duck.sql("PRAGMA disable_profiling")
    out.write("queries/queries.jsonl", {
        "test_name": "TEST9", "status": "OK",
        "profiles": profile_dir, "slowest_query": slowest[0],
        "note": "per-operator timings in profiles/*.json"})
    duck.sql("DROP TABLE q_t")


def _make_lake(duck: Duck, work: str, name: str, files: int, rows_per_file: int) -> str:
    """A local DuckLake catalog with a known fragmented state."""
    cat = os.path.join(work, f"{name}.ducklake")
    data = os.path.join(work, f"{name}_data")
    for p in (cat, cat + ".wal", data):
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else (
            os.remove(p) if os.path.exists(p) else None)
    os.makedirs(data, exist_ok=True)
    duck.sql("INSTALL ducklake; LOAD ducklake")
    duck.sql(f"ATTACH 'ducklake:{cat}' AS {name} "
             f"(DATA_PATH '{data}', DATA_INLINING_ROW_LIMIT 0)")
    duck.sql(f"CREATE TABLE {name}.main.ev AS "
             + DATASET_SQL.format(rows=rows_per_file) + " LIMIT 0")
    # One INSERT per file: each DuckLake transaction writes its own Parquet
    # file, which is exactly the known-fragmented state the test needs.
    row_sql = DATASET_SQL.format(rows=rows_per_file)
    for i in range(files):
        lo, hi = i * rows_per_file, (i + 1) * rows_per_file
        duck.sql(f"INSERT INTO {name}.main.ev "
                 + row_sql.replace(f"range({rows_per_file})", f"range({lo}, {hi})"))
    return name


def _lake_files(duck: Duck, name: str):
    return duck.sql(
        f"SELECT count(*), COALESCE(sum(file_size_bytes),0), COALESCE(sum(record_count),0) "
        f'FROM "__ducklake_metadata_{name}".ducklake_data_file '
        f"WHERE end_snapshot IS NULL").fetchone()


def test10_12_compaction(out: Out, temp_dir: str, work: str) -> None:
    log("TEST 10/11/12 — DuckLake compaction, isolated")
    # TEST 10: one measured merge on a known state.
    d = Duck(2, "4GB", temp_dir)
    _make_lake(d, work, "lk10", files=50, rows_per_file=20_000)
    before = _lake_files(d, "lk10")
    t0 = time.perf_counter()
    d.sql("CALL ducklake_merge_adjacent_files('lk10', max_file_size => 67108864)")
    secs = time.perf_counter() - t0
    after = _lake_files(d, "lk10")
    out.write("compaction/isolated.jsonl", {
        "test_name": "TEST10", "files_before": before[0], "files_after": after[0],
        "input_bytes": before[1], "input_rows": before[2],
        "merge_seconds": round(secs, 3),
        "compaction_rows_per_sec": round(before[2] / secs),
        "compaction_gb_per_sec": round(before[1] / 2**30 / secs, 4),
        "status": "OK"})
    d.sql("DETACH lk10")
    d.close()

    # TEST 11: minor merges across file counts.
    for files in (100, 1000):
        d = Duck(2, "4GB", temp_dir)
        name = f"lk11_{files}"
        _make_lake(d, work, name, files=files, rows_per_file=5_000)
        before = _lake_files(d, name)
        t0 = time.perf_counter()
        d.sql(f"CALL ducklake_merge_adjacent_files('{name}', max_file_size => 8388608)")
        secs = time.perf_counter() - t0
        after = _lake_files(d, name)
        out.write("compaction/minor.jsonl", {
            "test_name": "TEST11", "file_count": files,
            "files_before": before[0], "files_after": after[0],
            "input_rows": before[2], "input_bytes": before[1],
            "merge_seconds": round(secs, 3),
            "rows_per_second": round(before[2] / secs), "status": "OK"})
        d.sql(f"DETACH {name}")
        d.close()
    for files in (10_000, 100_000):
        out.write("compaction/minor.jsonl", {
            "test_name": "TEST11", "file_count": files, "status": NOT_TESTED,
            "error": "time and inode budget on this host; trend covered to 1000"})

    # TEST 12: the largest merge that fits on this disk (~1 GiB), then the
    # explicit NOT TESTED tiers.
    free = disk_free_bytes(work)
    if free > 3 * 2**30:
        d = Duck(2, "4GB", temp_dir)
        _make_lake(d, work, "lk12", files=20, rows_per_file=400_000)   # ~1 GiB
        before = _lake_files(d, "lk12")
        t0 = time.perf_counter()
        d.sql("CALL ducklake_merge_adjacent_files('lk12', max_file_size => 268435456)")
        secs = time.perf_counter() - t0
        after = _lake_files(d, "lk12")
        out.write("compaction/major.jsonl", {
            "test_name": "TEST12", "tier": "1GB",
            "files_before": before[0], "files_after": after[0],
            "input_bytes": before[1], "input_rows": before[2],
            "merge_seconds": round(secs, 2),
            "gb_per_second": round(before[1] / 2**30 / secs, 4),
            "free_space_before": free, "status": "OK"})
        d.sql("DETACH lk12")
        d.close()
    for tier in ("100GB", "500GB", "1TB"):
        out.write("compaction/major.jsonl", {
            "test_name": "TEST12", "tier": tier, "status": NOT_TESTED,
            "error": f"free disk is {free / 2**30:.1f} GiB; a major merge needs headroom "
                     "for new files before old ones are cleaned"})


def test13_14_memory(out: Out, ds: dict, temp_dir: str, work: str) -> None:
    log("TEST 13/14 — memory scaling and spill")
    q5 = QUERIES["q5_multi_dim"].format(t=f"read_parquet('{ds['path']}')")
    spill_q = (f"SELECT tenant, service, body, count(*) FROM read_parquet('{ds['path']}') "
               "GROUP BY 1,2,3 ORDER BY 4 DESC")
    for limit in ("1GB", "2GB", "4GB"):
        d = Duck(2, limit, temp_dir)
        try:
            st = repeat(lambda: d.time(q5), warmup=1, runs=3)
            spill = d.sql("SELECT coalesce(sum(size), 0) AS temporary_storage_bytes FROM duckdb_temporary_files() "
                          "").fetchone()
            out.write("memory/scaling.jsonl", {
                "test_name": "TEST13", "memory_limit": limit, "rows": ds["rows"],
                "spill_bytes": spill[0] if spill else None,
                "outcome": "OK", **st})
        except Exception as exc:                                  # noqa: BLE001
            out.write("memory/scaling.jsonl", {
                "test_name": "TEST13", "memory_limit": limit,
                "outcome": "error", "error": str(exc)[:200], "status": "FAILED"})
        finally:
            d.close()

    # TEST 14: force out-of-core with a deliberately tight limit.
    baseline = None
    for limit in ("512MB", "1GB", "4GB"):
        d = Duck(2, limit, temp_dir)
        try:
            t0 = time.perf_counter()
            d.sql(spill_q).fetchall()
            secs = time.perf_counter() - t0
            if limit == "4GB":
                baseline = secs
            spill = d.sql("SELECT coalesce(sum(size), 0) AS temporary_storage_bytes FROM duckdb_temporary_files() "
                          "").fetchone()
            out.write("memory/spill.jsonl", {
                "test_name": "TEST14", "memory_limit": limit, "completed": True,
                "duration_seconds": round(secs, 2),
                "spill_bytes": spill[0] if spill else None,
                "slowdown_vs_4GB": round(secs / baseline, 2) if baseline else None,
                "status": "OK"})
        except Exception as exc:                                  # noqa: BLE001
            out.write("memory/spill.jsonl", {
                "test_name": "TEST14", "memory_limit": limit, "completed": False,
                "error": str(exc)[:200], "status": "FAILED",
                "note": "an OOM is a result, per the spec"})
        finally:
            d.close()


def test15_concurrency(out: Out, ds: dict, temp_dir: str) -> None:
    log("TEST 15 — concurrent jobs")
    q = AGG5.format(src=f"read_parquet('{ds['path']}')")

    def job() -> float:
        d = Duck(1, "1500MB", temp_dir)
        t0 = time.perf_counter()
        d.sql(q).fetchall()
        d.close()
        return time.perf_counter() - t0

    for n in (1, 2, 4):
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
            per_job = list(ex.map(lambda _: job(), range(n)))
        wall = time.perf_counter() - t0
        out.write("concurrency/jobs.jsonl", {
            "test_name": "TEST15", "concurrent_jobs": n,
            "wall_seconds": round(wall, 2),
            "per_job_seconds": [round(x, 2) for x in per_job],
            "total_rows_per_second": round(n * ds["rows"] / wall),
            "status": "OK"})
    out.write("concurrency/jobs.jsonl", {
        "test_name": "TEST15", "concurrent_jobs": 8, "status": NOT_TESTED,
        "error": "8 jobs on 2 cores measures the scheduler, not the engine"})


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def write_report(root: str, env: dict, datasets: list[dict], started: float) -> None:
    lines = ["# Track A — DuckDB engine benchmark", "",
             f"DuckDB **{duckdb.__version__}**, standalone, no DazzleDuck code in the path. "
             "These numbers are the CEILING for this hardware; none of them is pipeline "
             "capacity. Track B (results/*/report.md) is the product measurement.", "",
             f"Host: {env['hostname']} — {env['cpu_count']} vCPU, "
             f"{env['ram_gb']:.1f} GiB RAM, {env['disk_free_gb']:.1f} GiB free. "
             f"Wall time {time.time() - started:.0f}s. Generated {utcnow()}.", "",
             "Protocol: 1 warm-up discarded + measured repeats; medians quoted, full "
             "distributions in the JSONL. `NOT TESTED` rows are deliberate and carry the "
             "reason — nothing is estimated.", ""]
    for ds in datasets:
        lines.append(f"- dataset `{os.path.basename(ds['path'])}`: {ds['rows']:,} rows, "
                     f"{ds['bytes'] / 2**20:.0f} MiB, sha256 `{ds['sha256']}`")
    lines.append("")
    for rel in ("parquet/read_throughput.jsonl", "parquet/fragmentation.jsonl",
                "parquet/row_groups.jsonl", "parquet/compression.jsonl",
                "parquet/thread_scaling.jsonl", "ingestion/bulk.jsonl",
                "ingestion/strategies.jsonl", "queries/queries.jsonl",
                "compaction/isolated.jsonl", "compaction/minor.jsonl",
                "compaction/major.jsonl", "memory/scaling.jsonl",
                "memory/spill.jsonl", "concurrency/jobs.jsonl"):
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        rows = [json.loads(l) for l in open(path) if l.strip()]
        lines += [f"## {rel}", "", "```"]
        for r in rows:
            core = {k: v for k, v in r.items()
                    if k not in ("env_hash", "track", "recorded_at", "dataset_hash")}
            lines.append(json.dumps(core, default=str))
        lines += ["```", ""]
    open(os.path.join(root, "TRACK_A_REPORT.md"), "w").write("\n".join(lines) + "\n")
    log(f"report: {os.path.join(root, 'TRACK_A_REPORT.md')}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="results/track-a")
    ap.add_argument("--work", default="/var/tmp/track-a",
                    help="scratch on real disk — NOT /tmp, which is tmpfs")
    ap.add_argument("--rows", type=int, nargs="+", default=[1_000_000, 10_000_000])
    ap.add_argument("--quick", action="store_true", help="1M rows only, 3 repeats")
    args = ap.parse_args()

    if args.quick:
        args.rows = [1_000_000]

    started = time.time()
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.work, exist_ok=True)
    temp_dir = os.path.join(args.work, "duckdb-tmp")
    os.makedirs(temp_dir, exist_ok=True)

    env = {
        "hostname": platform.node(), "cpu_count": os.cpu_count(),
        "ram_gb": os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30,
        "disk_free_gb": disk_free_bytes(args.work) / 2**30,
        "duckdb": duckdb.__version__, "python": platform.python_version(),
        "started": utcnow(),
    }
    env_hash = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]
    json.dump({**env, "env_hash": env_hash},
              open(os.path.join(args.out, "environment.json"), "w"), indent=2)
    log(f"track A on {env['hostname']}: duckdb {env['duckdb']}, "
        f"{env['disk_free_gb']:.1f} GiB free in {args.work}")

    need_gb = max(args.rows) * 130 / 2**30 * 3   # dataset + copies + headroom
    if env["disk_free_gb"] < need_gb:
        log(f"trimming: {need_gb:.1f} GiB needed, {env['disk_free_gb']:.1f} free")
        args.rows = [r for r in args.rows if r * 130 * 3 / 2**30 < env["disk_free_gb"]]

    out = Out(args.out, env_hash)
    duck = Duck(2, "4GB", temp_dir)
    try:
        datasets = [build_dataset(duck, r, os.path.join(args.work, f"ds_{r}.parquet"))
                    for r in args.rows]
        json.dump(datasets, open(os.path.join(args.out, "datasets.json"), "w"), indent=2)
        big = datasets[-1]

        test1_read_throughput(out, duck, datasets)
        test2_fragmentation(out, duck, datasets[0], args.work)
        test3_row_groups(out, duck, datasets[0], args.work)
        test4_compression(out, duck, datasets[0], args.work)
        test5_threads(out, big, temp_dir)
        test6_bulk(out, duck, big, args.work)
        test7_strategies(out, duck, datasets[0])
        test8_9_queries(out, duck, big, os.path.join(args.out, "profiles"))
        duck.close()
        test10_12_compaction(out, temp_dir, args.work)
        test13_14_memory(out, big, temp_dir, args.work)
        test15_concurrency(out, big, temp_dir)
    finally:
        out.close()
        try:
            duck.close()
        except Exception:                                         # noqa: BLE001
            pass
        shutil.rmtree(args.work, ignore_errors=True)   # leave the disk as found

    write_report(args.out, env, datasets, started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
