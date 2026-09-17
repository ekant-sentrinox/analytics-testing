"""OTLP log payloads shaped for the ollylake `ai_txn` transform.

This is NOT the generic bench payload. Every key here is read by
`v_ai_txn_transform`; anything it does not read is dropped silently, and
anything it reads but we spell differently lands as NULL. The contract below is
taken from R__ai_txn_transform.sql, not from sample/gateway_event/claude.ndjson
-- that sample uses the `sntx.` prefix and targets the retired V1
`transaction_log` path. The live transform reads `snx.`.

Four things are easy to get wrong and expensive to discover late:

1. **`log_type` is load-bearing.** event_type is derived ONLY from it:
       'http'          -> 'llm_call'
       'mcp_tool_call' -> 'mcp_tool_call'
       anything else   -> NULL
   and `ai_txn.event_type` is NOT NULL. A missing or misspelled log_type does
   not produce bad rows, it fails the insert for the whole batch.

2. **Latencies are SECONDS, not milliseconds.** The transform does
   `ROUND(CAST(x AS DOUBLE) * 1000)` for snx.latency, snx.gateway_latency,
   llm.ttft and llm.retry_after. Emitting 820 instead of 0.820 yields a
   latency of 820 000 ms and nothing complains.

3. **customer_id lives in RESOURCE attributes, not record attributes** -- and
   the OTLP Resource is per export-request, not per record. So one batch is one
   customer, and customer spread comes from the TEMPLATE POOL: template i is
   assigned customer (i % customers) + 1. It is also `NULLIF(..., 0)`-guarded in
   the transform, so customer_id 0 silently falls back to the tenant lookup --
   ids start at 1 deliberately.

4. **res.token_usage is a JSON STRING**, parsed by the llm_* macros for tokens
   AND cast to JSON for provider_usage. It is the only source of input/output
   tokens and therefore of estimated_cost.

`bench.seq` and `bench.gen_id` are kept even though the transform ignores them:
the correctness validator reconciles landed rows against the manifest by seq to
prove zero loss / zero duplication, and losing that would turn a measurement
back into an assertion.
"""
from __future__ import annotations

import dataclasses
import json
import random
import time
import uuid

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

# Anthropic stop reasons, emitted the way the gateway emits them: a JSON array
# string. The transform pulls the first quoted token out with
# regexp_extract(..., '"([^"]+)"', 1), so a bare word would extract to NULL.
_STOP_REASONS = ['["end_turn"]', '["max_tokens"]', '["stop_sequence"]', '["tool_use"]']

_USER_AGENTS = [
    "anthropic-sdk-python/0.40.0", "anthropic-sdk-typescript/0.32.1",
    "openai-python/1.54.0", "langchain/0.3.7", "llamaindex/0.11.20",
]
_MCP_TOOLS = ["read_file", "search_docs", "run_query", "fetch_url", "list_tables"]
_MCP_SERVERS = ["filesystem", "postgres", "web-search", "github"]


def _kv(key: str, value) -> common_pb2.KeyValue:
    """All values are emitted as STRINGS.

    The transform TRY_CASTs every attribute out of a MAP(VARCHAR, VARCHAR), so
    the collector's otel_log stores them as text regardless. Emitting an OTLP
    int_value here would still arrive as text, but the two paths stringify
    differently for floats (1.0 vs 1) and that difference reaches TRY_CAST. One
    representation, chosen explicitly, avoids the whole class of problem.
    """
    av = common_pb2.AnyValue(string_value=str(value))
    return common_pb2.KeyValue(key=key, value=av)


def _weighted(rng: random.Random, mapping: dict) -> str:
    keys = list(mapping)
    return rng.choices(keys, weights=[float(mapping[k]) for k in keys], k=1)[0]


@dataclasses.dataclass
class RequestTemplate:
    request: logs_service_pb2.ExportLogsServiceRequest
    seq_values: list
    records: list
    record_count: int
    approx_bytes: int
    customer_id: int          # which customer partition this template feeds

    def stamp(self, first_seq: int) -> int:
        """Stamp bench.seq AND snx.sequence_id with the same monotonic value.

        Both, because they serve different readers. bench.seq survives only as
        far as otel_log; the transform does not project it, so it cannot prove
        anything about ai_txn. snx.sequence_id is the ONLY generator-controlled
        field that reaches ai_txn (as transaction_id), which makes it the sole
        basis for reconciling landed rows against the manifest -- a gap is loss,
        a repeat is duplication. Leaving it random, as the first draft did, meant
        zero-loss could be asserted but never measured.
        """
        now_ns = time.time_ns()
        seq = first_seq
        for (bench_v, snx_v), record in zip(self.seq_values, self.records):
            s = str(seq)
            bench_v.string_value = s
            snx_v.string_value = s
            record.time_unix_nano = now_ns
            record.observed_time_unix_nano = now_ns
            seq += 1
        return seq


