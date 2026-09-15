#!/usr/bin/env python3
"""Turn the compactor's log into a compaction time series.

The compactor is built with a LoggingMeterRegistry, so its Micrometer meters are
never exposed to a scraper — they are printed. That makes the log the only
source for merge durations and file-count gauges, and makes this parser part of
the measurement path rather than a convenience.

Lines it understands (format verified against a running compactor,
dazzleduck-sql-ducklake-compactor 0.2.17, slf4j-simple 2.0.16):

    ...ducklake.compaction.duration{database=bench,step=merge,type=minor} \\
        throughput=0.016667/s mean=0.024414737s max=0.027184873s
    ...ducklake.compaction.minor{database=bench} throughput=0.016667/s
    ...ducklake.files.small{database=bench} value=0
    [compaction] INFO ...CompactionService - Minor compaction completed for bench
    [housekeeping] INFO ...CompactionService - Housekeeping completed for bench

A timestamp is only present when simplelogger.properties sets showDateTime —
see compactor/config/simplelogger.properties. Without it every event is emitted
with "ts": null and a `no_timestamp` flag, so a caller can tell "the compactor
did not timestamp this" apart from "this happened at epoch 0".
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

# Optional leading ISO-8601 timestamp (present once showDateTime is on).
TS = r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[.,]\d+(?:Z|[+-]\d{2}:?\d{2})?)?\s*"

METER = re.compile(
    TS + r".*?(?P<name>ducklake\.[a-z_.]+)"
         r"\{(?P<tags>[^}]*)\}\s+(?P<fields>.*)$")

EVENT = re.compile(
    TS + r".*?CompactionService\s+-\s+(?P<msg>(?:Minor|Major) compaction completed for "
         r"(?P<db1>\S+)|Housekeeping completed for (?P<db2>\S+))")

FIELD = re.compile(r"(?P<key>\w+)=(?P<num>[0-9.eE+-]+)(?P<unit>/s|s|ms)?")


def parse_ts(raw: str | None):
    if not raw:
        return None
    text = raw.replace(",", ".")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # +0000 -> +00:00
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_tags(raw: str) -> dict:
    out = {}
    for part in raw.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def parse_fields(raw: str) -> dict:
    out = {}
    for m in FIELD.finditer(raw):
        key, num, unit = m.group("key"), m.group("num"), m.group("unit")
        try:
            value = float(num)
        except ValueError:
            continue
        # Normalise every duration to milliseconds. Mixing 0.0244s and 24.4ms in
        # one column is how a report ends up off by 1000x.
        if unit == "s":
            out[f"{key}_ms"] = value * 1000.0
        elif unit == "ms":
            out[f"{key}_ms"] = value
        elif unit == "/s":
            out[f"{key}_per_s"] = value
        else:
            out[key] = value
    return out


def parse_line(line: str) -> dict | None:
    m = METER.match(line)
    if m:
        tags = parse_tags(m.group("tags"))
        rec = {
            "kind": "meter",
            "ts": parse_ts(m.group("ts")),
            "metric": m.group("name"),
            "database": tags.get("database"),
            "type": tags.get("type"),
            "step": tags.get("step"),
        }
        rec.update(parse_fields(m.group("fields")))
        if rec["ts"] is None:
            rec["no_timestamp"] = True
        return rec

    m = EVENT.match(line)
    if m:
        msg = m.group("msg")
        rec = {
            "kind": "event",
            "ts": parse_ts(m.group("ts")),
            "event": msg,
            "database": m.group("db1") or m.group("db2"),
            "type": ("minor" if msg.startswith("Minor")
                     else "major" if msg.startswith("Major")
                     else "housekeeping"),
        }
        if rec["ts"] is None:
            rec["no_timestamp"] = True
        return rec
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--summary", action="store_true", help="also print a summary to stderr")
    args = ap.parse_args(argv)

    records, untimed = [], 0
    with open(args.input, errors="replace") as fh:
        for line in fh:
            rec = parse_line(line.rstrip("\n"))
            if rec:
                records.append(rec)
                untimed += 1 if rec.get("no_timestamp") else 0

    with open(args.output, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")

    if args.summary or untimed:
        counts: dict = {}
        for r in records:
            if r["kind"] == "event":
                counts[r["type"]] = counts.get(r["type"], 0) + 1
        sys.stderr.write(f"parsed {len(records)} records; events: {counts or 'none'}\n")
        if untimed:
            sys.stderr.write(
                f"WARNING: {untimed} records had no timestamp. The compactor's "
                "slf4j-simple is not configured with showDateTime, so these "
                "cannot be placed on a timeline. Redeploy with "
                "compactor/config/simplelogger.properties.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
