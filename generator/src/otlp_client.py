"""gRPC transport and JWT minting for the OTLP logs exporter.

The collector's JwtServerInterceptor requires a signed Bearer token carrying the
`x-dd-ingestion-queue` claim (Headers.CLAIM_INGESTION_QUEUE). There is no default
queue: a token without that claim is rejected with INVALID_ARGUMENT, not routed
somewhere sensible. The claim value must match an `ingestion_queue` in the
collector's ingestion_queue_table_mapping.

The secret is the same base64 HMAC key the collector is configured with. jjwt
sizes the algorithm to the key, so a 64-byte key is signed HS512 here; anything
shorter falls back to HS256 and will be rejected by a collector configured with
a longer key, which is the correct failure.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass

import grpc
import jwt as pyjwt
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc

log = logging.getLogger(__name__)

INGESTION_QUEUE_CLAIM = "x-dd-ingestion-queue"

# gRPC channel options tuned for many small-to-medium unary calls over a LAN.
# Large max message size because a 500-record batch with model attributes is
# comfortably over the 4 MiB default once bodies are a few hundred bytes.
CHANNEL_OPTIONS = [
    ("grpc.max_send_message_length", 64 * 1024 * 1024),
    ("grpc.max_receive_message_length", 16 * 1024 * 1024),
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    # Off: the generator must not smooth over a slow server by reordering or
    # coalescing. Every RPC is submitted when the pacer says so.
    ("grpc.enable_retries", 0),
]


class TokenSource:
    """Mints and refreshes the Bearer token.

    Refreshed at 75% of its TTL. A soak run outlives any sane token lifetime, and
    a mid-run flood of UNAUTHENTICATED would look exactly like a collector fault
    in the results.
    """

    def __init__(self, secret_b64: str, queue: str, subject: str, ttl_seconds: int) -> None:
        try:
            self._key = base64.b64decode(secret_b64, validate=True)
        except Exception as exc:                                  # noqa: BLE001
            raise ValueError("generator.auth.secret_b64 is not valid base64") from exc
        if len(self._key) < 32:
            raise ValueError(
                f"HMAC key is {len(self._key)} bytes; need at least 32. "
                "Regenerate with scripts/gen-secret.sh")
        self._alg = "HS512" if len(self._key) >= 64 else "HS256"
        self._queue = queue
        self._subject = subject
        self._ttl = max(60, int(ttl_seconds))
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    @property
    def algorithm(self) -> str:
        return self._alg

    def bearer(self) -> str:
        now = time.time()
        with self._lock:
            if now < self._expires_at:
                return self._token
            issued = int(now)
            payload = {
                "sub": self._subject,
                "iat": issued,
                "exp": issued + self._ttl,
                INGESTION_QUEUE_CLAIM: self._queue,
            }
            self._token = "Bearer " + pyjwt.encode(payload, self._key, algorithm=self._alg)
            self._expires_at = now + self._ttl * 0.75
            log.debug("minted %s token for queue=%s ttl=%ss", self._alg, self._queue, self._ttl)
            return self._token


@dataclass
class ExportResult:
    ok: bool
    latency_s: float
    code: str = "OK"
    detail: str = ""
    retry_after_s: float = 0.0


class LogsExporter:
    """A pool of channels with a LogsService stub on each."""

    def __init__(self, address: str, channels: int, tls: bool,
                 token_source: TokenSource, timeout_s: float) -> None:
        self.address = address
        self.timeout_s = timeout_s
        self._tokens = token_source
        creds = grpc.ssl_channel_credentials() if tls else None
        self._channels = [
            (grpc.secure_channel(address, creds, options=CHANNEL_OPTIONS) if tls
             else grpc.insecure_channel(address, options=CHANNEL_OPTIONS))
            for _ in range(max(1, channels))
        ]
        self._stubs = [logs_service_pb2_grpc.LogsServiceStub(ch) for ch in self._channels]

    def wait_ready(self, timeout_s: float = 10.0) -> bool:
        try:
            grpc.channel_ready_future(self._channels[0]).result(timeout=timeout_s)
            return True
        except grpc.FutureTimeoutError:
            return False

    def export(self, stub_index: int, request) -> ExportResult:
        stub = self._stubs[stub_index % len(self._stubs)]
        metadata = (("authorization", self._tokens.bearer()),)
        started = time.perf_counter()
        try:
            stub.Export(request, timeout=self.timeout_s, metadata=metadata)
            return ExportResult(ok=True, latency_s=time.perf_counter() - started)
        except grpc.RpcError as exc:
            latency = time.perf_counter() - started
            code = exc.code().name if exc.code() else "UNKNOWN"
            return ExportResult(
                ok=False, latency_s=latency, code=code,
                detail=(exc.details() or "")[:200],
                retry_after_s=_retry_delay_seconds(exc),
            )

    def close(self) -> None:
        for channel in self._channels:
            channel.close()


def _retry_delay_seconds(exc: grpc.RpcError) -> float:
    """Pull the server's RetryInfo delay out of the trailing metadata.

    The collector attaches one to RESOURCE_EXHAUSTED when pending write bytes
    exceed max_pending_write. Honoured only in 'polite' retry mode; recorded
    either way, since how long the server wanted us to back off is a
    measurement of how far behind it was.
    """
    try:
        for key, value in (exc.trailing_metadata() or ()):
            if key.endswith("google.rpc.retryinfo-bin"):
                from google.rpc import error_details_pb2
                info = error_details_pb2.RetryInfo()
                info.ParseFromString(value)
                return info.retry_delay.seconds + info.retry_delay.nanos / 1e9
    except Exception:                                             # noqa: BLE001
        pass
    return 0.0
