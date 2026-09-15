"""Generator metrics: counters, latency percentiles, per-second JSONL, /metrics.

Three rates are tracked separately and never added together, because conflating
them is the single easiest way to publish a wrong throughput number:

    offered   — records the generator attempted to submit
    accepted  — records the collector acked OK
    rejected  — records in RPCs that came back RESOURCE_EXHAUSTED

`offered` is a property of this process. Only `accepted`, sustained with a flat
backlog, is throughput.

Latency is measured around the whole export RPC. That call does not return until
the batch is durable on the collector side, so what is recorded here is queue
wait + Parquet write + catalog commit — not network time.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _PROM = True
except ImportError:                                        # optional dependency
    _PROM = False


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile. Empty input is 0.0, not an exception."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(1, min(len(sorted_values), int(round(q * len(sorted_values) + 0.5))))
    return sorted_values[rank - 1]


@dataclass
class Totals:
    """Cumulative counters for the whole run. Guarded by Metrics._lock."""
    requests_sent: int = 0
    requests_success: int = 0
    requests_failed: int = 0
    records_offered: int = 0
    records_accepted: int = 0
    records_rejected: int = 0
    failed_resource_exhausted: int = 0
    failed_deadline: int = 0
    failed_unavailable: int = 0
    failed_unauthenticated: int = 0
    failed_other: int = 0
    bytes_sent: int = 0
    stalled_ms: float = 0.0
    errors_by_code: dict = field(default_factory=dict)


class Metrics:
    """Thread-safe accumulator plus a 1 Hz sampler thread."""

    def __init__(self, results_dir: str, test_id: str, sample_interval: float = 1.0,
                 metrics_port: int | None = None) -> None:
        self.results_dir = results_dir
        self.test_id = test_id
        self.sample_interval = max(0.1, float(sample_interval))
        self._lock = threading.Lock()
        self.totals = Totals()

        self._window_latencies: list[float] = []
        self._window = Totals()
        self._all_latencies: list[float] = []
        self._inflight = 0
        self._step_index = 0
        self._step_rps = 0.0

        self.started_at = time.time()
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None

        os.makedirs(results_dir, exist_ok=True)
        self._jsonl_path = os.path.join(results_dir, "generator.jsonl")
        self._csv_path = os.path.join(results_dir, "generator.csv")
        self._jsonl = open(self._jsonl_path, "a", buffering=1)
        new_csv = not os.path.exists(self._csv_path) or os.path.getsize(self._csv_path) == 0
        self._csv_fh = open(self._csv_path, "a", newline="", buffering=1)
        self._csv = csv.writer(self._csv_fh)
        if new_csv:
            self._csv.writerow(SAMPLE_COLUMNS)

        self._prom = _PromMetrics(metrics_port) if (_PROM and metrics_port) else None

    # -- recording ------------------------------------------------------------

    def set_step(self, index: int, rps: float) -> None:
        with self._lock:
            self._step_index = index
            self._step_rps = rps

    def record_offered(self, records: int, nbytes: int) -> None:
        with self._lock:
            self.totals.records_offered += records
            self.totals.bytes_sent += nbytes
            self._window.records_offered += records
            self._window.bytes_sent += nbytes
        if self._prom:
            self._prom.offered.inc(records)
            self._prom.bytes_sent.inc(nbytes)

    def record_inflight(self, delta: int) -> None:
        with self._lock:
            self._inflight += delta
        if self._prom:
            self._prom.inflight.inc(delta)

    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    def record_stall(self, seconds: float) -> None:
        ms = seconds * 1000.0
        with self._lock:
            self.totals.stalled_ms += ms
            self._window.stalled_ms += ms
        if self._prom:
            self._prom.stalled_ms.inc(ms)

    def record_success(self, records: int, latency_s: float) -> None:
        ms = latency_s * 1000.0
        with self._lock:
            self.totals.requests_sent += 1
            self.totals.requests_success += 1
            self.totals.records_accepted += records
            self._window.requests_sent += 1
            self._window.requests_success += 1
            self._window.records_accepted += records
            self._window_latencies.append(ms)
            self._all_latencies.append(ms)
        if self._prom:
            self._prom.requests.labels(result="success").inc()
            self._prom.accepted.inc(records)
            self._prom.latency.observe(latency_s)

    def record_failure(self, records: int, latency_s: float, code: str) -> None:
        ms = latency_s * 1000.0
        with self._lock:
            self.totals.requests_sent += 1
            self.totals.requests_failed += 1
            self._window.requests_sent += 1
            self._window.requests_failed += 1
            self.totals.errors_by_code[code] = self.totals.errors_by_code.get(code, 0) + 1
            if code == "RESOURCE_EXHAUSTED":
                self.totals.records_rejected += records
                self.totals.failed_resource_exhausted += 1
                self._window.records_rejected += records
                self._window.failed_resource_exhausted += 1
            elif code == "DEADLINE_EXCEEDED":
                self.totals.failed_deadline += 1
            elif code == "UNAVAILABLE":
                self.totals.failed_unavailable += 1
            elif code == "UNAUTHENTICATED":
                self.totals.failed_unauthenticated += 1
            else:
                self.totals.failed_other += 1
            self._window_latencies.append(ms)
            self._all_latencies.append(ms)
        if self._prom:
            self._prom.requests.labels(result="failure").inc()
            self._prom.errors.labels(code=code).inc()
            self._prom.latency.observe(latency_s)

    # -- sampling -------------------------------------------------------------

    def start_sampler(self) -> None:
        self._sampler = threading.Thread(target=self._sample_loop, name="sampler", daemon=True)
        self._sampler.start()

    def stop_sampler(self) -> None:
        self._stop.set()
        if self._sampler:
            self._sampler.join(timeout=5)
        self._flush_sample()          # capture the partial final window
        self._jsonl.close()
        self._csv_fh.close()

    def _sample_loop(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            next_tick += self.sample_interval
            delay = next_tick - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                return
            self._flush_sample()

    def _flush_sample(self) -> None:
        with self._lock:
            window, latencies = self._window, sorted(self._window_latencies)
            self._window, self._window_latencies = Totals(), []
            inflight, step_index, step_rps = self._inflight, self._step_index, self._step_rps
            totals = asdict(self.totals)

        elapsed = self.sample_interval
        sample = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "test_id": self.test_id,
            "step_index": step_index,
            "step_target_rps": step_rps,
            "offered_rps": window.records_offered / elapsed,
            "accepted_rps": window.records_accepted / elapsed,
            "rejected_rps": window.records_rejected / elapsed,
            "requests_sent": window.requests_sent,
            "requests_success": window.requests_success,
            "requests_failed": window.requests_failed,
            "failed_resource_exhausted": window.failed_resource_exhausted,
            "bytes_sent": window.bytes_sent,
            "inflight": inflight,
            "stalled_ms": round(window.stalled_ms, 3),
            "latency_p50_ms": round(percentile(latencies, 0.50), 3),
            "latency_p95_ms": round(percentile(latencies, 0.95), 3),
            "latency_p99_ms": round(percentile(latencies, 0.99), 3),
            "latency_max_ms": round(latencies[-1], 3) if latencies else 0.0,
            "cum_records_offered": totals["records_offered"],
            "cum_records_accepted": totals["records_accepted"],
            "cum_records_rejected": totals["records_rejected"],
        }
        self._jsonl.write(json.dumps(sample) + "\n")
        self._csv.writerow([sample[c] for c in SAMPLE_COLUMNS])
        if self._prom:
            self._prom.offered_rps.set(sample["offered_rps"])
            self._prom.accepted_rps.set(sample["accepted_rps"])
            self._prom.rejected_rps.set(sample["rejected_rps"])

    # -- final ----------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            totals = asdict(self.totals)
            latencies = sorted(self._all_latencies)
        duration = max(1e-9, time.time() - self.started_at)
        totals.update({
            "duration_seconds": round(duration, 3),
            "offered_rps_mean": totals["records_offered"] / duration,
            "accepted_rps_mean": totals["records_accepted"] / duration,
            "rejected_rps_mean": totals["records_rejected"] / duration,
            "latency_p50_ms": round(percentile(latencies, 0.50), 3),
            "latency_p95_ms": round(percentile(latencies, 0.95), 3),
            "latency_p99_ms": round(percentile(latencies, 0.99), 3),
            "latency_min_ms": round(latencies[0], 3) if latencies else 0.0,
            "latency_max_ms": round(latencies[-1], 3) if latencies else 0.0,
            "success_rate": (totals["requests_success"] / totals["requests_sent"]
                             if totals["requests_sent"] else 0.0),
        })
        return totals


SAMPLE_COLUMNS = [
    "ts", "iso", "test_id", "step_index", "step_target_rps",
    "offered_rps", "accepted_rps", "rejected_rps",
    "requests_sent", "requests_success", "requests_failed",
    "failed_resource_exhausted", "bytes_sent", "inflight", "stalled_ms",
    "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "latency_max_ms",
    "cum_records_offered", "cum_records_accepted", "cum_records_rejected",
]


class _PromMetrics:
    """Prometheus exposition on the configured port. Optional by design: a
    missing prometheus_client degrades to JSONL-only, it does not stop a run."""

    def __init__(self, port: int) -> None:
        self.offered = Counter("bench_generator_records_offered_total",
                               "Records the generator attempted to submit")
        self.accepted = Counter("bench_generator_records_accepted_total",
                                "Records acked OK by the collector")
        self.requests = Counter("bench_generator_requests_total",
                                "Export RPCs", ["result"])
        self.errors = Counter("bench_generator_errors_total",
                              "Failed export RPCs by gRPC status code", ["code"])
        self.bytes_sent = Counter("bench_generator_bytes_sent_total",
                                  "Serialized request bytes offered")
        self.stalled_ms = Counter("bench_generator_stalled_ms_total",
                                  "Milliseconds the pacer was blocked on the inflight bound")
        self.inflight = Gauge("bench_generator_inflight",
                              "Export RPCs currently outstanding")
        self.offered_rps = Gauge("bench_generator_offered_rps", "Offered records/sec, last sample")
        self.accepted_rps = Gauge("bench_generator_accepted_rps", "Accepted records/sec, last sample")
        self.rejected_rps = Gauge("bench_generator_rejected_rps", "Rejected records/sec, last sample")
        self.latency = Histogram(
            "bench_generator_export_latency_seconds",
            "End-to-end export RPC latency (includes Parquet write and catalog commit)",
            buckets=(.01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 120))
        start_http_server(port)
