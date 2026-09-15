"""The open-loop load engine.

The control model is the whole point, so it is worth stating plainly:

    The pacer decides when a batch SHOULD be submitted, from the target rate and
    nothing else. It never waits for the server. If every worker is busy, the
    pacer records the time it was blocked (`stalled_ms`) and carries the deficit
    forward — it does not quietly lower the offered rate.

A closed-loop generator (submit, wait for ack, submit again) cannot measure
saturation: its offered rate is defined by the server's speed, so the server
always looks like it is keeping up. That is the flaw this design exists to
avoid, and `stalled_ms` is the honest admission of the one place the open loop
is bounded — `max_inflight`.

Rate accounting is deliberately three-way; see metrics.py.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from .config import GeneratorConfig, Step
from .metrics import Metrics
from .otlp_client import ExportResult, LogsExporter
from .payload import PayloadFactory

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    index: int
    target_rps: float
    duration_seconds: float
    started_at: float
    ended_at: float
    offered: int = 0
    accepted: int = 0
    rejected: int = 0
    requests: int = 0
    failures: int = 0
    stalled_ms: float = 0.0
    seq_start: int = 0
    seq_end: int = 0
    errors_by_code: dict = field(default_factory=dict)

    @property
    def accepted_ratio(self) -> float:
        return self.accepted / self.offered if self.offered else 0.0


@dataclass
class _WorkItem:
    seq_start: int
    record_count: int
    nbytes: int
    variant: int          # which of the owning worker's private templates to use


class LoadGenerator:
    """Owns the worker pool and executes a sequence of steps."""

    def __init__(self, cfg: GeneratorConfig, exporter: LogsExporter,
                 factory: PayloadFactory, metrics: Metrics) -> None:
        self.cfg = cfg
        self.exporter = exporter
        self.factory = factory
        self.metrics = metrics

        self._stop = threading.Event()
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue(
            maxsize=max(1, cfg.workload.max_inflight))
        self._workers: list[threading.Thread] = []
        self._seq_lock = threading.Lock()
        self._next_seq = 0
        self._consecutive_failures = 0
        self._abort_reason: str | None = None

        # Per-step accumulators, swapped under _step_lock at each boundary.
        self._step_lock = threading.Lock()
        self._step: StepResult | None = None

        # Templates are mutated in place by stamp(), so each worker must own its
        # own slice of the pool. Sharing one pool across workers would let two
        # threads stamp the same protobuf concurrently and ship records whose
        # bench.seq belongs to another batch — silently breaking exactly the
        # correctness check the seq numbers exist for.
        self._variants = factory.variants_per_worker
        if len(factory.templates) < cfg.workload.workers * self._variants:
            raise ValueError("payload template pool is smaller than workers x variants")

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        for i in range(self.cfg.workload.workers):
            t = threading.Thread(target=self._worker_loop, args=(i,),
                                 name=f"worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)
        log.info("started %d workers over %d channels",
                 len(self._workers), self.cfg.workload.channels)

    def stop(self) -> None:
        self._stop.set()
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        for t in self._workers:
            t.join(timeout=self.cfg.target.rpc_timeout_seconds + 5)

    @property
    def abort_reason(self) -> str | None:
        return self._abort_reason

    # -- execution ------------------------------------------------------------

    def run_step(self, step: Step) -> StepResult:
        """Hold `step.rps` for `step.duration_seconds`, then return its totals."""
        batch = self.cfg.workload.batch_size
        batches_per_second = step.rps / batch
        if batches_per_second <= 0:
            raise ValueError(f"step {step.index}: rate {step.rps} with batch {batch} yields no work")
        interval = 1.0 / batches_per_second

        with self._seq_lock:
            seq_at_start = self._next_seq
        result = StepResult(index=step.index, target_rps=step.rps,
                            duration_seconds=step.duration_seconds,
                            started_at=time.time(), ended_at=0.0,
                            seq_start=seq_at_start)
        with self._step_lock:
            self._step = result
        self.metrics.set_step(step.index, step.rps)

        log.info("step %d: %s rec/s for %.0fs (%.2f batches/s, batch=%d)",
                 step.index, f"{step.rps:,.0f}", step.duration_seconds,
                 batches_per_second, batch)

        started = time.monotonic()
        deadline = started + step.duration_seconds
        next_submit = started
        variant = 0
        # Any template has the same record count and roughly the same size, so
        # the pacer can size the item without knowing which worker takes it.
        probe = self.factory.template_for(0, 0)

        while not self._stop.is_set():
            now = time.monotonic()
            if now >= deadline:
                break

            sleep_for = next_submit - now
            if sleep_for > 0:
                # Interruptible so SIGINT does not have to wait out the interval.
                if self._stop.wait(min(sleep_for, 0.25)):
                    break
                if sleep_for > 0.25:
                    continue

            with self._seq_lock:
                seq_start = self._next_seq
                self._next_seq += batch

            item = _WorkItem(seq_start=seq_start,
                             record_count=probe.record_count,
                             nbytes=probe.approx_bytes,
                             variant=variant)
            variant = (variant + 1) % self._variants

            stall_started = time.monotonic()
            enqueued = False
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    self._queue.put(item, timeout=0.1)
                    enqueued = True
                    break
                except queue.Full:
                    continue
            stalled = time.monotonic() - stall_started
            if stalled > 0.001:
                self.metrics.record_stall(stalled)
                with self._step_lock:
                    result.stalled_ms += stalled * 1000.0

            if not enqueued:
                # Never submitted, so it was never offered. Give the seq range
                # back so the manifest's contiguity claim stays true.
                with self._seq_lock:
                    if self._next_seq == seq_start + batch:
                        self._next_seq = seq_start
                break

            self.metrics.record_offered(item.record_count, item.nbytes)
            with self._step_lock:
                result.offered += item.record_count

            # Advance the schedule by exactly one interval, not "now + interval".
            # Anchoring to the ideal timeline is what makes a lost interval show
            # up as catch-up rather than as a permanently lower offered rate.
            next_submit += interval
            if next_submit < now - 1.0:
                # More than a second behind: the box cannot pace this rate at all.
                # Re-anchor rather than spin, and let stalled_ms carry the story.
                next_submit = now

            if self._abort_reason:
                break

        self._drain_inflight(deadline_s=self.cfg.target.rpc_timeout_seconds + 10)

        with self._step_lock:
            result.ended_at = time.time()
            with self._seq_lock:
                result.seq_end = max(result.seq_start, self._next_seq - 1)
            self._step = None
        return result

    def _drain_inflight(self, deadline_s: float) -> None:
        """Let outstanding RPCs complete so a step's accepted count is real.

        Without this the tail of every step is attributed to the next one, and a
        staircase's per-level accounting silently smears across the boundary.
        Waits on the queue AND on in-flight RPCs — an empty queue with 60
        outstanding exports is not a drained step.
        """
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            if self._queue.empty() and self.metrics.inflight() == 0:
                return
            time.sleep(0.05)
        log.warning("step drain timed out after %.0fs with %d in flight; "
                    "late acks will be attributed to the next step",
                    deadline_s, self.metrics.inflight())

    # -- workers --------------------------------------------------------------

    def _worker_loop(self, worker_id: int) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                self._send(worker_id, item)
            except Exception:                                     # noqa: BLE001
                log.exception("worker %d: unhandled error", worker_id)
            finally:
                self._queue.task_done()

    def _send(self, worker_id: int, item: _WorkItem) -> None:
        # This worker's private template — never touched by another thread.
        template = self.factory.template_for(worker_id, item.variant)
        template.stamp(item.seq_start)

        self.metrics.record_inflight(1)
        try:
            result = self.exporter.export(worker_id, template.request)
        finally:
            self.metrics.record_inflight(-1)

        if result.ok:
            self.metrics.record_success(item.record_count, result.latency_s)
            self._consecutive_failures = 0
            with self._step_lock:
                if self._step:
                    self._step.accepted += item.record_count
                    self._step.requests += 1
            return

        self._record_failure(item, result)

    def _record_failure(self, item: _WorkItem, result: ExportResult) -> None:
        self.metrics.record_failure(item.record_count, result.latency_s, result.code)
        with self._step_lock:
            if self._step:
                self._step.requests += 1
                self._step.failures += 1
                self._step.errors_by_code[result.code] = \
                    self._step.errors_by_code.get(result.code, 0) + 1
                if result.code == "RESOURCE_EXHAUSTED":
                    self._step.rejected += item.record_count

        if result.code == "RESOURCE_EXHAUSTED":
            # R7. Rejection is a measurement, not a transient to be papered over.
            # 'polite' mode exists only because the soak definition may require a
            # well-behaved client; it is recorded in the manifest either way.
            if self.cfg.workload.retry_mode == "polite" and result.retry_after_s > 0:
                self._stop.wait(min(result.retry_after_s, 5.0))
            return

        self._consecutive_failures += 1
        limit = self.cfg.workload.abort_on_consecutive_failures
        if limit and self._consecutive_failures >= limit and not self._abort_reason:
            self._abort_reason = (
                f"{self._consecutive_failures} consecutive failures, "
                f"last={result.code}: {result.detail}")
            log.error("aborting: %s", self._abort_reason)
            self._stop.set()