class AiTxnPayloadFactory:
    """Template pool for ai_txn-shaped traffic across N customers.

    Interface-compatible with PayloadFactory so loadgen/main can swap on a
    config flag without further changes.
    """

    def __init__(self, data: dict, batch_size: int, gen_id: str | None = None,
                 workers: int = 1, variants_per_worker: int = 4,
                 customers: int = 100) -> None:
        self.gen_id = gen_id or str(uuid.uuid4())
        self.batch_size = int(batch_size)
        self.data = data or {}
        self.seed = int(self.data.get("seed", 42))
        self.workers = max(1, int(workers))
        self.variants_per_worker = max(1, int(variants_per_worker))
        self.customers = max(1, int(customers))
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
        covered = sorted({t.customer_id for t in self.templates})
        return {
            "gen_id": self.gen_id,
            "seed": self.seed,
            "batch_size": self.batch_size,
            "template_pool_size": len(self.templates),
            "variants_per_worker": self.variants_per_worker,
            "approx_batch_bytes": self.approx_batch_bytes,
            "schema": "ai_txn",
            "customers_configured": self.customers,
            "customers_covered": len(covered),
            "customer_id_range": [covered[0], covered[-1]] if covered else [],
        }

    # gRPC refuses any message over 4 MiB. The server does not negotiate it up and
    # dazzleduck exposes no setting, so this is a hard ceiling on batch_size *
    # bytes-per-record. Exceeding it fails at the WIRE: the collector still
    # reports HEALTHY, batchesProcessed stays 0, and the generator counts every
    # record as rejected -- a failure mode that looks like a pipeline problem and
    # is actually a payload-sizing one.
    GRPC_MAX_MESSAGE_BYTES = 4 * 1024 * 1024

    def oversize_error(self) -> str | None:
        """Non-None when a built batch would be refused by gRPC."""
        size = self.approx_batch_bytes
        if size >= self.GRPC_MAX_MESSAGE_BYTES:
            per = size // max(1, self.batch_size)
            safe = int(self.GRPC_MAX_MESSAGE_BYTES * 0.75) // max(1, per)
            return (f"batch of {self.batch_size} records is ~{size:,} bytes "
                    f"(~{per:,} B/record), over gRPC's {self.GRPC_MAX_MESSAGE_BYTES:,} "
                    f"byte limit. Every batch would be rejected with "
                    f"RESOURCE_EXHAUSTED before reaching the collector. "
                    f"Set batch_size <= {safe} for this payload.")
        return None

    def coverage_warning(self) -> str | None:
        """Pool smaller than the customer count means partitions never written.

        workers * variants_per_worker templates carry one customer each, so a
        pool of 32 can only ever reach 32 of 100 customers -- and the run would
        quietly produce 32 partitions while reporting it was testing 100.
        """
        pool = self.workers * self.variants_per_worker
        if pool < self.customers:
            return (f"template pool is {pool} but {self.customers} customers are "
                    f"configured: only {pool} customer partitions will receive "
                    f"traffic. Raise workers or variants_per_worker so "
                    f"workers*variants_per_worker >= customers.")
        return None

    # -- construction ---------------------------------------------------------

    def _build_pool(self) -> None:
        for i in range(self.workers * self.variants_per_worker):
            rng = random.Random(self.seed * 1_000_003 + i)
            # Round-robin so customers stay evenly covered for any pool size,
            # and +1 because the transform treats customer_id 0 as "unset".
            self.templates.append(self._build_template(rng, (i % self.customers) + 1))

    def _build_template(self, rng: random.Random, customer_id: int) -> RequestTemplate:
        regions = self.data.get("regions") or ["us-west-2"]
        # tenant_id is derived from the customer so the two stay consistent per
        # partition; sgwe_id models a small gateway fleet per region.
        resource = resource_pb2.Resource(attributes=[
            _kv("customer_id", customer_id),
            _kv("tenant_id", customer_id),
            _kv("sgwe_id", (customer_id % 8) + 1),
            _kv("cloud.region", rng.choice(regions)),
            _kv("service.name", "sentrinox-gateway-prod"),
            _kv("bench.gen_id", self.gen_id),
        ])

        prototypes = [self._build_record(rng, customer_id)
                      for _ in range(self.batch_size)]

        request = logs_service_pb2.ExportLogsServiceRequest(
            resource_logs=[logs_pb2.ResourceLogs(
                resource=resource,
                scope_logs=[logs_pb2.ScopeLogs(
                    scope=common_pb2.InstrumentationScope(
                        name="sentrinox.llm.usage", version="0.1.0"),
                    log_records=prototypes,
                )],
            )]
        )

        # Assigning into a repeated field COPIES; re-acquire handles from the
        # assembled request or stamp() mutates detached prototypes.
        live = list(request.resource_logs[0].scope_logs[0].log_records)
        return RequestTemplate(
            request=request,
            seq_values=[self._find_seq_value(r) for r in live],
            records=live,
            record_count=len(live),
            approx_bytes=request.ByteSize(),
            customer_id=customer_id,
        )

    @staticmethod
    def _find_seq_value(record):
        """Handles on both sequence fields, as a (bench.seq, snx.sequence_id) pair."""
        bench_v = snx_v = None
        for attr in record.attributes:
            if attr.key == "bench.seq":
                bench_v = attr.value
            elif attr.key == "snx.sequence_id":
                snx_v = attr.value
        if bench_v is None or snx_v is None:
            raise RuntimeError(
                "bench.seq and snx.sequence_id must both be present — "
                "snx.sequence_id is the only one that reaches ai_txn, so without "
                "it loss and duplication cannot be measured")
        return bench_v, snx_v

    def _build_record(self, rng: random.Random, customer_id: int) -> logs_pb2.LogRecord:
        mcp_ratio = float(self.data.get("mcp_tool_call_ratio", 0.15))
        is_mcp = rng.random() < mcp_ratio

        sev_name = _weighted(rng, self.data.get("severity_mix") or {"INFO": 1.0})
        status = int(_weighted(rng, self.data.get("http_status_mix") or {200: 1.0}))
        latency_s = self._sample_latency_seconds(rng)

        model = rng.choice((self.data.get("model_traffic") or {}).get(
            "models", ["claude-opus-5"]))

        attrs = [
            # Correctness identity — ignored by the transform, required by the validator.
            _kv("bench.seq", 0),
            _kv("bench.gen_id", self.gen_id),

            # THE gate. Without this, event_type is NULL and the NOT NULL insert fails.
            _kv("log_type", "mcp_tool_call" if is_mcp else "http"),

            # Stamped per send with the monotonic seq, not random: this is the
            # only generator field that survives the transform into ai_txn.
            _kv("snx.sequence_id", 0),
            _kv("snx.workspace_id", (customer_id * 10) + rng.randint(1, 3)),
            _kv("snx.user_id", (customer_id * 100) + rng.randint(1, 50)),
            _kv("snx.vkey_id", (customer_id * 10) + rng.randint(1, 5)),
            _kv("snx.agent_id", rng.randint(1, 12)),
            _kv("snx.provider_id", 1),
            _kv("snx.model_id", 1 + rng.randint(0, 2)),
            _kv("snx.cfg_ver", 7),
            # SECONDS. The transform multiplies by 1000.
            _kv("snx.latency", f"{latency_s:.3f}"),
            _kv("snx.gateway_latency", f"{latency_s * 0.02:.3f}"),
            _kv("snx.overall_action",
                _weighted(rng, {"PACT_ALLOW": 0.94, "PACT_CAUTION": 0.05, "PACT_BLOCK": 0.01})),

            _kv("req.method", "POST"),
            _kv("req.domain", "api.anthropic.com"),
            _kv("req.path", "/v1/messages"),
            _kv("req.model", model),
            _kv("req.source_ip", f"10.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}"),
            _kv("req.source_port", rng.randint(20000, 65000)),
            _kv("req.ua", rng.choice(_USER_AGENTS)),
            _kv("req.dbytes", rng.randint(800, 8000)),
            _kv("req.an-reqid", f"msg_{rng.getrandbits(64):016x}"),
            _kv("req.prompt", self._build_prompt(rng)),

            _kv("res.status", status),
            _kv("res.dbytes", rng.randint(200, 6000)),
            _kv("res.c-type", "application/json"),
            _kv("res.sse", "true" if rng.random() < 0.3 else "false"),
            _kv("res.service_tier", rng.choice(["standard", "priority", "batch"])),
        ]

        # Tokens/cost only exist on a successful LLM call. res.token_usage is the
        # sole source of input_tokens, output_tokens and estimated_cost.
        if not is_mcp and 200 <= status < 300:
            tin = (self.data.get("model_traffic") or {}).get("input_tokens") or {"min": 100, "max": 8000}
            tout = (self.data.get("model_traffic") or {}).get("output_tokens") or {"min": 20, "max": 2000}
            usage = {
                "input_tokens": rng.randint(int(tin["min"]), int(tin["max"])),
                "output_tokens": rng.randint(int(tout["min"]), int(tout["max"])),
                "cache_creation_input_tokens": rng.randint(0, 2000) if rng.random() < 0.25 else 0,
                "cache_read_input_tokens": rng.randint(0, 8000) if rng.random() < 0.35 else 0,
            }
            attrs.append(_kv("res.token_usage", json.dumps(usage)))
            attrs.append(_kv("llm.stop_reason", rng.choice(_STOP_REASONS)))
            attrs.append(_kv("llm.ttft", f"{latency_s * rng.uniform(0.1, 0.4):.3f}"))

        if status == 429:
            attrs.append(_kv("llm.retry_after", f"{rng.uniform(0.5, 30.0):.3f}"))
            attrs.append(_kv("llm.rl.limit_requests", 4000))
            attrs.append(_kv("llm.rl.remaining_requests", rng.randint(0, 50)))
            attrs.append(_kv("res.an-rlit", 400000))
            attrs.append(_kv("llm.rl.remaining_tokens", rng.randint(0, 5000)))

        attrs.append(_kv("llm.user_prompt_suppressed", "false"))
        attrs.append(_kv("llm.fallback_triggered", "true" if rng.random() < 0.02 else "false"))

        if is_mcp:
            attrs.append(_kv("mcp.tool.name", rng.choice(_MCP_TOOLS)))
            attrs.append(_kv("mcp.server.name", rng.choice(_MCP_SERVERS)))
            attrs.append(_kv("mcp.call_seq", rng.randint(1, 20)))
            attrs.append(_kv("mcp.session_id", f"sess_{rng.getrandbits(48):012x}"))
            if status >= 400:
                attrs.append(_kv("mcp.tool.error_code", rng.choice(["TIMEOUT", "NOT_FOUND", "DENIED"])))

        return logs_pb2.LogRecord(
            time_unix_nano=0,
            observed_time_unix_nano=0,
            severity_number=_SEVERITY_NUMBER.get(sev_name, logs_pb2.SEVERITY_NUMBER_INFO),
            severity_text=sev_name,
            body=common_pb2.AnyValue(string_value=""),
            trace_id=rng.getrandbits(128).to_bytes(16, "big"),
            span_id=rng.getrandbits(64).to_bytes(8, "big"),
            flags=1,
            attributes=attrs,
        )

    def _sample_latency_seconds(self, rng: random.Random) -> float:
        spec = self.data.get("latency_ms") or {}
        p50, p95 = float(spec.get("p50", 120)), float(spec.get("p95", 800))
        p99, top = float(spec.get("p99", 2500)), float(spec.get("max", 15000))
        u = rng.random()
        if u < 0.50:
            ms = rng.uniform(1, p50)
        elif u < 0.95:
            ms = rng.uniform(p50, p95)
        elif u < 0.99:
            ms = rng.uniform(p95, p99)
        else:
            ms = rng.uniform(p99, top)
        return ms / 1000.0

    def _build_prompt(self, rng: random.Random) -> str:
        target = int(self.data.get("body_bytes", 256))
        head = "Summarize the following document in three bullet points."
        pad_len = max(0, target - len(head) - 1)
        if pad_len == 0:
            return head
        # Random hex, as in the generic payload: a repeated character would
        # compress to nothing and flatter every Parquet ratio in the report.
        pad = "".join(rng.choice("0123456789abcdef") for _ in range(pad_len))
        return f"{head} {pad}"


def build_factory(data: dict, batch_size: int, workers: int,
                  gen_id: str | None = None, customers: int = 100) -> AiTxnPayloadFactory:
    return AiTxnPayloadFactory(data=data, batch_size=batch_size, workers=workers,
                               gen_id=gen_id, customers=customers)
