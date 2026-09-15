#!/usr/bin/env python3
"""Reconcile what landed in the lake against what the generator says it sent.

A throughput number from a run that lost or duplicated rows is not a throughput
number. This is the gate.

Three questions, answered from the data and nothing else:

  1. LOSS         acked rows that are not in the table
  2. DUPLICATION  any (gen_id, seq) pair present more than once
  3. GAPS         seq ranges that are not contiguous per generator

The reconciliation contract is manifest.json: it records, per generator, the
UUID and the exact seq range attempted, plus the total the collector ACKED.
Landed rows must equal ACKED rows exactly.

  offered - accepted  = rejected or failed. Expected under backpressure, NOT
                        loss, but it must be accounted for.
  accepted - landed   = LOSS. A hard failure at any throughput.
  landed - accepted   = DUPLICATION. Equally a hard failure.

Every record carries `bench.gen_id` and `bench.seq` as OTLP attributes, which
the collector flattens into the `attributes` MAP(VARCHAR, VARCHAR) column — so
seq arrives as text and is cast back here.

Post-compaction the same checks are re-run along with column checksums, because
a merge that changes a single value is a correctness failure that no row count
would catch.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import duckdb
except ImportError:
    sys.exit("duckdb required — run scripts/setup.sh")


def attach(args) -> "duckdb.DuckDBPyConnection":
    con = duckdb.connect(":memory:")
    for ext in ("httpfs", "aws", "ducklake", "postgres"):
        con.execute(f"INSTALL {ext}")
        con.execute(f"LOAD {ext}")
    con.execute("CREATE OR REPLACE SECRET s3_role "
                f"(TYPE S3, PROVIDER credential_chain, REGION '{args.aws_region}')")
    con.execute(
        f"ATTACH 'ducklake:postgres:host={args.pg_host} port={args.pg_port} "
        f"dbname={args.pg_database} user={args.pg_user} password={args.pg_password}' "
        f"AS {args.catalog} (DATA_PATH 's3://{args.s3_bucket}/{args.s3_prefix}/', "
        f"DATA_INLINING_ROW_LIMIT 0)")
    return con


def build_view(con, args, run_start: str, run_end: str, gen_ids: list[str]) -> None:
    """Two views: everything in the time window, and just THIS run's rows.

    Scoping by gen_id as well as by time is not optional. The window has to be
    padded (clocks, the settle period), so it will happily contain rows from a
    health-check probe or an overlapping test. Counting those as "landed" makes
    the row-count check report duplication that did not happen — observed
    exactly that: a 25-record contract test inside the window turned a clean run
    into VERDICT FAIL while `duplicates` and `contiguity` both passed. Three
    checks disagreeing is the tell that the odd one out is wrong.

    The manifest declares which generators belong to the run. That is the
    authority; anything else in the window is someone else's traffic and is
    reported separately rather than blamed on this run.

    element_at(...)[1] rather than attributes['k'] because the [] operator's
    return type has changed across DuckDB versions (scalar vs list); element_at
    is stable and always yields a list.
    """
    table = f"{args.catalog}.{args.schema}.{args.table}"
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW window_rows AS
        SELECT
            element_at(attributes, 'bench.gen_id')[1]                  AS gen_id,
            TRY_CAST(element_at(attributes, 'bench.seq')[1] AS BIGINT) AS seq,
            timestamp,
            severity_text,
            body
        FROM {table}
        WHERE timestamp >= TIMESTAMP '{run_start}'
          AND timestamp <= TIMESTAMP '{run_end}'
    """)

    if gen_ids:
        quoted = ", ".join("'" + g.replace("'", "''") + "'" for g in gen_ids)
        predicate = f"gen_id IN ({quoted})"
    else:
        # No generators in the manifest: fall back to the whole window and say
        # so, rather than silently validating nothing.
        predicate = "TRUE"
    con.execute(f"CREATE OR REPLACE TEMP VIEW run_rows AS "
                f"SELECT * FROM window_rows WHERE {predicate}")


