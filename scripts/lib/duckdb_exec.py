#!/usr/bin/env python3
"""Run SQL through the python duckdb package and print results.

Why not the `duckdb` CLI: the CLI installed on these hosts is 1.5.2 while the
server's JDBC driver is 1.5.4.0. DuckLake's catalog metadata schema is version
dependent, so reading the catalog with a different DuckDB than the one that
wrote it is exactly the sort of thing that produces a confidently wrong number.
`pip install duckdb==1.5.4` in .venv matches the server; use it.

    duckdb_exec.py --file q.sql
    duckdb_exec.py --sql "SELECT 1" --format json
    echo "SELECT 1" | duckdb_exec.py --format csv

--format table|json|csv|none. Secrets in the SQL are redacted from errors.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys

try:
    import duckdb
except ImportError:
    sys.exit("duckdb is not installed — run scripts/setup.sh")

_SECRET_RE = re.compile(r"(password=)\S+")


def redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1***", text)


def split_statements(sql: str) -> list[str]:
    """Split on ';' outside of single quotes.

    DuckLake ATTACH strings embed a whole libpq conninfo — semicolons inside
    those quotes must not be treated as statement terminators.
    """
    out, buf, in_quote = [], [], False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            # '' inside a quoted string is an escaped quote, not a terminator.
            if in_quote and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_quote = not in_quote
            buf.append(ch)
        elif ch == ";" and not in_quote:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def emit(rows, columns, fmt: str) -> None:
    if fmt == "none" or not columns:
        return
    if fmt == "json":
        print(json.dumps([dict(zip(columns, r)) for r in rows], indent=2, default=str))
        return
    if fmt == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(columns)
        w.writerows(rows)
        return
    buf = io.StringIO()
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) if rows else len(str(c))
              for i, c in enumerate(columns)]
    buf.write("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(columns)) + "\n")
    buf.write("  ".join("-" * w for w in widths) + "\n")
    for r in rows:
        buf.write("  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)) + "\n")
    sys.stdout.write(buf.getvalue())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file")
    src.add_argument("--sql")
    ap.add_argument("--format", default="table", choices=["table", "json", "csv", "none"])
    ap.add_argument("--database", default=":memory:")
    ap.add_argument("--last-only", action="store_true",
                    help="print only the final statement's result set")
    args = ap.parse_args()

    if args.file:
        sql = open(args.file).read()
    elif args.sql:
        sql = args.sql
    else:
        sql = sys.stdin.read()

    statements = split_statements(sql)
    if not statements:
        return 0

    con = duckdb.connect(args.database)
    try:
        for idx, stmt in enumerate(statements):
            last = idx == len(statements) - 1
            try:
                cur = con.execute(stmt)
            except Exception as exc:                              # noqa: BLE001
                sys.stderr.write(f"statement {idx + 1} failed: {redact(str(exc))}\n")
                sys.stderr.write(f"  sql: {redact(stmt)[:300]}\n")
                return 1
            if args.last_only and not last:
                continue
            if cur.description:
                emit(cur.fetchall(), [d[0] for d in cur.description], args.format)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
