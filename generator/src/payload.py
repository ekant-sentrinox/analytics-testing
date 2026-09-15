"""Deterministic OTLP log payload construction.

Two things make this more than a loop over `LogRecord()`:

1. **Cost.** Building a fresh protobuf with ~20 attributes per record, at tens of
   thousands of records a second, on two vCPUs, makes the generator the
   bottleneck — and then the benchmark measures Python. So a small pool of fully
   built `ExportLogsServiceRequest` templates is constructed once, and each send
   mutates only the fields that must be unique: `bench.seq` and the two
   timestamps. References to those exact submessages are captured at build time,
   so mutation is a field assignment, not a search.

2. **Provable correctness.** Every record carries `bench.gen_id` (this
   generator's UUID) and `bench.seq` (monotonic within the generator). Landed
   rows can then be reconciled against the manifest exactly: a gap is loss, a
   repeat is duplication. Without per-record identity, "zero data loss" is an
   assertion rather than a measurement.

The RNG is seeded, so the same seed produces a byte-identical stream of bodies,
services and attribute values across runs.
"""
from __future__ import annotations

import dataclasses
import random
import time
import uuid
from typing import Sequence

from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

_SEVERITY_NUMBER = {
    "TRACE": logs_pb2.SEVERITY_NUMBER_TRACE,
    "DEBUG": logs_pb2.SEVERITY_NUMBER_DEBUG,
    "INFO": logs_pb2.SEVERITY_NUMBER_INFO,
    "WARN": logs_pb2.SEVERITY_NUMBER_WARN,
    "ERROR": logs_pb2.SEVERITY_NUMBER_ERROR,
    "FATAL": logs_pb2.SEVERITY_NUMBER_FATAL,
}

_LOREM = (
    "request completed", "cache miss on key", "upstream latency elevated",
    "retrying downstream call", "token budget consumed", "payload validated",
    "connection pool checkout", "rate limit bucket refilled",
    "model inference finished", "streamed response flushed",
)


def _kv(key: str, value) -> common_pb2.KeyValue:
    av = common_pb2.AnyValue()
    if isinstance(value, bool):
        av.bool_value = value
    elif isinstance(value, int):
        av.int_value = value
    elif isinstance(value, float):
        av.double_value = value
    else:
        av.string_value = str(value)
    return common_pb2.KeyValue(key=key, value=av)


