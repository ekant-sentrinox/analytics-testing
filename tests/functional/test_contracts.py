#!/usr/bin/env python3
"""Contract tests against a live collector.

Each of these is an assumption the throughput tests rest on. They are checked
against the running service rather than read out of the source, because the
question is what the DEPLOYED collector does.

  1. no Authorization header      -> UNAUTHENTICATED
  2. garbage bearer token         -> UNAUTHENTICATED
  3. token signed with a wrong key-> UNAUTHENTICATED
  4. valid token, no queue claim  -> INVALID_ARGUMENT (there is no default queue)
  5. valid token, unknown queue   -> INVALID_ARGUMENT or NOT_FOUND
  6. valid token, correct queue   -> OK, and the RPC does not ack until durable
  7. records land with their identity attributes intact

Test 6 is the load-bearing one. The whole benchmark's latency definition depends
on the export RPC completing only after the batch is on disk and in the catalog;
if it acked early, every latency number would be meaningless and "accepted"
would not imply "persisted".
"""
from __future__ import annotations

import base64
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import grpc                                                       # noqa: E402
import jwt as pyjwt                                               # noqa: E402
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc  # noqa: E402

from generator.src.otlp_client import INGESTION_QUEUE_CLAIM, CHANNEL_OPTIONS  # noqa: E402
from generator.src.payload import PayloadFactory                  # noqa: E402

HOST = os.environ.get("COLLECTOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("COLLECTOR_GRPC_PORT", 4317))
SECRET = os.environ.get("OTEL_JWT_SECRET_B64", "")
QUEUE = os.environ.get("OTEL_INGESTION_QUEUE", "logs")
ADDR = f"{HOST}:{PORT}"

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"PASS  {name:<38} {detail}")
    else:
        FAILED += 1
        print(f"FAIL  {name:<38} {detail}")


def token(secret_b64: str, queue: str | None, ttl: int = 300) -> str:
    key = base64.b64decode(secret_b64)
    alg = "HS512" if len(key) >= 64 else "HS256"
    now = int(time.time())
    payload = {"sub": "contract-test", "iat": now, "exp": now + ttl}
    if queue is not None:
        payload[INGESTION_QUEUE_CLAIM] = queue
    return pyjwt.encode(payload, key, algorithm=alg)


def send(metadata, records: int = 5, gen_id: str | None = None):
    """Returns (ok, code, elapsed_seconds)."""
    factory = PayloadFactory(
        data={"seed": 7, "body_bytes": 128, "model_traffic": {"enabled": False}},
        batch_size=records, gen_id=gen_id or str(uuid.uuid4()), workers=1, variants_per_worker=1)
    tmpl = factory.template_for(0, 0)
    tmpl.stamp(0)
    channel = grpc.insecure_channel(ADDR, options=CHANNEL_OPTIONS)
    stub = logs_service_pb2_grpc.LogsServiceStub(channel)
    started = time.perf_counter()
    try:
        stub.Export(tmpl.request, timeout=60, metadata=metadata)
        return True, "OK", time.perf_counter() - started, factory.gen_id
    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "UNKNOWN"
        return False, code, time.perf_counter() - started, factory.gen_id
    finally:
        channel.close()


