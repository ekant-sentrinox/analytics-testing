#!/usr/bin/env python3
"""Build results/<test-id>/report.md and charts/ from a run's collected data.

Everything here is derived from files. Nothing is typed in, and nothing is
inferred when the underlying file is missing — a section whose input is absent
says NOT MEASURED and names the file it wanted. That rule is the whole point:
a report that quietly omits a missing measurement reads identically to one where
the measurement was fine.

Inputs, all under results/<test-id>/:
    metadata.json           config echo and host facts
    generator.json          final totals and per-step results
    generator.jsonl         per-second generator samples
    manifest.json           offered/accepted/rejected and seq ranges
    correctness.json        loss / duplication / gap verdict
    environment.json        all three servers
    state-before.json       catalog + S3 before the run
    state-after.json        catalog + S3 after settling
    raw/pipeline.jsonl      backlog, lag, compaction counters, PG stats
    raw/host-*.jsonl        per-second CPU / memory / disk / network per server
    raw/compaction.jsonl    compaction events parsed out of the compactor log
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from typing import Any

MISSING = "NOT MEASURED"


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def load_json(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:                                             # noqa: BLE001
        return None


def load_jsonl(path: str) -> list[dict]:
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


class Run:
    def __init__(self, run_dir: str) -> None:
        self.dir = run_dir
        self.id = os.path.basename(run_dir.rstrip("/"))
        self.metadata = load_json(f"{run_dir}/metadata.json") or {}
        self.generator = load_json(f"{run_dir}/generator.json") or {}
        self.manifest = load_json(f"{run_dir}/manifest.json") or {}
        self.correctness = load_json(f"{run_dir}/correctness.json") or {}
        self.environment = load_json(f"{run_dir}/environment.json") or {}
        self.before = load_json(f"{run_dir}/state-before.json") or {}
        self.after = (load_json(f"{run_dir}/state-collected.json")
                      or load_json(f"{run_dir}/state-after.json") or {})
        self.samples = load_jsonl(f"{run_dir}/generator.jsonl")
        self.pipeline = load_jsonl(f"{run_dir}/raw/pipeline.jsonl")
        self.compaction = load_jsonl(f"{run_dir}/raw/compaction.jsonl")
        self.hosts = {
            role: load_jsonl(f"{run_dir}/raw/host-{role}.jsonl")
            for role in ("generator", "collector", "compactor")
        }


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def fmt_int(v) -> str:
    return f"{int(v):,}" if isinstance(v, (int, float)) else MISSING


def fmt_num(v, digits=1, suffix="") -> str:
    return f"{v:,.{digits}f}{suffix}" if isinstance(v, (int, float)) else MISSING


def fmt_bytes(v) -> str:
    if not isinstance(v, (int, float)):
        return MISSING
    v = float(v)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(v) < 1024 or unit == "TiB":
            return f"{v:,.2f} {unit}"
        v /= 1024
    return f"{v:,.2f} TiB"


def series(rows: list[dict], key: str) -> list[float]:
    out = []
    for r in rows:
        v = r.get(key)
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def stats(values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    return {
        "n": len(s), "min": s[0], "max": s[-1],
        "mean": statistics.fmean(s), "median": statistics.median(s),
        "p95": s[min(len(s) - 1, int(0.95 * len(s)))],
        "stdev": statistics.pstdev(s) if len(s) > 1 else 0.0,
    }


def linear_slope(xs: list[float], ys: list[float]) -> float | None:
    """Least-squares slope. Used on the backlog series: a positive slope over
    the steady-state window means the pipeline is falling behind, which
    disqualifies a rate no matter how good the throughput looks."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def dig(node: Any, *path, default=None):
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return node if node is not None else default


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------