def _weighted(rng: random.Random, mapping: dict) -> str:
    keys = list(mapping)
    weights = [float(mapping[k]) for k in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


@dataclasses.dataclass
class RequestTemplate:
    """One prebuilt export request plus handles on its mutable fields."""
    request: logs_service_pb2.ExportLogsServiceRequest
    seq_values: list          # AnyValue for bench.seq, one per record
    records: list             # LogRecord, for timestamp stamping
    record_count: int
    approx_bytes: int

    def stamp(self, first_seq: int) -> int:
        """Assign a contiguous seq range and a fresh wall-clock time.

        Returns the seq immediately after the range, i.e. the caller's next
        starting point. Timestamps are set per send (not per template) because
        end-to-end visibility lag is measured from them; a stale timestamp would
        report lag that never happened.
        """
        now_ns = time.time_ns()
        seq = first_seq
        for value, record in zip(self.seq_values, self.records):
            value.int_value = seq
            record.time_unix_nano = now_ns
            record.observed_time_unix_nano = now_ns
            seq += 1
        return seq


class PayloadFactory:
    """Builds and owns the template pool for one generator instance.

    The pool is partitioned by worker: worker w owns
    templates[w*variants : (w+1)*variants] and no other thread touches them.
    stamp() mutates a template in place, so shared templates would mean two
    threads racing on the same protobuf and shipping records tagged with each
    other's sequence numbers.
    """

    def __init__(self, data: dict, batch_size: int, gen_id: str | None = None,
                 workers: int = 1, variants_per_worker: int = 4) -> None:
        self.gen_id = gen_id or str(uuid.uuid4())
        self.batch_size = int(batch_size)
        self.data = data or {}
        self.seed = int(self.data.get("seed", 42))
        self.workers = max(1, int(workers))
        self.variants_per_worker = max(1, int(variants_per_worker))
        self.templates: list[RequestTemplate] = []
        self._build_pool()

    # -- public ---------------------------------------------------------------

    def template_for(self, worker_id: int, variant: int) -> RequestTemplate:
        base = (worker_id % self.workers) * self.variants_per_worker
        return self.templates[base + (variant % self.variants_per_worker)]

    @property
    def approx_batch_bytes(self) -> int:
        return sum(t.approx_bytes for t in self.templates) // len(self.templates)

    def describe(self) -> dict:
        return {
            "gen_id": self.gen_id,
            "seed": self.seed,
            "batch_size": self.batch_size,
            "template_pool_size": len(self.templates),
            "variants_per_worker": self.variants_per_worker,
            "approx_batch_bytes": self.approx_batch_bytes,
            "model_traffic_enabled": bool(
                (self.data.get("model_traffic") or {}).get("enabled", False)),
        }

    # -- construction ---------------------------------------------------------

    def _build_pool(self) -> None:
        for i in range(self.workers * self.variants_per_worker):
            # Each template gets its own RNG stream so the pool is varied but
            # still fully determined by the configured seed.
            rng = random.Random(self.seed * 1_000_003 + i)
            self.templates.append(self._build_template(rng))

    def _build_template(self, rng: random.Random) -> RequestTemplate:
        services = self.data.get("service_names") or ["service"]
        envs = self.data.get("environments") or ["prod"]
        regions = self.data.get("regions") or ["us-west-2"]
        service = rng.choice(services)

        resource = resource_pb2.Resource(attributes=[
            _kv("service.name", service),
            _kv("deployment.environment", rng.choice(envs)),
            _kv("cloud.region", rng.choice(regions)),
            _kv("tenant.id", self.data.get("tenant_id", "testing-tenant")),
            _kv("workspace.id", self.data.get("workspace_id", "testing-workspace")),
            _kv("bench.gen_id", self.gen_id),
        ])

        prototypes = [self._build_record(rng, service) for _ in range(self.batch_size)]

        request = logs_service_pb2.ExportLogsServiceRequest(
            resource_logs=[logs_pb2.ResourceLogs(
                resource=resource,
                scope_logs=[logs_pb2.ScopeLogs(
                    scope=common_pb2.InstrumentationScope(
                        name="analytics-distributed-test", version="1.0.0"),
                    log_records=prototypes,
                )],
            )]
        )

        # Assigning into a repeated field COPIES. Re-acquire handles from inside
        # the assembled request, or stamp() would mutate detached prototypes and
        # every record would ship with seq 0.
        live = list(request.resource_logs[0].scope_logs[0].log_records)
        live_seq = [self._find_seq_value(r) for r in live]

        return RequestTemplate(
            request=request,
            seq_values=live_seq,
            records=live,
            record_count=len(live),
            approx_bytes=request.ByteSize(),
        )

    @staticmethod
    def _find_seq_value(record) -> common_pb2.AnyValue:
        for attr in record.attributes:
            if attr.key == "bench.seq":
                return attr.value
        raise RuntimeError("bench.seq attribute missing from generated record — "
                           "correctness validation would be impossible")

    def _build_record(self, rng: random.Random, service: str) -> logs_pb2.LogRecord:
        sev_name = _weighted(rng, self.data.get("severity_mix") or {"INFO": 1.0})
        status = int(_weighted(rng, self.data.get("http_status_mix") or {200: 1.0}))
        latency_ms = self._sample_latency(rng)

        attributes = [
            _kv("bench.seq", 0),                 # mutated per send
            _kv("bench.gen_id", self.gen_id),
            _kv("http.response.status_code", status),
            _kv("http.request.method", rng.choice(["GET", "POST", "PUT", "DELETE"])),
            _kv("http.route", f"/v1/{service}/{rng.choice(['items', 'query', 'batch'])}"),
            _kv("request.id", f"{rng.getrandbits(64):016x}"),
            _kv("duration_ms", latency_ms),
            _kv("error", status >= 400),
        ]
        attributes.extend(self._model_attributes(rng))

        body = self._build_body(rng)
        record = logs_pb2.LogRecord(
            time_unix_nano=0,
            observed_time_unix_nano=0,
            severity_number=_SEVERITY_NUMBER.get(sev_name, logs_pb2.SEVERITY_NUMBER_INFO),
            severity_text=sev_name,
            body=common_pb2.AnyValue(string_value=body),
            trace_id=rng.getrandbits(128).to_bytes(16, "big"),
            span_id=rng.getrandbits(64).to_bytes(8, "big"),
            flags=1,
            # No event_name: LogRecord.event_name does not exist in the proto
            # version either side is built against. The collector's schema has
            # an `event_name` column but LogRecordConverter hardcodes it to null
            # ("field added in proto > 1.3.2"), so the column is always NULL and
            # setting it here would only raise.
            attributes=attributes,
        )
        return record

    def _sample_latency(self, rng: random.Random) -> int:
        spec = self.data.get("latency_ms") or {}
        p50 = float(spec.get("p50", 100))
        p95 = float(spec.get("p95", 500))
        p99 = float(spec.get("p99", 1500))
        top = float(spec.get("max", 10000))
        u = rng.random()
        if u < 0.50:
            return int(rng.uniform(1, p50))
        if u < 0.95:
            return int(rng.uniform(p50, p95))
        if u < 0.99:
            return int(rng.uniform(p95, p99))
        return int(rng.uniform(p99, top))

    def _model_attributes(self, rng: random.Random) -> list:
        cfg = self.data.get("model_traffic") or {}
        if not cfg.get("enabled"):
            return []
        models = cfg.get("models") or ["unknown-model"]
        model = rng.choice(models)
        tin = cfg.get("input_tokens") or {"min": 100, "max": 4000}
        tout = cfg.get("output_tokens") or {"min": 20, "max": 1000}
        input_tokens = rng.randint(int(tin["min"]), int(tin["max"]))
        output_tokens = rng.randint(int(tout["min"]), int(tout["max"]))

        pricing = (cfg.get("pricing") or {}).get(model) or {}
        in_rate = float(pricing.get("input", 0.0))
        out_rate = float(pricing.get("output", 0.0))
        request_cost = input_tokens / 1_000_000 * in_rate
        response_cost = output_tokens / 1_000_000 * out_rate

        return [
            _kv("gen_ai.request.model", model),
            _kv("gen_ai.usage.input_tokens", input_tokens),
            _kv("gen_ai.usage.output_tokens", output_tokens),
            _kv("gen_ai.usage.total_tokens", input_tokens + output_tokens),
            _kv("gen_ai.cost.request_usd", round(request_cost, 8)),
            _kv("gen_ai.cost.response_usd", round(response_cost, 8)),
            _kv("gen_ai.cost.total_usd", round(request_cost + response_cost, 8)),
        ]

    def _build_body(self, rng: random.Random) -> str:
        target = int(self.data.get("body_bytes", 256))
        head = rng.choice(_LOREM)
        # Pad deterministically to the configured size. Random hex rather than a
        # repeated character so Parquet compression ratios stay realistic — a
        # run of 'x' would compress to nothing and flatter every ZSTD number.
        pad_len = max(0, target - len(head) - 1)
        if pad_len == 0:
            return head
        pad = "".join(rng.choice("0123456789abcdef") for _ in range(pad_len))
        return f"{head} {pad}"


def build_factory(data: dict, batch_size: int, workers: int,
                  gen_id: str | None = None) -> PayloadFactory:
    return PayloadFactory(data=data, batch_size=batch_size, workers=workers, gen_id=gen_id)


def seq_ranges_overlap(ranges: Sequence[tuple[int, int]]) -> bool:
    """True if any two [lo, hi] seq ranges intersect. Used by the validator."""
    ordered = sorted(ranges)
    return any(ordered[i][1] >= ordered[i + 1][0] for i in range(len(ordered) - 1))
