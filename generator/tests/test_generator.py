#!/usr/bin/env python3
"""Unit tests for the generator. No network, no collector, no AWS.

These cover the parts where a bug would corrupt a measurement rather than crash:

  * config precedence and validation
  * seq stamping — every record gets a unique, contiguous sequence number
  * determinism — the same seed produces the same payload bytes
  * per-worker template isolation — the bug that would silently mislabel records
  * percentile arithmetic
  * three-way rate accounting staying separate

Run with:  python -m pytest generator/tests/ -q
      or:  python generator/tests/test_generator.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from generator.src import config as cfgmod           # noqa: E402
from generator.src.metrics import Metrics, percentile  # noqa: E402
from generator.src.payload import PayloadFactory     # noqa: E402

MINIMAL = textwrap.dedent("""
    generator:
      target: { host: "10.0.0.1", port: 4317, tls: false, rpc_timeout_seconds: 30 }
      auth:
        secret_b64: "AAAA"
        ingestion_queue: logs
        subject: test
        token_ttl_seconds: 600
      workload:
        target_rps: 100
        duration_seconds: 60
        workers: 2
        channels: 1
        batch_size: 10
        max_inflight: 8
        retry_mode: none
      data: { seed: 42, body_bytes: 64, model_traffic: { enabled: false } }
      output: { results_dir: results, sample_interval_seconds: 1 }
    profiles:
      smoke:
        workload: { target_rps: 10, duration_seconds: 5 }
      stairs:
        steps:
          - { rps: 100, duration_seconds: 10 }
          - { rps: 200, duration_seconds: 10 }