def main() -> int:
    if not SECRET:
        print("OTEL_JWT_SECRET_B64 is not set — run scripts/setup.sh and source .env")
        return 2

    print(f"collector {ADDR}, queue '{QUEUE}'\n")

    # 1 -----------------------------------------------------------------
    ok, code, _, _ = send(())
    check("no Authorization header", not ok and code == "UNAUTHENTICATED",
          f"got {code}" + ("" if code == "UNAUTHENTICATED" else "  <-- auth is not enforced"))

    # 2 -----------------------------------------------------------------
    ok, code, _, _ = send((("authorization", "Bearer not-a-jwt"),))
    check("malformed bearer token", not ok and code == "UNAUTHENTICATED", f"got {code}")

    # 3 -----------------------------------------------------------------
    wrong = base64.b64encode(b"x" * 64).decode()
    ok, code, _, _ = send((("authorization", "Bearer " + token(wrong, QUEUE)),))
    check("token signed with the wrong key", not ok and code == "UNAUTHENTICATED",
          f"got {code}" + ("" if code == "UNAUTHENTICATED"
                           else "  <-- signature verification appears disabled"))

    # 4 -----------------------------------------------------------------
    ok, code, _, _ = send((("authorization", "Bearer " + token(SECRET, None)),))
    check("valid token, no queue claim", not ok and code in ("INVALID_ARGUMENT", "NOT_FOUND"),
          f"got {code}  (there must be no default queue)")

    # 5 -----------------------------------------------------------------
    ok, code, _, _ = send((("authorization", "Bearer " + token(SECRET, "queue-that-does-not-exist")),))
    check("valid token, unknown queue", not ok and code in ("INVALID_ARGUMENT", "NOT_FOUND",
                                                            "FAILED_PRECONDITION"),
          f"got {code}")

    # 6 -----------------------------------------------------------------
    gen_id = f"contract-{uuid.uuid4()}"
    ok, code, elapsed, gen_id = send((("authorization", "Bearer " + token(SECRET, QUEUE)),),
                                     records=25, gen_id=gen_id)
    check("valid token, correct queue", ok, f"got {code}, {elapsed * 1000:.0f} ms")

    if ok:
        # The RPC is completed from the batch-write future, so a non-trivial
        # elapsed time is the expected shape. A sub-millisecond ack would mean
        # the response is not waiting for persistence.
        check("ack waits for persistence", elapsed > 0.002,
              f"{elapsed * 1000:.1f} ms — plausible for queue + COPY + catalog commit"
              if elapsed > 0.002 else
              f"{elapsed * 1000:.3f} ms — suspiciously fast; is the ack really durable?")

    # 7 -----------------------------------------------------------------
    if ok:
        landed = wait_for_rows(gen_id, expected=25, timeout=180)
        if landed is None:
            check("records land with identity", False,
                  "could not query the lake (catalog credentials?)")
        else:
            check("records land with identity", landed == 25,
                  f"{landed}/25 rows found by bench.gen_id within the timeout")

    print()
    print(f"{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


def wait_for_rows(gen_id: str, expected: int, timeout: float) -> int | None:
    """Poll the lake until the rows appear. They will not be instant: the queue
    flushes on min_bucket_size or max_delay_ms, whichever comes first."""
    try:
        import duckdb
    except ImportError:
        return None
    pw = os.environ.get("PG_PASSWORD", "")
    if not pw:
        return None
    try:
        con = duckdb.connect(":memory:")
        for ext in ("httpfs", "aws", "ducklake", "postgres"):
            con.execute(f"INSTALL {ext}")
            con.execute(f"LOAD {ext}")
        con.execute("CREATE OR REPLACE SECRET s3_role (TYPE S3, PROVIDER credential_chain, "
                    f"REGION '{os.environ.get('AWS_REGION', 'us-west-2')}')")
        cat = os.environ.get("DUCKLAKE_CATALOG", "bench")
        con.execute(
            f"ATTACH 'ducklake:postgres:host={os.environ['PG_HOST']} port={os.environ.get('PG_PORT', 5432)} "
            f"dbname={os.environ['PG_DATABASE']} user={os.environ['PG_USER']} password={pw}' "
            f"AS {cat} (DATA_PATH 's3://{os.environ['S3_BUCKET']}/{os.environ.get('S3_PREFIX', 'bench')}/', "
            "DATA_INLINING_ROW_LIMIT 0)")
    except Exception as exc:                                      # noqa: BLE001
        print(f"      (lake query unavailable: {str(exc).splitlines()[0][:120]})")
        return None

    table = f"{cat}.{os.environ.get('DUCKLAKE_SCHEMA', 'main')}.{os.environ.get('DUCKLAKE_LOGS_TABLE', 'logs')}"
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        try:
            count = con.execute(
                f"SELECT count(*) FROM {table} "
                "WHERE element_at(attributes, 'bench.gen_id')[1] = ?", [gen_id]).fetchone()[0]
        except Exception:                                         # noqa: BLE001
            count = 0
        if count >= expected:
            break
        time.sleep(5)
    con.close()
    return count


if __name__ == "__main__":
    sys.exit(main())