def scalar(con, sql: str, default=0):
    row = con.execute(sql).fetchone()
    return default if row is None or row[0] is None else row[0]


def validate(con, args, manifest: dict) -> dict:
    gens = manifest.get("generators") or []
    accepted = int(manifest.get("total_accepted", 0))
    offered = int(manifest.get("total_offered", 0))
    rejected = int(manifest.get("total_rejected", 0))

    result: dict = {
        "test_id": manifest.get("test_id"),
        "offered": offered,
        "accepted": accepted,
        "rejected": rejected,
        "checks": {},
        "per_generator": [],
    }

    landed = int(scalar(con, "SELECT count(*) FROM run_rows"))
    result["landed"] = landed

    # Rows in the same time window that belong to some other generator — a
    # health-check probe, an overlapping test. Informational: attributing them
    # to this run is what produces a phantom duplication failure.
    in_window = int(scalar(con, "SELECT count(*) FROM window_rows"))
    result["rows_in_window"] = in_window
    result["foreign_rows_in_window"] = in_window - landed

    unparseable = int(scalar(con, "SELECT count(*) FROM run_rows WHERE seq IS NULL OR gen_id IS NULL"))
    result["rows_missing_identity"] = unparseable

    # -- 1. loss / duplication in aggregate -----------------------------------
    # Scoped to this run's gen_ids, so the comparison is like for like.
    delta = landed - accepted
    result["checks"]["row_count"] = {
        "pass": delta == 0,
        "landed": landed,
        "accepted": accepted,
        "delta": delta,
        "detail": ("exact match" if delta == 0 else
                   f"{-delta} acked rows never landed — DATA LOSS" if delta < 0 else
                   f"{delta} more rows landed than were acked for this run's generator(s) "
                   "— DUPLICATION"),
    }

    # -- 2. duplicate (gen_id, seq) -------------------------------------------
    dup_pairs = int(scalar(con, """
        SELECT count(*) FROM (
            SELECT gen_id, seq FROM run_rows
            WHERE gen_id IS NOT NULL AND seq IS NOT NULL
            GROUP BY 1, 2 HAVING count(*) > 1)"""))
    dup_rows = int(scalar(con, """
        SELECT COALESCE(sum(c - 1), 0) FROM (
            SELECT count(*) AS c FROM run_rows
            WHERE gen_id IS NOT NULL AND seq IS NOT NULL
            GROUP BY gen_id, seq HAVING count(*) > 1)"""))
    result["checks"]["duplicates"] = {
        "pass": dup_pairs == 0,
        "duplicate_keys": dup_pairs,
        "excess_rows": dup_rows,
        "detail": "no (gen_id, seq) appears twice" if dup_pairs == 0
                  else f"{dup_pairs} keys duplicated, {dup_rows} excess rows",
    }

    # -- 3. per-generator contiguity ------------------------------------------
    all_contiguous = True
    for gen in gens:
        gid = gen.get("gen_id")
        expect_min = int(gen.get("seq_min", 0))
        expect_max = int(gen.get("seq_max", -1))
        row = con.execute(
            "SELECT count(*), count(DISTINCT seq), min(seq), max(seq) "
            "FROM run_rows WHERE gen_id = ?", [gid]).fetchone()
        rows, distinct, smin, smax = (row or (0, 0, None, None))
        expected_span = max(0, expect_max - expect_min + 1)
        # A gap is only a violation when nothing explains it. Two things
        # legitimately leave holes in the sequence:
        #   - RESOURCE_EXHAUSTED rejections (counted in `rejected`)
        #   - RPCs that failed for any other reason: UNAVAILABLE while the
        #     collector was down during a fault test, DEADLINE_EXCEEDED, etc.
        # Both are offered-but-never-acked. The total unacked budget is
        # offered - accepted; only gaps beyond that are unexplained. The first
        # version used `rejected` alone, and a recovery test with 2,563
        # UNAVAILABLE failures — zero loss, zero duplicates — read as FAIL.
        unacked_budget = max(0, offered - accepted)
        unaccounted = max(0, expected_span - distinct - unacked_budget)
        entry = {
            "gen_id": gid,
            "expected_seq_min": expect_min,
            "expected_seq_max": expect_max,
            "expected_span": expected_span,
            "landed_rows": rows,
            "distinct_seq": distinct,
            "observed_seq_min": smin,
            "observed_seq_max": smax,
            "missing_seq_unaccounted_for": unaccounted,
        }
        if unaccounted > 0:
            all_contiguous = False
        result["per_generator"].append(entry)

    result["checks"]["contiguity"] = {
        "pass": all_contiguous,
        "detail": "every seq accounted for as landed, rejected or failed (unacked budget = offered - accepted)"
                  if all_contiguous else
                  "sequence numbers missing that rejections do not explain",
    }

    # -- 4. watermark agreement -------------------------------------------------
    # The watermark row is committed in the SAME transaction as the file
    # registration, so its row_count is an independent witness to what the
    # catalog thinks it accepted. Disagreement with the table means the two
    # halves of that transaction diverged.
    try:
        wm = int(scalar(con,
            f"SELECT COALESCE(sum(row_count), 0) FROM "
            f"{args.catalog}.{args.schema}.{args.watermark_table} "
            f"WHERE max_timestamp >= TIMESTAMP '{args.run_start}' "
            f"  AND max_timestamp <= TIMESTAMP '{args.run_end}'"))
        # The watermark counts every row the collector committed in the window,
        # regardless of which generator produced it, so it must be compared
        # against the window total rather than this run's subset.
        result["checks"]["watermark"] = {
            "pass": wm == in_window,
            "watermark_rows": wm,
            "rows_in_window": in_window,
            "landed_this_run": landed,
            "detail": ("watermark agrees with the table" if wm == in_window
                       else f"watermark says {wm}, the window holds {in_window}"),
        }
    except Exception as exc:                                      # noqa: BLE001
        result["checks"]["watermark"] = {
            "pass": None, "detail": f"not evaluated: {str(exc).splitlines()[0][:150]}"}

    # -- 5. checksums (for before/after comparison across a merge) ---------------
    try:
        row = con.execute("""
            SELECT count(*),
                   COALESCE(sum(hash(gen_id)), 0),
                   COALESCE(sum(hash(seq)), 0),
                   COALESCE(sum(hash(body)), 0),
                   COALESCE(sum(hash(severity_text)), 0),
                   COALESCE(min(timestamp), NULL),
                   COALESCE(max(timestamp), NULL)
            FROM run_rows""").fetchone()
        result["checksums"] = {
            "rows": row[0], "gen_id": str(row[1]), "seq": str(row[2]),
            "body": str(row[3]), "severity_text": str(row[4]),
            "timestamp_min": str(row[5]), "timestamp_max": str(row[6]),
        }
    except Exception as exc:                                      # noqa: BLE001
        result["checksums"] = {"error": str(exc).splitlines()[0][:150]}

    hard = [c for c in ("row_count", "duplicates", "contiguity")
            if result["checks"][c]["pass"] is False]
    result["verdict"] = "PASS" if not hard else "FAIL"
    result["failed_checks"] = hard
    return result


