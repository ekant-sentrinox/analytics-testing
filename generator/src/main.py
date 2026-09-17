"""Generator entrypoint.

    python -m generator.src.main --config config/generator.yaml --profile smoke

Writes everything for one run under results/<test-id>/:

    metadata.json   config echo, host facts, git commit, clock offset
    generator.jsonl per-second samples
    generator.csv   the same samples, flat, for plotting
    generator.json  final totals plus per-step results
    manifest.json   the reconciliation contract: per-gen_id seq ranges, totals,
                    UTC start/stop. validate_correctness.py compares the landed
                    rows against this and nothing else.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict

# Allow both `python -m generator.src.main` and direct execution.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from generator.src import config as config_mod                        # noqa: E402
    from generator.src.loadgen import LoadGenerator, StepResult           # noqa: E402
    from generator.src.metrics import Metrics                             # noqa: E402
    from generator.src.otlp_client import LogsExporter, TokenSource       # noqa: E402
    from generator.src.payload import build_factory                       # noqa: E402
    from generator.src.payload_ai_txn import build_factory as build_ai_txn_factory  # noqa: E402
else:
    from . import config as config_mod
    from .loadgen import LoadGenerator, StepResult
    from .metrics import Metrics
    from .otlp_client import LogsExporter, TokenSource
    from .payload import build_factory
    from .payload_ai_txn import build_factory as build_ai_txn_factory

log = logging.getLogger("generator")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="generator", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/generator.yaml")
    ap.add_argument("--profile", default=os.environ.get("DEFAULT_PROFILE", "smoke"))
    ap.add_argument("--test-id", default=None,
                    help="default: <UTC yyyymmdd-HHMMSS>-<profile>")
    ap.add_argument("--results-dir", default=None, help="overrides generator.output.results_dir")
    # Workload overrides; each wins over the profile.
    ap.add_argument("--rps", type=float, default=None)
    ap.add_argument("--duration", type=float, default=None, dest="duration_seconds")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--channels", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=None)
    ap.add_argument("--max-inflight", type=int, default=None)
    ap.add_argument("--retry-mode", choices=["none", "polite"], default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate config and connectivity, send nothing")
    ap.add_argument("--log-level", default="INFO")
    return ap.parse_args(argv)


def _git_commit(path: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
    except Exception:                                             # noqa: BLE001
        return "unknown"


def _clock_offset_ms() -> float | None:
    """Max NTP offset, so cross-machine lag numbers can be trusted or discounted."""
    try:
        out = subprocess.check_output(["chronyc", "tracking"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=5)
        for line in out.splitlines():
            if line.startswith("Last offset"):
                return abs(float(line.split(":")[1].strip().split()[0])) * 1000.0
    except Exception:                                             # noqa: BLE001
        return None
    return None


def collect_metadata(cfg, test_id: str, argv: list[str]) -> dict:
    return {
        "test_id": test_id,
        "profile": cfg.profile,
        "description": cfg.description,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "argv": argv,
        "host": {
            "hostname": socket.gethostname(),
            "fqdn": socket.getfqdn(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "clock": {"max_offset_ms": _clock_offset_ms(), "source": "chrony"},
        "git_commit": _git_commit(os.getcwd()),
        # Set to "ssh-tunnel" by scripts/tunnel.sh. Recorded here so the report
        # can refuse to present throughput or latency from a tunnelled run as a
        # valid measurement — the path carries SSH crypto and TCP-over-TCP.
        "transport": os.environ.get("BENCH_TRANSPORT", "direct"),
        "transport_note": os.environ.get("BENCH_TRANSPORT_NOTE", ""),
        "target": {"address": cfg.target.address, "tls": cfg.target.tls,
                   "rpc_timeout_seconds": cfg.target.rpc_timeout_seconds},
        "auth": {"ingestion_queue": cfg.auth.ingestion_queue,
                 "subject": cfg.auth.subject},   # never the secret
        "workload": asdict(cfg.workload),
        "pipeline_model": asdict(cfg.pipeline),
        "reachability_warnings": list(cfg.warnings),
        "steps": [asdict(s) for s in cfg.steps],
        "data": {k: v for k, v in cfg.data.items()},
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)sZ %(levelname)-5s %(name)s %(message)s")
    logging.Formatter.converter = time.gmtime

    try:
        cfg = config_mod.load(args.config, args.profile, overrides={
            "target_rps": args.rps,
            "duration_seconds": args.duration_seconds,
            "workers": args.workers,
            "channels": args.channels,
            "batch_size": args.batch_size,
            "max_inflight": args.max_inflight,
            "retry_mode": args.retry_mode,
        })
    except config_mod.ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2

    test_id = args.test_id or f"{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{cfg.profile}"
    base_results = args.results_dir or cfg.output.get("results_dir") or "results"
    run_dir = os.path.join(base_results, test_id)

    # A dry run validates and connects but must leave no trace: preflight calls
    # this on every invocation, and writing a directory per preflight litters
    # results/ with empty "smoke" runs (observed: nine of them in one afternoon).
    if not args.dry_run:
        os.makedirs(run_dir, exist_ok=True)
        metadata = collect_metadata(cfg, test_id, sys.argv)
        with open(os.path.join(run_dir, "metadata.json"), "w") as fh:
            json.dump(metadata, fh, indent=2, default=str)

    log.info("test_id=%s profile=%s target=%s queue=%s",
             test_id, cfg.profile, cfg.target.address, cfg.auth.ingestion_queue)
    log.info("plan: %d step(s), %.0fs total, batch=%d workers=%d channels=%d",
             len(cfg.steps), cfg.total_duration_seconds, cfg.workload.batch_size,
             cfg.workload.workers, cfg.workload.channels)

    # A rate that cannot be offered produces a run that looks like the pipeline
    # saturated when in fact the client never asked for the load. Say so before
    # spending an hour on it, not afterwards in the analysis.
    for warning in cfg.warnings:
        log.warning("UNREACHABLE RATE — %s", warning)

    tokens = TokenSource(cfg.auth.secret_b64, cfg.auth.ingestion_queue,
                         cfg.auth.subject, cfg.auth.token_ttl_seconds)
    exporter = LogsExporter(cfg.target.address, cfg.workload.channels,
                            cfg.target.tls, tokens, cfg.target.rpc_timeout_seconds)

    if not exporter.wait_ready(timeout_s=15):
        log.error("gRPC channel to %s never became ready — is the collector running "
                  "and is port %d reachable?", cfg.target.address, cfg.target.port)
        exporter.close()
        return 4

    # Payload schema is config-driven so the generic `logs` path stays the
    # default and the rollback is "delete the key". ai_txn emits the
    # snx./req./res./llm. attribute set that v_ai_txn_transform reads; the
    # generic payload's gen_ai.* keys are read by nothing in that transform and
    # would land every column NULL.
    _schema = str((cfg.data or {}).get("schema", "logs")).lower()
    if _schema == "ai_txn":
        _customers = int((cfg.data or {}).get("customers", 100))
        factory = build_ai_txn_factory(cfg.data, cfg.workload.batch_size,
                                       cfg.workload.workers, customers=_customers)
        # The OTLP Resource is per export-request, so one template carries one
        # customer and the pool size caps how many customer partitions can ever
        # be written. Silently covering 32 of 100 would look like a successful
        # 100-customer run, so refuse rather than under-report.
        _warn = factory.coverage_warning()
        if _warn:
            log.error("ai_txn customer coverage: %s", _warn)
            exporter.close()
            return 5
        # Refuse to start rather than reject every batch at the wire for the
        # whole run. gRPC's 4 MiB cap is not negotiable and dazzleduck exposes
        # no setting for it, so batch_size * bytes-per-record is a hard bound.
        _over = factory.oversize_error()
        if _over:
            log.error("ai_txn batch too large for gRPC: %s", _over)
            exporter.close()
            return 6
    else:
        factory = build_factory(cfg.data, cfg.workload.batch_size, cfg.workload.workers)
    log.info("payload: %s", json.dumps(factory.describe()))

    if args.dry_run:
        log.info("dry run: configuration valid, channel ready, nothing sent")
        exporter.close()
        return 0

    metrics = Metrics(results_dir=run_dir, test_id=test_id,
                      sample_interval=float(cfg.output.get("sample_interval_seconds", 1)),
                      metrics_port=_int_or_none(cfg.output.get("metrics_port")))
    metrics.start_sampler()

    engine = LoadGenerator(cfg, exporter, factory, metrics)
    engine.start()

    interrupted = {"value": False}

    def _handle(signum, _frame):
        # A soak is expected to be stopped by hand. Stopping must still produce a
        # complete manifest, or the run is unusable for correctness checking.
        log.warning("signal %s received — draining and writing results",
                    signal.Signals(signum).name)
        interrupted["value"] = True
        engine._stop.set()                              # noqa: SLF001

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    started_wall = time.time()
    step_results: list[StepResult] = []
    try:
        for step in cfg.steps:
            if interrupted["value"]:
                break
            result = engine.run_step(step)
            step_results.append(result)
            log.info("step %d done: offered=%d accepted=%d rejected=%d "
                     "accepted_ratio=%.4f stalled=%.0fms",
                     result.index, result.offered, result.accepted, result.rejected,
                     result.accepted_ratio, result.stalled_ms)
            if _should_stop(cfg, result, metrics):
                log.warning("stop_when triggered at step %d — not escalating further",
                            result.index)
                break
    finally:
        engine.stop()
        metrics.stop_sampler()
        exporter.close()

    ended_wall = time.time()
    totals = metrics.snapshot()

    with open(os.path.join(run_dir, "generator.json"), "w") as fh:
        json.dump({"test_id": test_id, "profile": cfg.profile, "totals": totals,
                   "steps": [asdict(s) for s in step_results],
                   "abort_reason": engine.abort_reason,
                   "interrupted": interrupted["value"]},
                  fh, indent=2, default=str)

    manifest = {
        "test_id": test_id,
        "profile": cfg.profile,
        "generators": [{
            "gen_id": factory.gen_id,
            "seq_min": step_results[0].seq_start if step_results else 0,
            "seq_max": step_results[-1].seq_end if step_results else -1,
        }],
        "total_offered": totals["records_offered"],
        "total_accepted": totals["records_accepted"],
        "total_rejected": totals["records_rejected"],
        "requests_sent": totals["requests_sent"],
        "requests_failed": totals["requests_failed"],
        "errors_by_code": totals["errors_by_code"],
        "retry_mode": cfg.workload.retry_mode,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_wall)),
        "ended_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ended_wall)),
        "started_at_epoch": started_wall,
        "ended_at_epoch": ended_wall,
        "config_echo": metadata,
        # Stated explicitly so nobody has to infer it from the numbers later.
        "accounting_note": (
            "total_offered is what this process attempted. total_accepted is what "
            "the collector acked. They are different quantities; only accepted, "
            "held with a flat backlog, is throughput."),
    }
    with open(os.path.join(run_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2, default=str)

    _print_summary(test_id, run_dir, totals, step_results)

    if engine.abort_reason:
        return 5
    return 0


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _should_stop(cfg, result: StepResult, metrics: Metrics) -> bool:
    rules = cfg.stop_when or {}
    if not rules:
        return False
    if "accepted_ratio_below" in rules and result.offered:
        if result.accepted_ratio < float(rules["accepted_ratio_below"]):
            return True
    if "error_rate_above" in rules and result.requests:
        if result.failures / result.requests > float(rules["error_rate_above"]):
            return True
    if "latency_p99_ms_above" in rules:
        if metrics.snapshot()["latency_p99_ms"] > float(rules["latency_p99_ms_above"]):
            return True
    return False


def _print_summary(test_id, run_dir, totals, steps) -> None:
    print()
    print(f"  test id        {test_id}")
    print(f"  results        {run_dir}")
    print(f"  duration       {totals['duration_seconds']:.1f}s")
    print(f"  offered        {totals['records_offered']:,} records "
          f"({totals['offered_rps_mean']:,.0f}/s mean)")
    print(f"  accepted       {totals['records_accepted']:,} records "
          f"({totals['accepted_rps_mean']:,.0f}/s mean)")
    print(f"  rejected       {totals['records_rejected']:,} records")
    print(f"  requests       {totals['requests_sent']:,} "
          f"({totals['requests_failed']:,} failed, "
          f"{totals['success_rate'] * 100:.2f}% ok)")
    print(f"  latency ms     p50={totals['latency_p50_ms']:.1f} "
          f"p95={totals['latency_p95_ms']:.1f} p99={totals['latency_p99_ms']:.1f} "
          f"max={totals['latency_max_ms']:.1f}")
    print(f"  stalled        {totals['stalled_ms']:.0f} ms on the inflight bound")
    if totals["errors_by_code"]:
        print(f"  errors         {totals['errors_by_code']}")
    if len(steps) > 1:
        print()
        print("  step   target_rps    offered   accepted   rejected   ratio")
        for s in steps:
            print(f"  {s.index:>4}   {s.target_rps:>10,.0f} {s.offered:>10,} "
                  f"{s.accepted:>10,} {s.rejected:>10,}   {s.accepted_ratio:.4f}")
    print()


if __name__ == "__main__":
    sys.exit(main())
