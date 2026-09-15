"""Configuration loading for the generator.

One source of truth: config/generator.yaml. A profile name selects a block under
`profiles:` which is overlaid on `generator.workload`. Command-line flags overlay
on top of that, so precedence is

    CLI  >  profile  >  generator.workload defaults

Everything is validated up front. A generator that starts with a nonsense rate
and discovers it 40 minutes into a soak has wasted the run.
"""
from __future__ import annotations

import dataclasses
import math
import os
import re
from typing import Any, Iterable

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Raised for any invalid or missing configuration value."""


def _expand(node: Any) -> Any:
    if isinstance(node, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), node)
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    return node


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclasses.dataclass(frozen=True)
class Step:
    """One rung of a staircase: hold `rps` for `duration_seconds`."""
    index: int
    rps: float
    duration_seconds: float


@dataclasses.dataclass(frozen=True)
class Target:
    host: str
    port: int
    tls: bool
    rpc_timeout_seconds: float

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclasses.dataclass(frozen=True)
class Auth:
    secret_b64: str
    ingestion_queue: str
    subject: str
    token_ttl_seconds: int


@dataclasses.dataclass(frozen=True)
class Workload:
    target_rps: float
    duration_seconds: float
    ramp_up_seconds: float
    ramp_down_seconds: float
    workers: int
    channels: int
    batch_size: int
    max_inflight: int
    retry_mode: str
    abort_on_consecutive_failures: int


@dataclasses.dataclass(frozen=True)
class Pipeline:
    """What the generator needs to know about the collector to sanity-check a rate.

    Not used to talk to it — only to predict whether a target rate is reachable
    at all. Mirror of config/collector.yaml; if you change the collector's flush
    settings, change these too.
    """
    max_delay_ms: float
    min_bucket_size: int
    approx_record_bytes: int

    def flush_seconds(self, offered_rps: float) -> float:
        """How long a bucket takes to flush at this offered rate.

        A bucket flushes on whichever comes first: it reaches min_bucket_size,
        or max_delay_ms elapses. At low rates the timer always wins.
        """
        if offered_rps <= 0:
            return self.max_delay_ms / 1000.0
        seconds_to_fill = self.min_bucket_size / (offered_rps * self.approx_record_bytes)
        return min(self.max_delay_ms / 1000.0, seconds_to_fill)

    def max_reachable_rps(self, workers: int, batch_size: int, offered_rps: float) -> float:
        """Ceiling imposed by concurrency against flush latency.

        The export RPC does not ack until the batch is durable, so a worker is
        blocked for the whole flush. With `workers` workers each carrying
        `batch_size` records, the most that can be in flight per flush is
        workers x batch_size, and that empties once per flush interval:

            max_rps = workers * batch_size / flush_seconds

        This is a real property of the pipeline, not a generator artefact —
        any client that waits for durability faces the same bound.
        """
        return workers * batch_size / max(1e-9, self.flush_seconds(offered_rps))


@dataclasses.dataclass(frozen=True)
class GeneratorConfig:
    profile: str
    description: str
    target: Target
    auth: Auth
    workload: Workload
    steps: tuple[Step, ...]
    data: dict
    output: dict
    stop_when: dict
    fault: dict
    checkpoint_interval_seconds: float
    pipeline: Pipeline
    warnings: tuple[str, ...] = ()

    @property
    def is_staircase(self) -> bool:
        return len(self.steps) > 1 or (len(self.steps) == 1 and self.steps[0].rps
                                       != self.workload.target_rps)

    @property
    def total_duration_seconds(self) -> float:
        return sum(s.duration_seconds for s in self.steps)


def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"{where}.{key} is required but missing or empty")
    return mapping[key]


def _positive(value: Any, where: str, allow_zero: bool = False) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} must be a number, got {value!r}") from exc
    if num < 0 or (num == 0 and not allow_zero):
        raise ConfigError(f"{where} must be > 0, got {num}")
    return num


def load(path: str, profile: str, overrides: dict | None = None) -> GeneratorConfig:
    """Read `path`, overlay `profile`, then `overrides`, and validate."""
    with open(path) as fh:
        raw = _expand(yaml.safe_load(fh) or {})

    gen = raw.get("generator")
    if not isinstance(gen, dict):
        raise ConfigError(f"{path}: missing top-level 'generator' mapping")

    profiles = raw.get("profiles") or {}
    if profile not in profiles:
        known = ", ".join(sorted(profiles)) or "(none defined)"
        raise ConfigError(f"unknown profile {profile!r}; available: {known}")
    prof = profiles[profile] or {}

    workload = _merge(gen.get("workload") or {}, prof.get("workload") or {})
    workload = _merge(workload, {k: v for k, v in (overrides or {}).items() if v is not None})

    tgt = gen.get("target") or {}
    target = Target(
        host=str(_require(tgt, "host", "generator.target")),
        port=int(_require(tgt, "port", "generator.target")),
        tls=bool(tgt.get("tls", False)),
        rpc_timeout_seconds=_positive(tgt.get("rpc_timeout_seconds", 60),
                                      "generator.target.rpc_timeout_seconds"),
    )
    if "${" in target.host:
        raise ConfigError(f"generator.target.host did not expand: {target.host!r} "
                          "— is COLLECTOR_HOST set in .env?")

    au = gen.get("auth") or {}
    secret = str(_require(au, "secret_b64", "generator.auth"))
    if "${" in secret:
        raise ConfigError("generator.auth.secret_b64 did not expand — run scripts/gen-secret.sh")
    auth = Auth(
        secret_b64=secret,
        ingestion_queue=str(_require(au, "ingestion_queue", "generator.auth")),
        subject=str(au.get("subject", "bench-generator")),
        token_ttl_seconds=int(au.get("token_ttl_seconds", 3600)),
    )

    retry_mode = str(workload.get("retry_mode", "none")).lower()
    if retry_mode not in ("none", "polite"):
        raise ConfigError(f"workload.retry_mode must be 'none' or 'polite', got {retry_mode!r}")

    wl = Workload(
        target_rps=_positive(workload.get("target_rps", 0), "workload.target_rps"),
        duration_seconds=_positive(workload.get("duration_seconds", 60),
                                   "workload.duration_seconds"),
        ramp_up_seconds=_positive(workload.get("ramp_up_seconds", 0),
                                  "workload.ramp_up_seconds", allow_zero=True),
        ramp_down_seconds=_positive(workload.get("ramp_down_seconds", 0),
                                    "workload.ramp_down_seconds", allow_zero=True),
        workers=int(_positive(workload.get("workers", 8), "workload.workers")),
        channels=int(_positive(workload.get("channels", 1), "workload.channels")),
        batch_size=int(_positive(workload.get("batch_size", 100), "workload.batch_size")),
        max_inflight=int(_positive(workload.get("max_inflight", 64), "workload.max_inflight")),
        retry_mode=retry_mode,
        abort_on_consecutive_failures=int(workload.get("abort_on_consecutive_failures", 0) or 0),
    )
    if wl.workers > wl.max_inflight:
        raise ConfigError(
            f"workload.workers ({wl.workers}) exceeds max_inflight ({wl.max_inflight}); "
            "the inflight bound would never be observable")

    steps = _build_steps(prof.get("steps"), wl)

    # --- can this rate actually be offered? ---------------------------------
    pl = gen.get("pipeline") or {}
    data = gen.get("data") or {}
    pipeline = Pipeline(
        max_delay_ms=float(pl.get("max_delay_ms", 5000)),
        min_bucket_size=int(pl.get("min_bucket_size", 16777216)),
        approx_record_bytes=int(pl.get("approx_record_bytes", data.get("body_bytes", 256) + 250)),
    )

    warnings: list[str] = []
    for step in steps:
        ceiling = pipeline.max_reachable_rps(wl.workers, wl.batch_size, step.rps)
        if step.rps > ceiling * 1.02:
            flush = pipeline.flush_seconds(step.rps)
            need = math.ceil(step.rps * flush / wl.batch_size)
            warnings.append(
                f"step {step.index}: target {step.rps:,.0f} rec/s is above the reachable "
                f"ceiling of {ceiling:,.0f} rec/s. The export RPC blocks until the batch is "
                f"durable, and at this rate a bucket takes {flush:.1f}s to flush, so "
                f"{wl.workers} workers x {wl.batch_size} records can only deliver "
                f"{ceiling:,.0f}/s. Raise workers to >= {need} (and max_inflight with it), "
                f"or raise batch_size.")
        if wl.max_inflight < wl.workers:
            break

    return GeneratorConfig(
        profile=profile,
        description=str(prof.get("description", "")).strip(),
        target=target,
        auth=auth,
        workload=wl,
        steps=steps,
        data=gen.get("data") or {},
        output=gen.get("output") or {},
        stop_when=prof.get("stop_when") or {},
        fault=prof.get("fault") or {},
        checkpoint_interval_seconds=float(prof.get("checkpoint_interval_seconds", 0) or 0),
        pipeline=pipeline,
        warnings=tuple(warnings),
    )


def _build_steps(raw_steps: Iterable[dict] | None, wl: Workload) -> tuple[Step, ...]:
    if not raw_steps:
        return (Step(index=0, rps=wl.target_rps, duration_seconds=wl.duration_seconds),)
    steps = []
    for i, s in enumerate(raw_steps):
        steps.append(Step(
            index=i,
            rps=_positive(s.get("rps"), f"profiles.steps[{i}].rps"),
            duration_seconds=_positive(s.get("duration_seconds"),
                                       f"profiles.steps[{i}].duration_seconds"),
        ))
    return tuple(steps)