def main(argv=None) -> int:
    e = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--output", help="write the full result as JSON here")
    ap.add_argument("--baseline", help="a previous result JSON to compare checksums against "
                                       "(use to prove a compaction changed nothing)")
    ap.add_argument("--pg-host", default=e("PG_HOST", ""))
    ap.add_argument("--pg-port", default=e("PG_PORT", "5432"))
    ap.add_argument("--pg-database", default=e("PG_DATABASE", "bench_catalog"))
    ap.add_argument("--pg-user", default=e("PG_USER", "analytics"))
    ap.add_argument("--pg-password", default=e("PG_PASSWORD", ""))
    ap.add_argument("--s3-bucket", default=e("S3_BUCKET", ""))
    ap.add_argument("--s3-prefix", default=e("S3_PREFIX", "bench"))
    ap.add_argument("--aws-region", default=e("AWS_REGION", "us-west-2"))
    ap.add_argument("--catalog", default=e("DUCKLAKE_CATALOG", "bench"))
    ap.add_argument("--schema", default=e("DUCKLAKE_SCHEMA", "main"))
    ap.add_argument("--table", default=e("DUCKLAKE_LOGS_TABLE", "logs"))
    ap.add_argument("--watermark-table", default=e("DUCKLAKE_WATERMARK_TABLE", "ingest_watermark"))
    # Widen the window a little: the generator stamps record timestamps at send
    # time, but clocks and the settle window mean a strict [start, end] can clip
    # the tail of a run.
    ap.add_argument("--pad-seconds", type=float, default=120.0)
    args = ap.parse_args(argv)

    with open(args.manifest) as fh:
        manifest = json.load(fh)

    import datetime as _dt
    start = float(manifest["started_at_epoch"]) - args.pad_seconds
    end = float(manifest["ended_at_epoch"]) + args.pad_seconds
    fmt = "%Y-%m-%d %H:%M:%S"
    args.run_start = _dt.datetime.fromtimestamp(start, _dt.timezone.utc).strftime(fmt)
    args.run_end = _dt.datetime.fromtimestamp(end, _dt.timezone.utc).strftime(fmt)

    con = attach(args)
    gen_ids = [g.get('gen_id') for g in (manifest.get('generators') or []) if g.get('gen_id')]
    build_view(con, args, args.run_start, args.run_end, gen_ids)
    result = validate(con, args, manifest)
    result["window_utc"] = [args.run_start, args.run_end]

    if args.baseline and os.path.exists(args.baseline):
        with open(args.baseline) as fh:
            base = json.load(fh)
        before, after = base.get("checksums") or {}, result.get("checksums") or {}
        differing = [k for k in ("rows", "gen_id", "seq", "body", "severity_text")
                     if before.get(k) != after.get(k)]
        result["checks"]["checksums_stable"] = {
            "pass": not differing,
            "differing_columns": differing,
            "detail": "all column checksums unchanged" if not differing
                      else f"changed across the merge: {', '.join(differing)}",
        }
        if differing:
            result["verdict"] = "FAIL"
            result["failed_checks"].append("checksums_stable")

    # -- report ------------------------------------------------------------------
    print(f"window            {args.run_start} .. {args.run_end} UTC")
    print(f"offered           {result['offered']:,}")
    print(f"accepted (acked)  {result['accepted']:,}")
    print(f"rejected          {result['rejected']:,}")
    print(f"landed in lake    {result['landed']:,}  (this run's generator(s))")
    if result.get("foreign_rows_in_window"):
        print(f"other rows        {result['foreign_rows_in_window']:,} in the same time window from "
              "other generators — excluded, not an error")
    if result["rows_missing_identity"]:
        print(f"!! {result['rows_missing_identity']:,} rows lack bench.gen_id/bench.seq")
    print()
    for name, check in result["checks"].items():
        state = {True: "PASS", False: "FAIL", None: "SKIP"}[check.get("pass")]
        print(f"  {state}  {name:<18} {check.get('detail', '')}")
    print()
    for g in result["per_generator"]:
        print(f"  gen {g['gen_id'][:8]}  expected seq {g['expected_seq_min']}..{g['expected_seq_max']} "
              f"({g['expected_span']:,})  landed {g['landed_rows']:,}  distinct {g['distinct_seq']:,}"
              + (f"  UNACCOUNTED {g['missing_seq_unaccounted_for']:,}"
                 if g["missing_seq_unaccounted_for"] else ""))
    print()
    print(f"VERDICT {result['verdict']}")

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(result, fh, indent=2, default=str)

    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
