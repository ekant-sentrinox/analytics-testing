#!/usr/bin/env python3
"""Generate the Grafana dashboard JSON in monitoring/grafana/dashboards/.

The JSON files are what Grafana provisioning loads; this script is their source.
Hand-editing 2000 lines of generated JSON across seven dashboards is how panels
drift apart — same metric, three different units, two different colour scales.
Edit here and re-run:

    python3 monitoring/grafana/build_dashboards.py

Panels that have no data source are still included, with a panel description
saying why, rather than being silently omitted. A dashboard with a visible
"this cannot be measured yet" panel is more honest than one that looks complete.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "dashboards")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

# One palette across every dashboard. Muted, distinguishable in both themes,
# and safe for the common forms of colour-blindness.
COLORS = {
    "offered":  "#8899a6",
    "accepted": "#2f7d95",
    "rejected": "#b4553f",
    "p50":      "#8fae9b",
    "p95":      "#c39b4e",
    "p99":      "#b4553f",
    "generator": "#7a8fa6",
    "collector": "#2f7d95",
    "compactor": "#8a6fa8",
    "neutral":  "#6b7d8f",
    "good":     "#4f8a6a",
    "warn":     "#c39b4e",
    "bad":      "#b4553f",
}


def target(expr: str, legend: str, ref="A") -> dict:
    return {"datasource": DS, "expr": expr, "legendFormat": legend,
            "refId": ref, "editorMode": "code", "range": True}


def overrides(mapping: dict) -> list:
    return [{"matcher": {"id": "byName", "options": name},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}]}
            for name, colour in mapping.items()]


def timeseries(title, exprs, unit="short", w=12, h=8, x=0, y=0, desc="",
               colours=None, stack=False, fill=8, minimum=None, axis_soft_max=None):
    return {
        "type": "timeseries", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [target(e, l, chr(65 + i)) for i, (e, l) in enumerate(exprs)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": minimum,
                "custom": {
                    "drawStyle": "line", "lineWidth": 1.5,
                    "fillOpacity": fill if stack else 6,
                    "showPoints": "never", "spanNulls": False,
                    "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                    "axisSoftMax": axis_soft_max,
                    "gradientMode": "opacity",
                },
                "color": {"mode": "palette-classic"},
            },
            "overrides": overrides(colours or {}),
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "calcs": ["mean", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(title, expr, unit="short", w=4, h=4, x=0, y=0, desc="", thresholds=None,
         decimals=None, text_mode="auto"):
    steps = [{"color": COLORS["neutral"], "value": None}]
    for value, colour in (thresholds or []):
        steps.append({"color": colour, "value": value})
    return {
        "type": "stat", "title": title, "description": desc,
        "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [target(expr, title)],
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": decimals,
            "thresholds": {"mode": "absolute", "steps": steps},
            "color": {"mode": "thresholds"},
        }, "overrides": []},
        "options": {"colorMode": "value", "graphMode": "area",
                    "textMode": text_mode, "reduceOptions":
                        {"calcs": ["lastNonNull"], "fields": "", "values": False}},
    }


def text_panel(title, content, w=24, h=4, x=0, y=0):
    return {"type": "text", "title": title, "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "options": {"mode": "markdown", "content": content}}


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "collapsed": False, "panels": []}


def dashboard(uid, title, description, panels, tags=None, refresh="10s") -> dict:
    return {
        "uid": uid, "title": title, "description": description,
        "tags": ["benchmark"] + (tags or []),
        "timezone": "utc", "editable": True, "schemaVersion": 39,
        "refresh": refresh,
        "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": [{
            "name": "DS_PROMETHEUS", "label": "Datasource", "type": "datasource",
            "query": "prometheus", "current": {"text": "Prometheus", "value": "Prometheus"},
            "hide": 0,
        }]},
        "panels": panels,
        "annotations": {"list": [{
            "name": "Alerts", "enable": True, "iconColor": COLORS["bad"],
            "datasource": {"type": "datasource", "uid": "grafana"},
            "target": {"type": "dashboard", "limit": 100, "matchAny": True},
        }]},
    }


# ---------------------------------------------------------------------------
# 1. end-to-end pipeline
# ---------------------------------------------------------------------------
def d_pipeline():
    p = [
        text_panel("How to read this", (
            "**offered** is what the generator attempted. **accepted** is what the collector "
            "acked — and because the export RPC does not return until the batch is durable, "
            "accepted is the only one of the two that means anything about capacity.\n\n"
            "A rate counts as **sustainable** only if accepted tracks offered *and* the "
            "uncompacted file backlog is flat. Either one alone will mislead you."
        ), h=4, y=0),

        row("Rates", 4),
        stat("Accepted rec/s", "bench_generator_accepted_rps", "reqps", 4, 4, 0, 5,
             desc="Records acked by the collector in the last generator sample."),
        stat("Offered rec/s", "bench_generator_offered_rps", "reqps", 4, 4, 4, 5,
             desc="What the generator attempted. NOT throughput."),
        stat("Rejected rec/s", "bench_generator_rejected_rps", "reqps", 4, 4, 8, 5,
             desc="RESOURCE_EXHAUSTED — explicit backpressure from the collector.",
             thresholds=[(1, COLORS["bad"])]),
        stat("Accepted / offered", "bench:accepted_over_offered", "percentunit", 4, 4, 12, 5,
             desc="Must be >= 0.99 for a level to count as sustainable.",
             decimals=4, thresholds=[(0.99, COLORS["good"])]),
        stat("Visibility lag", "bench_watermark_lag_seconds", "s", 4, 4, 16, 5,
             desc="B3: now() - newest committed watermark. How stale the lake is.",
             thresholds=[(60, COLORS["warn"]), (300, COLORS["bad"])]),
        stat("Backlog files", "bench_backlog_files", "short", 4, 4, 20, 5,
             desc="B2: live data files registered in the catalog."),

        timeseries("Offered vs accepted vs rejected", [
            ("bench_generator_offered_rps", "offered"),
            ("bench_generator_accepted_rps", "accepted"),
            ("bench_generator_rejected_rps", "rejected"),
        ], "reqps", 24, 8, 0, 9,
            desc="The single most important panel. A widening gap between offered and "
                 "accepted is the pipeline failing to keep up.",
            colours={"offered": COLORS["offered"], "accepted": COLORS["accepted"],
                     "rejected": COLORS["rejected"]},
            minimum=0),

        row("Backlog — three different quantities, never averaged", 17),
        timeseries("B2 — uncompacted file backlog", [
            ("bench_backlog_files", "live files"),
            ("bench_backlog_small_files", "below minor threshold"),
        ], "short", 12, 8, 0, 18,
            desc="What compaction drains. A rising trend in steady state means this rate "
                 "is not sustainable, regardless of the accept rate.",
            colours={"live files": COLORS["collector"],
                     "below minor threshold": COLORS["rejected"]}, minimum=0),
        timeseries("B2 slope (files/min)", [
            ("bench:backlog_files_slope_per_min", "slope"),
        ], "short", 12, 8, 12, 18,
            desc="Positive = falling behind. This is the quantity the sustainability "
                 "criterion is actually about.",
            colours={"slope": COLORS["warn"]}),
        timeseries("B3 — end-to-end visibility lag", [
            ("bench_watermark_lag_seconds", "lag"),
        ], "s", 12, 7, 0, 26,
            desc="From the watermark table, which is committed in the same transaction as "
                 "the file registration.",
            colours={"lag": COLORS["compactor"]}, minimum=0),
        text_panel("B1 — collector queue depth", (
            "**Not available.** `writer.pending_batches` and `writer.pending_buckets` are "
            "registered in the collector's `SimpleMeterRegistry`, which has no exporter, and "
            "`/health` publishes only `batchesProcessed`.\n\n"
            "Closing this needs a `PrometheusMeterRegistry` in the collector — see "
            "`IMPROVEMENTS.md`. Until then, `rejected rec/s` above is the only visible "
            "signal that the in-memory queue is saturating, and by then it is already full."
        ), 12, 7, 12, 26),

        row("Storage and catalog", 33),
        timeseries("S3 bytes under the test prefix", [
            ("bench_s3_bytes", "bytes"),
        ], "bytes", 8, 7, 0, 34, colours={"bytes": COLORS["accepted"]}, minimum=0),
        timeseries("Mean live file size", [
            ("bench:mean_live_file_bytes", "mean file size"),
        ], "bytes", 8, 7, 8, 34,
            desc="Set by the collector's min_bucket_size and max_delay_ms. This is what "
                 "decides how much work compaction has to do.",
            colours={"mean file size": COLORS["compactor"]}, minimum=0),
        timeseries("Catalog rows and snapshots", [
            ("bench_backlog_rows", "rows in live files"),
            ("bench_catalog_snapshots", "snapshots"),
        ], "short", 8, 7, 16, 34, minimum=0),
    ]
    return dashboard("bench-pipeline", "Pipeline — end to end",
                     "Generator -> collector -> S3/catalog -> compactor, on one screen.",
                     p, ["pipeline"])


# ---------------------------------------------------------------------------
# 2. generator
# ---------------------------------------------------------------------------
def d_generator():
    p = [
        row("Throughput", 0),
        stat("Records offered", "bench_generator_records_offered_total", "short", 4, 4, 0, 1),
        stat("Records accepted", "bench_generator_records_accepted_total", "short", 4, 4, 4, 1),
        stat("RPCs sent", "sum(bench_generator_requests_total)", "short", 4, 4, 8, 1),
        stat("In flight", "bench_generator_inflight", "short", 4, 4, 12, 1,
             desc="Outstanding export RPCs. Pinned at max_inflight means the generator is "
                  "the thing being measured, not the pipeline."),
        stat("Stalled", "rate(bench_generator_stalled_ms_total[1m])", "ms", 4, 4, 16, 1,
             desc="Milliseconds per second the pacer spent blocked on the inflight bound. "
                  "Any sustained value means the offered rate was not actually offered.",
             thresholds=[(100, COLORS["warn"]), (500, COLORS["bad"])]),
        stat("Bytes sent", "bench_generator_bytes_sent_total", "bytes", 4, 4, 20, 1),

        timeseries("Rates", [
            ("bench_generator_offered_rps", "offered"),
            ("bench_generator_accepted_rps", "accepted"),
            ("bench_generator_rejected_rps", "rejected"),
        ], "reqps", 12, 8, 0, 5,
            colours={"offered": COLORS["offered"], "accepted": COLORS["accepted"],
                     "rejected": COLORS["rejected"]}, minimum=0),
        timeseries("In-flight and stalls", [
            ("bench_generator_inflight", "in flight"),
            ("rate(bench_generator_stalled_ms_total[1m])", "stalled ms/s"),
        ], "short", 12, 8, 12, 5,
            desc="The open loop's one honest limit. When in-flight saturates, the pacer "
                 "cannot submit on schedule and the deficit is recorded here rather than "
                 "hidden by slowing down.",
            colours={"in flight": COLORS["collector"], "stalled ms/s": COLORS["warn"]},
            minimum=0),

        row("Latency", 13),
        timeseries("Export RPC latency percentiles", [
            ("histogram_quantile(0.50, sum(rate(bench_generator_export_latency_seconds_bucket[1m])) by (le))", "p50"),
            ("histogram_quantile(0.95, sum(rate(bench_generator_export_latency_seconds_bucket[1m])) by (le))", "p95"),
            ("histogram_quantile(0.99, sum(rate(bench_generator_export_latency_seconds_bucket[1m])) by (le))", "p99"),
        ], "s", 16, 8, 0, 14,
            desc="Includes queue wait, the DuckDB COPY to Parquet and the catalog commit — "
                 "the RPC does not ack until the batch is durable. This is persistence "
                 "latency, not network time.",
            colours={"p50": COLORS["p50"], "p95": COLORS["p95"], "p99": COLORS["p99"]},
            minimum=0),
        timeseries("Mean latency", [
            ("rate(bench_generator_export_latency_seconds_sum[1m]) / "
             "clamp_min(rate(bench_generator_export_latency_seconds_count[1m]), 0.001)", "mean"),
        ], "s", 8, 8, 16, 14, colours={"mean": COLORS["neutral"]}, minimum=0),

        row("Errors", 22),
        timeseries("Errors by gRPC status", [
            ("sum by (code) (rate(bench_generator_errors_total[1m]))", "{{code}}"),
        ], "reqps", 12, 8, 0, 23,
            desc="RESOURCE_EXHAUSTED is backpressure, not a bug — but it disqualifies the "
                 "level. DEADLINE_EXCEEDED usually means a slow flush, and those records may "
                 "still have landed.",
            stack=True, minimum=0),
        timeseries("Error rate", [("bench:error_rate", "error rate")], "percentunit",
                   12, 8, 12, 23, colours={"error rate": COLORS["bad"]}, minimum=0),
    ]
    return dashboard("bench-generator", "Generator (SERVER 1)",
                     "Open-loop OTLP load generator: rates, latency, in-flight, errors.",
                     p, ["generator"])


# ---------------------------------------------------------------------------
# 3. collector
# ---------------------------------------------------------------------------
def d_collector():
    p = [
        text_panel("Collector observability is limited by design of the build", (
            "The collector is constructed with a `SimpleMeterRegistry`. Its Micrometer "
            "meters — `export.records`, `export.latency`, `writer.data_phase_ms`, "
            "`writer.post_ingest_phase_ms`, `writer.pending_batches` — exist in process "
            "memory and are **never exported anywhere**.\n\n"
            "What is shown below is everything actually observable: the `/health` endpoint "
            "(status, uptime, queue count, `batchesProcessed`), what the generator sees from "
            "the client side, and the effect on the catalog. The single most useful "
            "diagnostic in the whole pipeline — `data_phase_ms` vs `post_ingest_phase_ms`, "
            "which answers *is the bottleneck DuckDB or the catalog?* — is not reachable. "
            "See `IMPROVEMENTS.md`."
        ), h=5, y=0),

        row("Liveness", 5),
        stat("Collector", "bench_collector_up", "short", 4, 4, 0, 6,
             thresholds=[(1, COLORS["good"])], text_mode="value"),
        stat("Uptime", "bench_collector_uptime_seconds", "s", 4, 4, 4, 6,
             desc="A reset means a restart. Restarts during a measured level invalidate it."),
        stat("Known queues", "bench_collector_known_queues", "short", 4, 4, 8, 6),
        stat("Batches processed", "bench_collector_batches_processed", "short", 4, 4, 12, 6,
             desc="The only throughput-ish counter /health exposes. Batches, not records."),
        stat("Batches/s", "rate(bench_collector_batches_processed[1m])", "short", 4, 4, 16, 6,
             desc="Derived. Multiply by the flush size to sanity-check against accepted rec/s."),
        stat("Scrape OK", 'bench_scrape_ok{source="collector"}', "short", 4, 4, 20, 6,
             thresholds=[(1, COLORS["good"])], text_mode="value"),

        timeseries("Batches processed (rate)", [
            ("rate(bench_collector_batches_processed[1m])", "batches/s"),
        ], "short", 12, 8, 0, 10, colours={"batches/s": COLORS["collector"]}, minimum=0),
        timeseries("Client-observed accept rate and latency", [
            ("bench_generator_accepted_rps", "accepted rec/s"),
            ("histogram_quantile(0.99, sum(rate(bench_generator_export_latency_seconds_bucket[1m])) by (le)) * 1000", "p99 ms"),
        ], "short", 12, 8, 12, 10,
            desc="From the generator, because the collector does not publish its own. "
                 "Latency here is the collector's full write path.",
            colours={"accepted rec/s": COLORS["accepted"], "p99 ms": COLORS["p99"]}),

        row("Effect on the lake", 18),
        timeseries("Files and bytes written into the catalog", [
            ("bench_backlog_files", "live files"),
            ("rate(bench_backlog_bytes[1m])", "bytes/s added"),
        ], "short", 12, 8, 0, 19, minimum=0),
        timeseries("PostgreSQL catalog activity", [
            ("bench_pg_connections", "connections"),
            ("bench_pg_active_queries", "active queries"),
            ("rate(bench_pg_xact_commit_total[1m])", "commits/s"),
        ], "short", 12, 8, 12, 19,
            desc="Every flush ends in a catalog commit. If commits/s flattens while accepted "
                 "rec/s keeps climbing, the catalog is becoming the constraint.",
            minimum=0),
    ]
    return dashboard("bench-collector", "Collector (SERVER 2)",
                     "What can actually be observed of the OTLP collector, and what cannot.",
                     p, ["collector"])


# ---------------------------------------------------------------------------
# 4. compactor
# ---------------------------------------------------------------------------
def d_compactor():
    p = [
        text_panel("Scheduler behaviour worth knowing before reading these numbers", (
            "In `CompactionService.runCompaction`, a **major run replaces that tick's minor "
            "run** — it does not run alongside it. A single compactor therefore never "
            "produces concurrent minor and major activity, and any claim of \"concurrent "
            "compaction\" from a single-instance run is wrong. See `TEST_SCENARIOS.md`.\n\n"
            "The compactor uses a `LoggingMeterRegistry`, so merge *durations* are log lines, "
            "not metrics. They are parsed out into `raw/compaction.jsonl` at collection time "
            "and appear in the generated report, not here."
        ), h=5, y=0),

        row("Activity", 5),
        stat("Compactor", "bench_compactor_up", "short", 4, 4, 0, 6,
             thresholds=[(1, COLORS["good"])], text_mode="value"),
        stat("Uptime", "bench_compactor_uptime_seconds", "s", 4, 4, 4, 6),
        stat("Minor runs", "sum(bench_compaction_minor_total)", "short", 4, 4, 8, 6),
        stat("Major runs", "sum(bench_compaction_major_total)", "short", 4, 4, 12, 6),
        stat("Files merged", "sum(bench_compaction_files_compacted_total)", "short", 4, 4, 16, 6,
             desc="Cumulative. Flat while small files accumulate means compaction is running "
                  "but finding nothing to do — check the threshold against actual file sizes."),
        stat("Small files now", "sum(bench_compactor_files_small)", "short", 4, 4, 20, 6,
             thresholds=[(100, COLORS["warn"]), (1000, COLORS["bad"])]),

        timeseries("Compaction runs (cumulative)", [
            ("bench_compaction_minor_total", "minor {{database}}"),
            ("bench_compaction_major_total", "major {{database}}"),
        ], "short", 12, 8, 0, 10, minimum=0),
        timeseries("Files merged (rate)", [
            ("rate(bench_compaction_files_compacted_total[5m]) * 60", "files/min {{database}}"),
        ], "short", 12, 8, 12, 10,
            desc="The drain rate. Compare against how fast the collector creates files: if "
                 "this is lower for a sustained period, the backlog grows without bound.",
            colours={"files/min bench": COLORS["compactor"]}, minimum=0),

        row("File-size distribution — what compaction is fixing", 18),
        timeseries("File counts by size class", [
            ("bench_compactor_files_small", "small (< minor threshold)"),
            ("bench_compactor_files_medium", "medium"),
            ("bench_compactor_files_total", "total"),
        ], "short", 12, 8, 0, 19,
            colours={"small (< minor threshold)": COLORS["rejected"],
                     "medium": COLORS["warn"], "total": COLORS["neutral"]},
            minimum=0),
        timeseries("Mean live file size", [
            ("bench:mean_live_file_bytes", "mean"),
        ], "bytes", 12, 8, 12, 19,
            desc="Should trend up while compaction runs and there is a backlog to merge. "
                 "Flat and small means merges are not happening or not helping.",
            colours={"mean": COLORS["compactor"]}, minimum=0),

        row("Housekeeping", 27),
        timeseries("Snapshots retained", [
            ("bench_catalog_snapshots", "snapshots"),
        ], "short", 12, 7, 0, 28,
            desc="expire_snapshots trims these to snapshot_retention. Unbounded growth means "
                 "housekeeping is not running.",
            colours={"snapshots": COLORS["neutral"]}, minimum=0),
        timeseries("S3 objects under the prefix", [
            ("bench_s3_objects", "objects"),
        ], "short", 12, 7, 12, 28,
            desc="Should fall after cleanup_old_files reclaims files superseded by a merge. "
                 "Objects far above live files = orphans awaiting cleanup.",
            colours={"objects": COLORS["accepted"]}, minimum=0),
    ]
    return dashboard("bench-compactor", "Compactor (SERVER 3)",
                     "DuckLake merge activity, file-size distribution and housekeeping.",
                     p, ["compactor"])


# ---------------------------------------------------------------------------
# 5. infrastructure
# ---------------------------------------------------------------------------
def d_infra():
    """All three hosts, sourced from the host-local agents.

    Uses bench_host_metric{role,metric} rather than node_exporter for SERVER 2
    and SERVER 3. Scraping node_exporter there would need an inbound rule on
    9100 per host; the agent already collects the same things at 1 s and its
    snapshot is pulled over SSH. See MONITORING_ARCHITECTURE.md.
    """
    def hm(metric, legend):
        return (f'bench_host_metric{{metric="{metric}"}}', legend)

    p = [
        text_panel("Source and why there is no node_exporter here", (
            "CPU, memory, disk and network for **all three** hosts come from each machine's "
            "`bench-agent`, republished by the pipeline exporter as "
            "`bench_host_metric{role=...,metric=...}`.\n\n"
            "There is deliberately no `node_exporter` on SERVER 2 or SERVER 3: that would "
            "require opening port 9100 inbound on each, and the agent already collects the "
            "same data at **1 s** — finer than this 15 s scrape. The full-resolution series "
            "lands in `results/<test-id>/raw/host-*.jsonl`, which is what the generated "
            "report actually reads.\n\n"
            "All three are burstable **t3.large**. CPU credit exhaustion throttles the vCPU "
            "and is indistinguishable from a software regression on these graphs — check "
            "`CPUCreditBalance` before concluding anything from a late-run slowdown."
        ), h=5, y=0),

        row("CPU", 5),
        timeseries("CPU utilisation", [hm("cpu_percent", "{{role}}")],
                   "percent", 12, 8, 0, 6,
                   colours={"generator": COLORS["generator"], "collector": COLORS["collector"],
                            "compactor": COLORS["compactor"]},
                   minimum=0, axis_soft_max=100),
        timeseries("Watched process CPU", [("bench_process_cpu_percent", "{{role}}")],
                   "percent", 12, 8, 12, 6,
                   desc="The JVM itself, separated from total host CPU — the gap is "
                        "everything else on the box, including this monitoring.",
                   minimum=0),

        row("Memory", 14),
        timeseries("Memory used", [hm("mem_used_bytes", "{{role}}")],
                   "bytes", 8, 8, 0, 15,
                   desc="No swap on these hosts, so memory pressure becomes an OOM kill "
                        "rather than a slowdown.",
                   minimum=0),
        timeseries("Memory available", [hm("mem_available_bytes", "{{role}}")],
                   "bytes", 8, 8, 8, 15, minimum=0),
        timeseries("Process RSS", [("bench_process_rss_bytes", "{{role}}")],
                   "bytes", 8, 8, 16, 15,
                   desc="A slow monotonic rise here across a soak is the leak this test "
                        "exists to catch.",
                   minimum=0),

        row("Disk", 23),
        timeseries("Disk throughput", [
            hm("disk_read_bytes_per_s", "read {{role}}"),
            hm("disk_write_bytes_per_s", "write {{role}}"),
        ], "Bps", 12, 8, 0, 24, minimum=0),
        timeseries("Root filesystem free", [hm("root_free_bytes", "{{role}}")],
                   "bytes", 12, 8, 12, 24,
                   desc="8 GiB root volumes. Filling one is a realistic failure mode for a "
                        "long run, between DuckDB spill, Parquet staging and 1 s JSONL.",
                   minimum=0),

        row("Network", 32),
        timeseries("Network throughput", [
            hm("net_rx_bytes_per_s", "rx {{role}}"),
            hm("net_tx_bytes_per_s", "tx {{role}}"),
        ], "Bps", 24, 8, 0, 33, minimum=0),

        row("Agent liveness", 41),
        timeseries("Snapshot age", [("bench_agent_snapshot_age_seconds", "{{role}}")],
                   "s", 12, 7, 0, 42,
                   desc="Seconds since each agent last wrote its state. A dead agent leaves "
                        "a snapshot that reads healthy forever; age is what distinguishes "
                        "fresh from frozen. Over 90 s is treated as a scrape failure.",
                   colours={"generator": COLORS["generator"], "collector": COLORS["collector"],
                            "compactor": COLORS["compactor"]},
                   minimum=0),
        timeseries("Service restarts and OOM kills", [
            ("bench_service_restarts_total", "restarts {{role}}"),
            ("bench_oom_kills_total", "oom {{role}}"),
        ], "short", 12, 7, 12, 42,
            desc="Both are results, not diagnostics: a restart during a measured level "
                 "invalidates that level.",
            colours={"restarts collector": COLORS["warn"], "oom collector": COLORS["bad"]},
            minimum=0),
    ]
    return dashboard("bench-infra", "Infrastructure (all servers)",
                     "CPU, memory, disk and network for all three hosts, from the "
                     "host-local agents — no inbound monitoring ports required.",
                     p, ["infra"])


# ---------------------------------------------------------------------------
# 6. errors and health
# ---------------------------------------------------------------------------
def d_errors():
    p = [
        row("Component health", 0),
        stat("Collector up", "bench_collector_up", "short", 6, 4, 0, 1,
             desc="From the loopback health check run by bench-agent ON SERVER 2, "
                  "pulled here over SSH. No inbound port involved.",
             thresholds=[(1, COLORS["good"])], text_mode="value"),
        stat("Compactor up", "bench_compactor_up", "short", 6, 4, 6, 1,
             desc="Same, from bench-agent on SERVER 3.",
             thresholds=[(1, COLORS["good"])], text_mode="value"),
        stat("Failing scrapes", "count(bench_scrape_ok == 0) or vector(0)", "short", 6, 4, 12, 1,
             desc="Each one is a blind spot in the measurements.",
             thresholds=[(1, COLORS["bad"])]),
        stat("Error rate", "bench:error_rate", "percentunit", 6, 4, 18, 1,
             thresholds=[(0.001, COLORS["warn"]), (0.01, COLORS["bad"])], decimals=4),

        row("Ingestion errors", 5),
        timeseries("Errors by gRPC status code", [
            ("sum by (code) (rate(bench_generator_errors_total[1m]))", "{{code}}"),
        ], "reqps", 24, 8, 0, 6, stack=True, minimum=0,
            desc="RESOURCE_EXHAUSTED = the collector explicitly refusing work because pending "
                 "write bytes exceeded max_pending_write. UNAVAILABLE = the collector is gone. "
                 "UNAUTHENTICATED = the JWT secret does not match the deployed collector."),

        row("Scrape health", 14),
        timeseries("Scrape success by source", [
            ("bench_scrape_ok", "{{source}}"),
        ], "short", 12, 7, 0, 15, minimum=0,
            desc="0 for collector or compactor almost always means the security group is "
                 "dropping 8080/8081, not that the service is down."),
        timeseries("Scrape duration", [
            ("bench_scrape_duration_seconds", "{{source}}"),
        ], "s", 12, 7, 12, 15, minimum=0,
            desc="The S3 listing is the expensive one; if it climbs, raise s3_interval before "
                 "assuming the pipeline slowed down."),

        row("Dependency reachability — measured FROM each host", 22),
        timeseries("S3 and RDS reachable", [
            ("bench_dependency_up", "{{role}} -> {{dependency}}"),
        ], "short", 12, 7, 0, 23, minimum=0,
            desc="Each host checks its own dependencies. S3 being reachable from the "
                 "control node says nothing about whether the collector can write; this "
                 "is measured where it matters."),
        timeseries("Dependency latency", [
            ("bench_dependency_latency_ms", "{{role}} -> {{dependency}}"),
        ], "ms", 12, 7, 12, 23, minimum=0,
            desc="RDS is a TCP connect (single-digit ms on this VPC); S3 is an HTTPS HEAD "
                 "(~120 ms). A sustained rise in either precedes write failures."),

        row("Catalog", 30),
        timeseries("Rollbacks and deadlocks", [
            ("rate(bench_pg_xact_rollback_total[5m])", "rollbacks/s"),
            ("rate(bench_pg_deadlocks_total[5m])", "deadlocks/s"),
        ], "short", 12, 7, 0, 23, minimum=0,
            desc="Two writers share this catalog. Rollbacks here are the visible edge of "
                 "concurrent-writer conflicts between the collector and the compactor.",
            colours={"rollbacks/s": COLORS["warn"], "deadlocks/s": COLORS["bad"]}),
        timeseries("Longest running query", [
            ("bench_pg_longest_query_seconds", "seconds"),
        ], "s", 12, 7, 12, 23, minimum=0,
            colours={"seconds": COLORS["warn"]}),
    ]
    return dashboard("bench-errors", "Errors and health",
                     "Everything that went wrong, and everywhere the measurements are blind.",
                     p, ["errors"])


# ---------------------------------------------------------------------------
# 7. storage and catalog
# ---------------------------------------------------------------------------
def d_storage():
    p = [
        row("S3", 0),
        stat("Objects", "bench_s3_objects", "short", 6, 4, 0, 1),
        stat("Bytes", "bench_s3_bytes", "bytes", 6, 4, 6, 1),
        stat("Growth", "rate(bench_s3_bytes[5m])", "Bps", 6, 4, 12, 1),
        stat("Mean file size", "bench:mean_live_file_bytes", "bytes", 6, 4, 18, 1),

        timeseries("S3 data growth", [
            ("bench_s3_bytes", "bytes under prefix"),
        ], "bytes", 12, 8, 0, 5, colours={"bytes under prefix": COLORS["accepted"]}, minimum=0),
        timeseries("Objects vs live files", [
            ("bench_s3_objects", "S3 objects"),
            ("bench_backlog_files", "live files in catalog"),
        ], "short", 12, 8, 12, 5,
            desc="Objects well above live files means files superseded by a merge have not "
                 "been reclaimed yet. cleanup_old_files does that on the housekeeping timer.",
            colours={"S3 objects": COLORS["accepted"],
                     "live files in catalog": COLORS["collector"]}, minimum=0),

        row("PostgreSQL catalog", 13),
        stat("DB size", "bench_pg_database_size_bytes", "bytes", 6, 4, 0, 14),
        stat("Connections", "bench_pg_connections", "short", 6, 4, 6, 14,
             thresholds=[(40, COLORS["warn"]), (80, COLORS["bad"])]),
        stat("Snapshots", "bench_catalog_snapshots", "short", 6, 4, 12, 14),
        stat("Commits/s", "rate(bench_pg_xact_commit_total[1m])", "short", 6, 4, 18, 14),

        timeseries("Catalog database size", [
            ("bench_pg_database_size_bytes", "bytes"),
        ], "bytes", 12, 8, 0, 18,
            desc="Grows with every registered file and every snapshot. Unbounded growth with "
                 "flat data volume means snapshots are not being expired.",
            colours={"bytes": COLORS["compactor"]}, minimum=0),
        timeseries("Transaction rate", [
            ("rate(bench_pg_xact_commit_total[1m])", "commits/s"),
            ("rate(bench_pg_xact_rollback_total[1m])", "rollbacks/s"),
        ], "short", 12, 8, 12, 18,
            colours={"commits/s": COLORS["good"], "rollbacks/s": COLORS["bad"]}, minimum=0),

        row("Rows", 26),
        timeseries("Rows in live files", [
            ("bench_backlog_rows", "rows"),
            ("bench_watermark_committed_rows", "watermark rows"),
        ], "short", 24, 7, 0, 27,
            desc="These two should agree. Divergence means the watermark and the file "
                 "registration, which are supposed to commit in one transaction, did not.",
            minimum=0),
    ]
    return dashboard("bench-storage", "Storage and catalog",
                     "S3 growth, file sizes, and PostgreSQL catalog health.", p, ["storage"])


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    builders = [
        ("pipeline.json", d_pipeline),
        ("generator.json", d_generator),
        ("collector.json", d_collector),
        ("compactor.json", d_compactor),
        ("infrastructure.json", d_infra),
        ("errors.json", d_errors),
        ("storage.json", d_storage),
    ]
    for name, fn in builders:
        path = os.path.join(OUT, name)
        with open(path, "w") as fh:
            json.dump(fn(), fh, indent=2)
            fh.write("\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