def make_charts(run: Run, out_dir: str) -> list[tuple[str, str]]:
    """Render PNGs. Returns [(filename, caption)] for the ones actually made."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        return []

    os.makedirs(out_dir, exist_ok=True)
    made: list[tuple[str, str]] = []

    # Muted, colour-blind-safe, consistent across every chart.
    C_OFFERED, C_ACCEPTED, C_REJECTED = "#8899a6", "#2f7d95", "#b4553f"
    C_P50, C_P95, C_P99 = "#8fae9b", "#c39b4e", "#b4553f"
    C_GEN, C_COL, C_COM = "#7a8fa6", "#2f7d95", "#8a6fa8"

    def finish(fig, ax_or_axes, name, caption, legend=True):
        axes = ax_or_axes if isinstance(ax_or_axes, (list, tuple)) else [ax_or_axes]
        for ax in axes:
            ax.grid(True, alpha=0.25, linewidth=0.6)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            if legend and ax.get_legend_handles_labels()[0]:
                ax.legend(frameon=False, fontsize=8)
            if getattr(ax, "_time_axis", False):
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        fig.autofmt_xdate()
        fig.tight_layout()
        path = os.path.join(out_dir, name)
        fig.savefig(path, dpi=110)
        plt.close(fig)
        made.append((name, caption))

    def times(rows):
        import datetime as dt
        return [dt.datetime.fromtimestamp(r["ts"], dt.timezone.utc)
                for r in rows if isinstance(r.get("ts"), (int, float))]

    gs = [r for r in run.samples if isinstance(r.get("ts"), (int, float))]

    # 1. offered vs accepted vs rejected
    if gs:
        t = times(gs)
        fig, ax = plt.subplots(figsize=(10, 3.6))
        ax._time_axis = True
        ax.plot(t, series(gs, "offered_rps"), color=C_OFFERED, lw=1.2,
                ls="--", label="offered")
        ax.plot(t, series(gs, "accepted_rps"), color=C_ACCEPTED, lw=1.6, label="accepted")
        rej = series(gs, "rejected_rps")
        if any(rej):
            ax.plot(t, rej, color=C_REJECTED, lw=1.2, label="rejected")
        ax.set_ylabel("records / s")
        ax.set_title("Offered vs delivered throughput", fontsize=10, loc="left")
        finish(fig, ax, "throughput.png",
               "Offered is what the generator attempted; accepted is what the collector acked. "
               "A gap between them is the pipeline refusing or failing work.")

    # 2. latency percentiles
    if gs:
        t = times(gs)
        fig, ax = plt.subplots(figsize=(10, 3.6))
        ax._time_axis = True
        ax.plot(t, series(gs, "latency_p50_ms"), color=C_P50, lw=1.2, label="p50")
        ax.plot(t, series(gs, "latency_p95_ms"), color=C_P95, lw=1.3, label="p95")
        ax.plot(t, series(gs, "latency_p99_ms"), color=C_P99, lw=1.5, label="p99")
        ax.set_ylabel("ms")
        ax.set_yscale("symlog", linthresh=10)
        ax.set_title("Export RPC latency (includes Parquet write and catalog commit)",
                     fontsize=10, loc="left")
        finish(fig, ax, "latency.png",
               "The export RPC does not ack until the batch is durable, so this is "
               "end-to-end persistence latency, not network round trip.")

    # 3. backlog (B2) and visibility lag (B3)
    pl = [r for r in run.pipeline if isinstance(r.get("ts"), (int, float))]
    if pl:
        t = times(pl)
        files = [dig(r, "catalog", "backlog_files", default=None) for r in pl]
        small = [dig(r, "catalog", "small_files", default=None) for r in pl]
        if any(v is not None for v in files):
            fig, ax = plt.subplots(figsize=(10, 3.6))
            ax._time_axis = True
            ax.plot(t, [v if v is not None else float("nan") for v in files],
                    color=C_COL, lw=1.5, label="live files")
            ax.plot(t, [v if v is not None else float("nan") for v in small],
                    color=C_REJECTED, lw=1.2, label="below minor threshold")
            ax.set_ylabel("files")
            ax.set_title("B2 — uncompacted file backlog in the catalog",
                         fontsize=10, loc="left")
            finish(fig, ax, "backlog.png",
                   "What compaction drains. A rising trend over the second half of a "
                   "level means that rate is not sustainable.")

        lag = [dig(r, "watermark", "lag_seconds", default=None) for r in pl]
        if any(v is not None for v in lag):
            fig, ax = plt.subplots(figsize=(10, 3.2))
            ax._time_axis = True
            ax.plot(t, [v if v is not None else float("nan") for v in lag],
                    color="#8a6fa8", lw=1.4)
            ax.set_ylabel("seconds")
            ax.set_title("B3 — end-to-end visibility lag (now - newest committed watermark)",
                         fontsize=10, loc="left")
            finish(fig, ax, "lag.png",
                   "How stale a query against the lake is. Measured from the watermark "
                   "row committed with each file registration.", legend=False)

        objs = [dig(r, "s3", "bytes", default=None) for r in pl]
        if any(v for v in objs):
            fig, ax = plt.subplots(figsize=(10, 3.2))
            ax._time_axis = True
            ax.plot(t, [(v or 0) / 1048576 for v in objs], color="#2f7d95", lw=1.4)
            ax.set_ylabel("MiB")
            ax.set_title("S3 data growth under the test prefix", fontsize=10, loc="left")
            finish(fig, ax, "s3-growth.png",
                   "Total bytes under the test prefix, including files not yet cleaned "
                   "up after a merge.", legend=False)

        minor = [dig(r, "compactor", "databases", "bench", "totalMinorCompactions", default=None)
                 for r in pl]
        merged = [dig(r, "compactor", "databases", "bench", "totalFilesCompacted", default=None)
                  for r in pl]
        if any(v is not None for v in minor):
            fig, ax = plt.subplots(figsize=(10, 3.2))
            ax._time_axis = True
            ax.plot(t, [v if v is not None else float("nan") for v in minor],
                    color=C_COM, lw=1.4, label="minor compactions")
            ax.plot(t, [v if v is not None else float("nan") for v in merged],
                    color=C_ACCEPTED, lw=1.2, label="files merged")
            ax.set_ylabel("cumulative")
            ax.set_title("Compaction activity", fontsize=10, loc="left")
            finish(fig, ax, "compaction.png",
                   "Cumulative counters from the compactor's /health endpoint.")

    # 4. host CPU / memory / disk across all three servers
    host_rows = {k: [r for r in v if isinstance(r.get("ts"), (int, float))]
                 for k, v in run.hosts.items()}
    if any(host_rows.values()):
        colours = {"generator": C_GEN, "collector": C_COL, "compactor": C_COM}
        for metric, extract, ylabel, title, fname in (
            ("cpu", lambda r: r.get("cpu_percent"), "%", "CPU utilisation", "cpu.png"),
            ("mem", lambda r: dig(r, "memory", "mem_used_bytes", default=0) / 1073741824,
             "GiB", "Memory used", "memory.png"),
            ("rss", lambda r: (dig(r, "process", "rss_bytes", default=0) or 0) / 1073741824,
             "GiB", "Watched process RSS", "rss.png"),
        ):
            fig, ax = plt.subplots(figsize=(10, 3.4))
            ax._time_axis = True
            plotted = False
            for role, rows in host_rows.items():
                if not rows:
                    continue
                ys = [extract(r) for r in rows]
                if not any(ys):
                    continue
                ax.plot(times(rows), ys, color=colours[role], lw=1.2, label=role)
                plotted = True
            if not plotted:
                import matplotlib.pyplot as _p
                _p.close(fig)
                continue
            ax.set_ylabel(ylabel)
            ax.set_title(title, fontsize=10, loc="left")
            if metric == "cpu":
                ax.set_ylim(0, 105)
            finish(fig, ax, fname, f"{title}, 1 s samples from each host agent.")

        fig, ax = plt.subplots(figsize=(10, 3.4))
        ax._time_axis = True
        plotted = False
        for role, rows in host_rows.items():
            if not rows:
                continue
            ys = []
            for r in rows:
                io = r.get("disk_io") or {}
                ys.append(sum((d.get("write_bytes_per_s") or 0) for d in io.values()) / 1048576)
            if any(ys):
                ax.plot(times(rows), ys, color=colours[role], lw=1.2, label=role)
                plotted = True
        if plotted:
            ax.set_ylabel("MiB/s")
            ax.set_title("Disk write throughput", fontsize=10, loc="left")
            finish(fig, ax, "disk.png", "Aggregate write bytes per second per host.")
        else:
            import matplotlib.pyplot as _p
            _p.close(fig)

        fig, ax = plt.subplots(figsize=(10, 3.2))
        ax._time_axis = True
        plotted = False
        for role, rows in host_rows.items():
            if not rows:
                continue
            ys = [dig(r, "disk_space", "/", "free_bytes", default=None) for r in rows]
            ys = [(v or 0) / 1073741824 for v in ys]
            if any(ys):
                ax.plot(times(rows), ys, color=colours[role], lw=1.2, label=role)
                plotted = True
        if plotted:
            ax.set_ylabel("GiB free")
            ax.set_title("Root filesystem free space", fontsize=10, loc="left")
            finish(fig, ax, "disk-free.png",
                   "These are 8 GiB root volumes; running one out of space is a "
                   "plausible failure mode, so it is tracked.")
        else:
            import matplotlib.pyplot as _p
            _p.close(fig)

    # 5. error rate
    if gs and any(series(gs, "requests_failed")):
        t = times(gs)
        fig, ax = plt.subplots(figsize=(10, 3.0))
        ax._time_axis = True
        sent = series(gs, "requests_sent")
        failed = series(gs, "requests_failed")
        rate = [100.0 * f / s if s else 0.0 for f, s in zip(failed, sent)]
        ax.plot(t, rate, color=C_REJECTED, lw=1.3)
        ax.set_ylabel("% of RPCs")
        ax.set_title("Error rate", fontsize=10, loc="left")
        finish(fig, ax, "errors.png", "Failed export RPCs as a share of RPCs sent.",
               legend=False)

    # 6. staircase summary
    steps = run.generator.get("steps") or []
    if len(steps) > 1:
        fig, ax = plt.subplots(figsize=(9, 3.6))
        x = [s["target_rps"] for s in steps]
        offered = [s["offered"] / max(1e-9, s["duration_seconds"]) for s in steps]
        accepted = [s["accepted"] / max(1e-9, s["duration_seconds"]) for s in steps]
        width = 0.38
        idx = list(range(len(x)))
        ax.bar([i - width / 2 for i in idx], offered, width,
               color=C_OFFERED, label="offered")
        ax.bar([i + width / 2 for i in idx], accepted, width,
               color=C_ACCEPTED, label="accepted")
        ax.set_xticks(idx)
        ax.set_xticklabels([f"{int(v):,}" for v in x], fontsize=8)
        ax.set_xlabel("target rate (records/s)")
        ax.set_ylabel("records / s")
        ax.set_title("Staircase — offered vs accepted per level", fontsize=10, loc="left")
        finish(fig, ax, "staircase.png",
               "Per level, averaged over the level. A level only counts as sustainable "
               "if accepted tracks offered AND backlog is flat.")

    return made


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def steady_window(rows: list[dict], discard_fraction: float = 0.25) -> list[dict]:
    """Drop the leading warm-up. Buckets flush on a timer and compaction runs on
    its own schedule, so the first part of any level is transient by
    construction and fitting a trend through it measures the warm-up."""
    if len(rows) < 8:
        return rows
    return rows[int(len(rows) * discard_fraction):]


def analyse_backlog(run: Run) -> dict:
    pl = [r for r in run.pipeline
          if isinstance(r.get("ts"), (int, float)) and dig(r, "catalog", "backlog_files") is not None]
    if len(pl) < 4:
        return {"available": False,
                "reason": "fewer than 4 pipeline samples with catalog data"}
    window = steady_window(pl)
    xs = [r["ts"] - window[0]["ts"] for r in window]
    files = [dig(r, "catalog", "backlog_files", default=0) for r in window]
    nbytes = [dig(r, "catalog", "backlog_bytes", default=0) for r in window]
    return {
        "available": True,
        "samples": len(window),
        "discarded_warmup": len(pl) - len(window),
        "files_first": files[0], "files_last": files[-1],
        "files_slope_per_min": (linear_slope(xs, files) or 0.0) * 60,
        "bytes_slope_mib_per_min": (linear_slope(xs, nbytes) or 0.0) * 60 / 1048576,
        "files_max": max(files),
    }


def analyse_lag(run: Run) -> dict:
    vals = [dig(r, "watermark", "lag_seconds") for r in run.pipeline]
    vals = [v for v in vals if isinstance(v, (int, float)) and v >= 0]
    return {"available": bool(vals), **(stats(vals) if vals else {})}


def bottleneck(run: Run) -> tuple[str, str]:
    """Name the binding constraint, or refuse to.

    Conclusions get invented here more than anywhere else, so each branch has to
    point at a specific measured quantity.
    """
    transport = (run.metadata or {}).get("transport", "direct")
    if transport != "direct":
        return (MISSING,
                f"this run went over {transport}, so the rate and latency data cannot "
                "identify a bottleneck — the transport is a plausible constraint of its "
                "own and cannot be separated from the pipeline's")

    totals = run.generator.get("totals") or {}
    offered = totals.get("records_offered", 0)
    accepted = totals.get("records_accepted", 0)
    rejected = totals.get("records_rejected", 0)
    if not offered:
        return MISSING, "the generator recorded no offered records"

    ratio = accepted / offered
    backlog = analyse_backlog(run)
    stalled = totals.get("stalled_ms", 0.0)
    duration = totals.get("duration_seconds", 0.0) or 1.0
    stall_share = stalled / (duration * 1000.0)

    host_cpu = {}
    for role, rows in run.hosts.items():
        vals = series(rows, "cpu_percent")
        if vals:
            host_cpu[role] = statistics.fmean(vals)

    if rejected > 0:
        return ("collector ingestion queue",
                f"{rejected:,} records were rejected with RESOURCE_EXHAUSTED, which the "
                "collector raises only when pending write bytes exceed max_pending_write — "
                "the writer could not drain buckets as fast as they arrived")

    if ratio < 0.99:
        return ("collector accept path",
                f"accepted/offered was {ratio:.4f}; the shortfall is failed or unsent RPCs "
                "rather than explicit rejection — see the error breakdown")

    if backlog.get("available") and backlog["files_slope_per_min"] > 0.5:
        return ("compaction",
                f"accept kept up but the uncompacted file backlog grew "
                f"{backlog['files_slope_per_min']:.2f} files/min in steady state — "
                "the compactor is not draining as fast as the collector produces")

    if stall_share > 0.10:
        top = max(host_cpu, key=host_cpu.get) if host_cpu else None
        if top == "generator" and host_cpu.get("generator", 0) > 80:
            return ("generator (SERVER 1)",
                    f"the pacer was blocked on the inflight bound {stall_share * 100:.1f}% of the "
                    f"run with generator CPU at {host_cpu['generator']:.0f}% — this run measured "
                    "the load generator, not the pipeline")
        return ("in-flight bound",
                f"the pacer stalled for {stall_share * 100:.1f}% of the run; raise "
                "max_inflight or add workers before treating this as a pipeline limit")

    saturated = [r for r, v in host_cpu.items() if v > 85]
    if saturated:
        return (f"CPU on {', '.join(saturated)}",
                "mean CPU above 85% with no rejection and flat backlog — the level held, "
                "but there is no headroom above it")

    return ("not reached",
            "accepted tracked offered, no rejections, backlog flat and CPU below 85%: "
            "the offered rate was below the system's capacity, so this run did not find "
            "the ceiling")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render(run: Run, charts: list[tuple[str, str]]) -> str:
    md: list[str] = []
    w = md.append

    totals = run.generator.get("totals") or {}
    meta = run.metadata
    steps = run.generator.get("steps") or []
    env = run.environment

    w(f"# Test report — `{run.id}`")
    w("")
    w("Generated by `bench/report.py` from the files in this directory. "
      "Every number below has a source file named next to it. Anything whose "
      f"source is absent reads `{MISSING}` rather than being estimated.")
    w("")

    # A tunnelled run is functionally valid and performance-invalid. Say so at
    # the top, before anyone reads a rate off the summary table.
    transport = meta.get("transport", "direct")
    if transport != "direct":
        w("> ## ⚠ Throughput and latency in this report are NOT VALID")
        w("> ")
        w(f"> This run was recorded over **`{transport}`**, not a direct connection.")
        note = meta.get("transport_note")
        if note:
            w(f"> {note}")
        w("> ")
        w("> The measured path carries SSH encryption, an extra userspace hop and "
          "TCP-over-TCP, and a single `ssh` process is single-threaded — it becomes "
          "a bottleneck of its own well before the pipeline does.")
        w("> ")
        w("> | | |")
        w("> |---|---|")
        w("> | Functional behaviour, correctness, compaction, fault response | **valid** |")
        w("> | Offered / accepted / rejected rates | **not valid** |")
        w("> | Latency percentiles | **not valid** |")
        w("> | Backlog, catalog and S3 figures | valid — they are read directly, not through the tunnel |")
        w("> ")
        w("> Fix the security group and re-run before quoting any rate from this. "
          "See `NETWORK.md`.")
        w("")

    # -- summary -----------------------------------------------------------
    w("## Test summary")
    w("")
    verdict = run.correctness.get("verdict")
    perf_note = ""
    if verdict == "FAIL":
        perf_note = "  **Performance figures below are reported with this failure attached.**"
    w("| | |")
    w("|---|---|")
    w(f"| Test id | `{run.id}` |")
    w(f"| Profile | `{meta.get('profile', MISSING)}` |")
    w(f"| Description | {meta.get('description') or '—'} |")
    w(f"| Started (UTC) | {run.manifest.get('started_at_utc', MISSING)} |")
    w(f"| Ended (UTC) | {run.manifest.get('ended_at_utc', MISSING)} |")
    w(f"| Duration | {fmt_num(totals.get('duration_seconds'), 1, ' s')} |")
    w(f"| Offered | {fmt_int(totals.get('records_offered'))} records "
      f"({fmt_num(totals.get('offered_rps_mean'), 0)} /s mean) |")
    w(f"| **Accepted** | **{fmt_int(totals.get('records_accepted'))} records "
      f"({fmt_num(totals.get('accepted_rps_mean'), 0)} /s mean)** |")
    w(f"| Rejected | {fmt_int(totals.get('records_rejected'))} records |")
    w(f"| Success rate | {fmt_num((totals.get('success_rate') or 0) * 100, 2, ' %')} |")
    w(f"| Error rate | {fmt_num(100 - (totals.get('success_rate') or 0) * 100, 2, ' %')} |")
    w(f"| Correctness | **{verdict or MISSING}**{perf_note} |")
    w(f"| Transport | `{transport}`"
      + ("  **— rates and latency above are not valid measurements**"
         if transport != "direct" else "") + " |")
    w(f"| Environment hash | `{env.get('env_hash', MISSING)}` |")
    w(f"| DazzleDuck commit | `{dig(env, 'dazzleduck', 'git_commit', default=MISSING)}` |")
    w("")
    w("> `offered` is what the generator attempted and is **not** throughput. "
      "Only `accepted`, sustained with a flat backlog, is throughput. "
      "Source: `manifest.json`, `generator.json`.")
    w("")

    # -- throughput --------------------------------------------------------
    w("## Throughput")
    w("")
    acc = series(run.samples, "accepted_rps")
    if acc:
        st = stats(acc)
        steady = series(steady_window(run.samples), "accepted_rps")
        sst = stats(steady) if steady else {}
        w("| Metric | records/s |")
        w("|---|---:|")
        w(f"| Mean (whole run) | {fmt_num(st['mean'], 0)} |")
        w(f"| Mean (steady state, first 25% discarded) | {fmt_num(sst.get('mean'), 0)} |")
        w(f"| Median | {fmt_num(st['median'], 0)} |")
        w(f"| Minimum | {fmt_num(st['min'], 0)} |")
        w(f"| Peak (1 s) | {fmt_num(st['max'], 0)} |")
        w(f"| Std deviation | {fmt_num(st['stdev'], 0)} |")
        w("")
        w("Peak is a single one-second sample and is not a sustainable rate. "
          "Source: `generator.jsonl`.")
    else:
        w(f"{MISSING} — `generator.jsonl` has no samples.")
    w("")

    if len(steps) > 1:
        w("### Staircase")
        w("")
        w("| Step | Target rps | Offered | Accepted | Rejected | Accepted/offered | Stalled |")
        w("|---:|---:|---:|---:|---:|---:|---:|")
        for s in steps:
            ratio = s["accepted"] / s["offered"] if s["offered"] else 0
            w(f"| {s['index']} | {s['target_rps']:,.0f} | {s['offered']:,} | "
              f"{s['accepted']:,} | {s['rejected']:,} | {ratio:.4f} | "
              f"{s['stalled_ms']:,.0f} ms |")
        w("")
        w("A level is **sustainable** only if accepted/offered >= 0.99, the backlog slope "
          "over its steady-state window is not positive, there were no RESOURCE_EXHAUSTED "
          "rejections, and no restart or OOM occurred. Judging on the ratio alone will "
          "call a level sustainable while the lake silently falls behind.")
        w("")

    # -- latency -----------------------------------------------------------
    w("## Latency")
    w("")
    if totals.get("latency_p50_ms") is not None:
        w("| Percentile | ms |")
        w("|---|---:|")
        w(f"| p50 | {fmt_num(totals.get('latency_p50_ms'), 1)} |")
        w(f"| p95 | {fmt_num(totals.get('latency_p95_ms'), 1)} |")
        w(f"| p99 | {fmt_num(totals.get('latency_p99_ms'), 1)} |")
        w(f"| min | {fmt_num(totals.get('latency_min_ms'), 1)} |")
        w(f"| max | {fmt_num(totals.get('latency_max_ms'), 1)} |")
        w("")
        w("Measured around the whole export RPC. The collector completes the response "
          "from the batch-write future, so this includes queue wait, the DuckDB COPY to "
          "Parquet, and the DuckLake catalog commit — it is persistence latency, not "
          "network time. Source: `generator.json`.")
    else:
        w(f"{MISSING} — no latency samples in `generator.json`.")
    w("")

    # -- resources ---------------------------------------------------------
    w("## Resource utilisation")
    w("")
    any_host = False
    w("| Server | Role | CPU mean | CPU peak | Mem mean | Mem peak | Process RSS peak | Samples |")
    w("|---|---|---:|---:|---:|---:|---:|---:|")
    hosts_cfg = {"generator": meta.get("host", {}).get("hostname", ""),
                 "collector": "", "compactor": ""}
    for role, rows in run.hosts.items():
        if not rows:
            w(f"| — | {role} | {MISSING} | | | | | 0 |")
            continue
        any_host = True
        cpu = stats(series(rows, "cpu_percent"))
        mem = stats([dig(r, "memory", "mem_used_bytes", default=0) for r in rows])
        rss = stats([(dig(r, "process", "rss_bytes", default=0) or 0) for r in rows])
        host = rows[0].get("hostname", hosts_cfg.get(role, ""))
        w(f"| `{host}` | {role} | {fmt_num(cpu.get('mean'), 1, '%')} | "
          f"{fmt_num(cpu.get('max'), 1, '%')} | {fmt_bytes(mem.get('mean'))} | "
          f"{fmt_bytes(mem.get('max'))} | {fmt_bytes(rss.get('max'))} | {len(rows)} |")
    w("")
    if any_host:
        w("1 s samples from `bench/host_agent.py`. Source: `raw/host-*.jsonl`.")
        restarts = {role: max((r.get("process_restarts") or 0) for r in rows)
                    for role, rows in run.hosts.items() if rows}
        ooms = {role: max((r.get("oom_kills") or 0) for r in rows)
                for role, rows in run.hosts.items() if rows}
        if any(restarts.values()):
            w("")
            w(f"**Process restarts observed:** {restarts}. A restart during a measured "
              "level invalidates that level.")
        if any(ooms.values()):
            w("")
            w(f"**OOM kills observed:** {ooms}.")
    else:
        w(f"{MISSING} — no host agent samples were collected.")
    w("")

    # -- storage -----------------------------------------------------------
    w("## Storage and catalog")
    w("")
    b, a = run.before.get("catalog") or {}, run.after.get("catalog") or {}
    bs, as_ = run.before.get("s3") or {}, run.after.get("s3") or {}
    if b or a:
        w("| Quantity | Before | After | Delta |")
        w("|---|---:|---:|---:|")

        def row(label, key, fmt=fmt_int, src=(b, a)):
            x, y = src[0].get(key), src[1].get(key)
            d = (y - x) if isinstance(x, (int, float)) and isinstance(y, (int, float)) else None
            w(f"| {label} | {fmt(x)} | {fmt(y)} | {fmt(d) if d is not None else MISSING} |")

        row("Live data files", "live_files")
        row("Live bytes", "live_bytes", fmt_bytes)
        row("Live rows", "live_rows")
        row("Files below minor threshold", "small_files")
        row("Snapshots", "snapshots")
        row("Catalog DB size", "catalog_db_bytes", fmt_bytes)
        row("S3 objects", "objects", fmt_int, (bs, as_))
        row("S3 bytes", "bytes", fmt_bytes, (bs, as_))
        w("")
        if isinstance(a.get("file_size_avg"), (int, float)) and a.get("live_files"):
            w(f"Mean live file size after the run: **{fmt_bytes(a['file_size_avg'])}** "
              f"(min {fmt_bytes(a.get('file_size_min'))}, max {fmt_bytes(a.get('file_size_max'))}). "
              "File size is set by the collector's `min_bucket_size` and `max_delay_ms`, "
              "and it is what determines how much work compaction has to do.")
            w("")
        w("Source: `state-before.json`, `state-collected.json`.")
    else:
        w(f"{MISSING} — no catalog snapshots.")
    w("")

    # -- backlog -----------------------------------------------------------
    w("## Backlog")
    w("")
    w("Three different quantities. They are reported separately because averaging "
      "them together destroys the only useful information in them.")
    w("")
    w("**B1 — collector in-memory queue depth.** `NOT AVAILABLE`. The gauges "
      "(`writer.pending_batches`, `writer.pending_buckets`) are registered in a "
      "`SimpleMeterRegistry` that is never exported, and `/health` exposes only "
      "`batchesProcessed`. Closing this needs a Prometheus registry in the collector "
      "— see IMPROVEMENTS.md.")
    w("")
    bl = analyse_backlog(run)
    if bl.get("available"):
        trend = ("flat" if abs(bl["files_slope_per_min"]) < 0.5
                 else "GROWING" if bl["files_slope_per_min"] > 0 else "draining")
        w(f"**B2 — uncompacted file backlog.** {bl['files_first']} files at the start of the "
          f"steady-state window, {bl['files_last']} at the end, peak {bl['files_max']}. "
          f"Least-squares slope **{bl['files_slope_per_min']:+.2f} files/min** "
          f"({bl['bytes_slope_mib_per_min']:+.2f} MiB/min) — **{trend}**. "
          f"{bl['samples']} samples, {bl['discarded_warmup']} warm-up samples discarded.")
        if trend == "GROWING":
            w("")
            w("> A positive backlog slope in steady state means compaction is not keeping up. "
              "Whatever the accept rate was, this level is **not sustainable**.")
    else:
        w(f"**B2 — uncompacted file backlog.** {MISSING} — {bl.get('reason', 'no data')}.")
    w("")
    lag = analyse_lag(run)
    if lag.get("available"):
        w(f"**B3 — end-to-end visibility lag.** mean {fmt_num(lag['mean'], 1, ' s')}, "
          f"median {fmt_num(lag['median'], 1, ' s')}, p95 {fmt_num(lag['p95'], 1, ' s')}, "
          f"max {fmt_num(lag['max'], 1, ' s')} over {lag['n']} samples. "
          "Computed as `now() - max(max_timestamp)` against the watermark table, which is "
          "committed in the same transaction as the file registration.")
    else:
        w(f"**B3 — end-to-end visibility lag.** {MISSING} — the watermark table produced no "
          "readings (no batch committed, or the watermark scrape failed).")
    w("")

    # -- compaction --------------------------------------------------------
    w("## Compaction")
    w("")
    cb = dig(run.before, "compactor_health", "databases", "bench", default={}) or {}
    ca = dig(run.after, "compactor_health", "databases", "bench", default={}) or {}
    if cb or ca:
        w("| Counter | Before | After | Delta |")
        w("|---|---:|---:|---:|")
        for label, key in (("Minor compactions", "totalMinorCompactions"),
                           ("Major compactions", "totalMajorCompactions"),
                           ("Files merged", "totalFilesCompacted"),
                           ("Small files (current)", "currentSmallFiles"),
                           ("Medium files (current)", "currentMediumFiles"),
                           ("Total files (current)", "currentTotalFiles")):
            x, y = cb.get(key), ca.get(key)
            d = (y - x) if isinstance(x, int) and isinstance(y, int) else None
            w(f"| {label} | {fmt_int(x)} | {fmt_int(y)} | {fmt_int(d) if d is not None else MISSING} |")
        w("")
        w("Source: compactor `/health`, captured in `state-before.json` / "
          "`state-collected.json`.")
    else:
        w(f"{MISSING} — compactor `/health` was not reachable at snapshot time.")
    w("")

    merges = [r for r in run.compaction
              if r.get("kind") == "meter" and r.get("metric") == "ducklake.compaction.duration"]
    if merges:
        by_type: dict[str, list[float]] = {}
        for r in merges:
            key = f"{r.get('type')}/{r.get('step')}"
            if isinstance(r.get("mean_ms"), (int, float)):
                by_type.setdefault(key, []).append(r["mean_ms"])
        if by_type:
            w("| type/step | samples | mean ms | max ms |")
            w("|---|---:|---:|---:|")
            for key, vals in sorted(by_type.items()):
                st = stats(vals)
                w(f"| {key} | {st['n']} | {fmt_num(st['mean'], 1)} | {fmt_num(st['max'], 1)} |")
            w("")
            w("Parsed out of the compactor log by `bench/parse_compactor_log.py`. "
              "The compactor uses a `LoggingMeterRegistry`, so these timings exist "
              "only as log lines — there is no scrape endpoint. Source: "
              "`raw/compaction.jsonl`.")
        untimed = sum(1 for r in run.compaction if r.get("no_timestamp"))
        if untimed:
            w("")
            w(f"> {untimed} compaction records carried no timestamp and could not be placed "
              "on a timeline.")
    w("")

    # -- correctness -------------------------------------------------------
    w("## Correctness")
    w("")
    if run.correctness:
        c = run.correctness
        w(f"**VERDICT: {c.get('verdict', MISSING)}**")
        w("")
        w(f"Window `{' .. '.join(c.get('window_utc', ['?', '?']))}` UTC.")
        w("")
        w("| Check | Result | Detail |")
        w("|---|---|---|")
        for name, chk in (c.get("checks") or {}).items():
            state = {True: "PASS", False: "**FAIL**", None: "skipped"}[chk.get("pass")]
            w(f"| {name} | {state} | {chk.get('detail', '')} |")
        w("")
        w(f"Offered {fmt_int(c.get('offered'))}, accepted {fmt_int(c.get('accepted'))}, "
          f"rejected {fmt_int(c.get('rejected'))}, landed {fmt_int(c.get('landed'))}.")
        w("")
        w("`accepted - landed` is data loss and is a hard failure at any throughput. "
          "`offered - accepted` is rejection or RPC failure, which is expected under "
          "backpressure and is not loss. Source: `correctness.json`.")
    else:
        w(f"{MISSING} — `correctness.json` absent. **A performance number without a "
          "correctness verdict is not a result.**")
    w("")

    # -- errors ------------------------------------------------------------
    w("## Errors")
    w("")
    errs = totals.get("errors_by_code") or {}
    if errs:
        w("| gRPC status | Count |")
        w("|---|---:|")
        for code, n in sorted(errs.items(), key=lambda kv: -kv[1]):
            w(f"| `{code}` | {n:,} |")
        w("")
        if "RESOURCE_EXHAUSTED" in errs:
            w("`RESOURCE_EXHAUSTED` is the collector's explicit backpressure signal: pending "
              "write bytes exceeded `max_pending_write` and the queue raised "
              "`PendingWriteExceededException`. It carries a `RetryInfo` delay. This is the "
              "pipeline correctly refusing work, not an error in the usual sense — but any "
              "of it disqualifies the level as sustainable.")
            w("")
        if "DEADLINE_EXCEEDED" in errs:
            w("`DEADLINE_EXCEEDED` means the export RPC did not complete within "
              f"{dig(meta, 'target', 'rpc_timeout_seconds', default='?')}s. Because the RPC "
              "acks on durability, this is a slow flush, and the records may still have "
              "landed — check the correctness section before calling it loss.")
            w("")
    else:
        w("No failed RPCs recorded.")
    w("")

    for role in ("collector", "compactor"):
        path = os.path.join(run.dir, "logs", f"{role}.errors.log")
        if os.path.exists(path) and os.path.getsize(path):
            with open(path, errors="replace") as fh:
                lines = fh.read().splitlines()
            w(f"### {role} log — {len(lines)} error/warning lines")
            w("")
            w("```")
            for line in lines[:15]:
                w(line[:200])
            if len(lines) > 15:
                w(f"... {len(lines) - 15} more in logs/{role}.errors.log")
            w("```")
            w("")

    # -- environment -------------------------------------------------------
    w("## Environment")
    w("")
    servers = env.get("servers") or {}
    if servers:
        w("| Role | Host | Instance | vCPU (phys/logical) | RAM | Root free | Burstable |")
        w("|---|---|---|---|---:|---:|---|")
        for role in ("generator", "collector", "compactor"):
            s = servers.get(role) or {}
            h, i, d = s.get("host") or {}, s.get("instance") or {}, s.get("disk") or {}
            w(f"| {role} | `{h.get('hostname', '?')}` | {i.get('type', '?')} | "
              f"{h.get('physical_cores', '?')}/{h.get('logical_cores', '?')} | "
              f"{fmt_bytes(h.get('ram_bytes'))} | {fmt_bytes(d.get('root_free_bytes'))} | "
              f"{'yes' if i.get('burstable') else 'no'} |")
        w("")
        if any((servers.get(r) or {}).get("instance", {}).get("burstable") for r in servers):
            w("> All three are **burstable t3** instances. CPU credit exhaustion throttles the "
              "vCPU and looks exactly like a software regression. A long soak on t3 will hit "
              "this; treat any unexplained late-run slowdown as a credit question first.")
            w("")
        if any((servers.get(r) or {}).get("disk", {}).get("tmp_is_tmpfs") for r in servers):
            w("> `/tmp` is tmpfs (RAM) on these hosts, and there is no swap. DuckDB's "
              "`temp_directory` is therefore pointed at `/var/tmp`; spilling to `/tmp` would "
              "convert a disk spill into an OOM kill.")
            w("")
    w(f"Catalog: `{dig(env, 'catalog', 'kind', default='?')}` at "
      f"`{dig(env, 'catalog', 'host', default='?')}/{dig(env, 'catalog', 'database', default='?')}`. "
      f"Storage: `s3://{dig(env, 'storage', 'bucket', default='?')}/"
      f"{dig(env, 'storage', 'prefix', default='?')}/` in "
      f"`{dig(env, 'storage', 'region', default='?')}`.")
    w("")
    w("Source: `environment.json`.")
    w("")

    # -- charts ------------------------------------------------------------
    if charts:
        w("## Charts")
        w("")
        for name, caption in charts:
            title = name.replace(".png", "").replace("-", " ")
            w(f"### {title}")
            w("")
            w(f"![{title}](charts/{name})")
            w("")
            w(f"{caption}")
            w("")

    # -- conclusion --------------------------------------------------------
    w("## Conclusion")
    w("")
    name, why = bottleneck(run)
    w(f"**Binding constraint: {name}**")
    w("")
    w(why + ".")
    w("")

    if len(steps) > 1:
        w("### Maximum sustainable rate")
        w("")
        sustainable = None
        for s in steps:
            ratio = s["accepted"] / s["offered"] if s["offered"] else 0
            if ratio >= 0.99 and s["rejected"] == 0:
                sustainable = s
            else:
                break
        if sustainable:
            w(f"Highest level meeting accepted/offered >= 0.99 with zero rejections: "
              f"**{sustainable['target_rps']:,.0f} records/s** "
              f"(step {sustainable['index']}, accepted "
              f"{sustainable['accepted'] / max(1e-9, sustainable['duration_seconds']):,.0f}/s).")
            if not bl.get("available"):
                w("")
                w("> This is **provisional**: the backlog series was not available, so the "
                  "flat-backlog half of the sustainability criterion could not be evaluated. "
                  "It is the highest level that passed the accept-rate test only.")
        else:
            w("No level met the sustainability criteria.")
        w("")
        peak = max((s["accepted"] / max(1e-9, s["duration_seconds"]) for s in steps), default=0)
        w(f"Highest instantaneous accepted rate at any level: **{peak:,.0f} records/s**. "
          "Reported separately from the sustainable figure on purpose — they are different "
          "claims and only the sustainable one describes what the system can be run at.")
        w("")

    w("### What this run does not establish")
    w("")
    unknowns = []
    if not run.hosts.get("collector"):
        unknowns.append("collector host metrics were not collected, so nothing can be said "
                        "about CPU or memory headroom on SERVER 2")
    if not bl.get("available"):
        unknowns.append("no backlog series, so sustainability could not be judged")
    if not lag.get("available"):
        unknowns.append("no watermark readings, so end-to-end visibility lag is unknown")
    if len(steps) <= 1:
        unknowns.append("a single rate was offered, so the throughput ceiling was not searched for")
    if (totals.get("duration_seconds") or 0) < 600:
        unknowns.append(f"the run was only {fmt_num(totals.get('duration_seconds'), 0, 's')} long — "
                        "too short to contain several compaction cycles, so backlog trend is "
                        "indicative at best")
    if not run.correctness:
        unknowns.append("correctness was not validated")
    if unknowns:
        for u in unknowns:
            w(f"- {u}")
    else:
        w("- Nothing material was left unmeasured for this profile.")
    w("")

    w("---")
    w("")
    w(f"Files in this run directory: "
      f"`{'`, `'.join(sorted(os.listdir(run.dir))[:24])}`")
    w("")
    return "\n".join(md)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--no-charts", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.run_dir):
        sys.exit(f"no such run directory: {args.run_dir}")

    run = Run(args.run_dir)
    charts = [] if args.no_charts else make_charts(run, os.path.join(args.run_dir, "charts"))
    text = render(run, charts)

    out = os.path.join(args.run_dir, "report.md")
    with open(out, "w") as fh:
        fh.write(text)
    print(f"wrote {out} ({len(text.splitlines())} lines, {len(charts)} charts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