""")


def write_config(text=MINIMAL):
    fh = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    fh.write(text)
    fh.close()
    return fh.name


# --- config -----------------------------------------------------------------

def test_profile_overlays_defaults():
    cfg = cfgmod.load(write_config(), "smoke")
    assert cfg.workload.target_rps == 10, "profile should override the default rate"
    assert cfg.workload.batch_size == 10, "unspecified keys should fall through to defaults"


def test_cli_beats_profile():
    cfg = cfgmod.load(write_config(), "smoke", overrides={"target_rps": 999})
    assert cfg.workload.target_rps == 999


def test_unknown_profile_is_rejected():
    try:
        cfgmod.load(write_config(), "nope")
    except cfgmod.ConfigError as exc:
        assert "nope" in str(exc)
        return
    raise AssertionError("an unknown profile must not be accepted")


def test_workers_may_not_exceed_max_inflight():
    bad = MINIMAL.replace("workers: 2", "workers: 32")
    try:
        cfgmod.load(write_config(bad), "smoke")
    except cfgmod.ConfigError as exc:
        assert "max_inflight" in str(exc)
        return
    raise AssertionError("workers > max_inflight makes the inflight bound unobservable "
                         "and must be rejected")


def test_steps_are_parsed():
    cfg = cfgmod.load(write_config(), "stairs")
    assert len(cfg.steps) == 2
    assert cfg.steps[1].rps == 200
    assert cfg.total_duration_seconds == 20


def test_unexpanded_host_is_rejected():
    bad = MINIMAL.replace('host: "10.0.0.1"', 'host: "${COLLECTOR_HOST}"')
    os.environ.pop("COLLECTOR_HOST", None)
    try:
        cfgmod.load(write_config(bad), "smoke")
    except cfgmod.ConfigError as exc:
        assert "COLLECTOR_HOST" in str(exc)
        return
    raise AssertionError("an unexpanded ${VAR} must fail loudly, not resolve to a literal host")


# --- payload ----------------------------------------------------------------

def make_factory(workers=2, variants=2, batch=8):
    return PayloadFactory(
        data={"seed": 42, "body_bytes": 64, "model_traffic": {"enabled": False},
              "service_names": ["a", "b"], "severity_mix": {"INFO": 1.0},
              "http_status_mix": {200: 1.0}},
        batch_size=batch, workers=workers, variants_per_worker=variants)


def seqs_of(template):
    return [v.int_value for v in template.seq_values]


def test_stamp_assigns_contiguous_sequence():
    f = make_factory()
    t = f.template_for(0, 0)
    nxt = t.stamp(1000)
    assert seqs_of(t) == list(range(1000, 1008)), seqs_of(t)
    assert nxt == 1008, "stamp must return the next free seq"


def test_stamp_is_visible_in_the_serialized_request():
    f = make_factory()
    t = f.template_for(0, 0)
    t.stamp(500)
    records = t.request.resource_logs[0].scope_logs[0].log_records
    found = []
    for rec in records:
        for attr in rec.attributes:
            if attr.key == "bench.seq":
                found.append(attr.value.int_value)
    assert found == list(range(500, 508)), (
        f"mutation did not reach the request being sent: {found}")


def test_stamp_sets_timestamps():
    f = make_factory()
    t = f.template_for(0, 0)
    t.stamp(0)
    assert all(r.time_unix_nano > 0 for r in t.records)
    assert all(r.observed_time_unix_nano > 0 for r in t.records)


def test_workers_do_not_share_templates():
    """The bug this guards: two workers stamping the same protobuf would ship
    records tagged with each other's sequence numbers, and the correctness
    validator would report loss that never happened."""
    f = make_factory(workers=3, variants=2)
    seen = set()
    for w in range(3):
        for v in range(2):
            t = f.template_for(w, v)
            assert id(t) not in seen, f"worker {w} variant {v} shares a template"
            seen.add(id(t))
    assert len(seen) == 6

    a = f.template_for(0, 0)
    b = f.template_for(1, 0)
    a.stamp(0)
    b.stamp(9000)
    assert seqs_of(a)[0] == 0 and seqs_of(b)[0] == 9000, "templates are not independent"


def test_same_seed_same_bytes():
    a = make_factory()
    b = make_factory()
    for w in range(2):
        for v in range(2):
            ta, tb = a.template_for(w, v), b.template_for(w, v)
            ta.stamp(0)
            tb.stamp(0)
            # Timestamps are stamped from the wall clock, so compare the parts
            # that are supposed to be deterministic.
            ba = [r.body.string_value for r in ta.records]
            bb = [r.body.string_value for r in tb.records]
            assert ba == bb, "the same seed must produce the same payload"


def test_different_seed_different_bytes():
    a = make_factory()
    b = PayloadFactory(data={"seed": 43, "body_bytes": 64,
                             "model_traffic": {"enabled": False}},
                       batch_size=8, workers=2, variants_per_worker=2)
    ba = [r.body.string_value for r in a.template_for(0, 0).records]
    bb = [r.body.string_value for r in b.template_for(0, 0).records]
    assert ba != bb


def test_every_record_carries_identity():
    f = make_factory()
    t = f.template_for(0, 0)
    for rec in t.records:
        keys = {a.key for a in rec.attributes}
        assert "bench.seq" in keys and "bench.gen_id" in keys


def test_model_attributes_and_cost():
    f = PayloadFactory(
        data={"seed": 1, "body_bytes": 32, "model_traffic": {
            "enabled": True, "models": ["claude-opus-5"],
            "input_tokens": {"min": 1000, "max": 1000},
            "output_tokens": {"min": 100, "max": 100},
            "pricing": {"claude-opus-5": {"input": 5.0, "output": 25.0}}}},
        batch_size=1, workers=1, variants_per_worker=1)
    attrs = {a.key: a.value for a in f.template_for(0, 0).records[0].attributes}
    assert attrs["gen_ai.usage.input_tokens"].int_value == 1000
    assert attrs["gen_ai.usage.total_tokens"].int_value == 1100
    # 1000/1e6 * 5.00 = 0.005 ; 100/1e6 * 25.00 = 0.0025
    assert abs(attrs["gen_ai.cost.total_usd"].double_value - 0.0075) < 1e-9


def test_body_size_is_respected():
    f = PayloadFactory(data={"seed": 1, "body_bytes": 512,
                             "model_traffic": {"enabled": False}},
                       batch_size=2, workers=1, variants_per_worker=1)
    for rec in f.template_for(0, 0).records:
        assert len(rec.body.string_value) == 512


# --- metrics ----------------------------------------------------------------

def test_percentile_edges():
    assert percentile([], 0.5) == 0.0
    assert percentile([7.0], 0.99) == 7.0
    vals = [float(i) for i in range(1, 101)]
    assert percentile(vals, 0.50) in (50.0, 51.0)
    assert percentile(vals, 0.99) in (99.0, 100.0)


def test_rates_stay_separate():
    """offered, accepted and rejected are three different quantities and the
    accumulator must never conflate them."""
    with tempfile.TemporaryDirectory() as d:
        m = Metrics(results_dir=d, test_id="t", sample_interval=60, metrics_port=None)
        m.record_offered(1000, 5000)
        m.record_success(600, 0.01)
        m.record_failure(400, 0.02, "RESOURCE_EXHAUSTED")
        snap = m.snapshot()
        assert snap["records_offered"] == 1000
        assert snap["records_accepted"] == 600
        assert snap["records_rejected"] == 400
        assert snap["requests_sent"] == 2
        assert snap["requests_failed"] == 1
        assert snap["errors_by_code"]["RESOURCE_EXHAUSTED"] == 1
        m.stop_sampler()


def test_non_exhausted_failures_are_not_counted_as_rejected():
    with tempfile.TemporaryDirectory() as d:
        m = Metrics(results_dir=d, test_id="t", sample_interval=60, metrics_port=None)
        m.record_offered(100, 500)
        m.record_failure(100, 1.0, "DEADLINE_EXCEEDED")
        snap = m.snapshot()
        assert snap["records_rejected"] == 0, (
            "only RESOURCE_EXHAUSTED is rejection; a deadline is a failure and may "
            "still have landed")
        assert snap["failed_deadline"] == 1
        m.stop_sampler()


def test_inflight_tracking():
    with tempfile.TemporaryDirectory() as d:
        m = Metrics(results_dir=d, test_id="t", sample_interval=60, metrics_port=None)
        assert m.inflight() == 0
        m.record_inflight(3)
        assert m.inflight() == 3
        m.record_inflight(-3)
        assert m.inflight() == 0
        m.stop_sampler()


def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:                                  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
