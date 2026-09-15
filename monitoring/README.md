# Monitoring

```
monitoring/
├── exporters/pipeline_exporter.py    the load-bearing piece
├── prometheus/prometheus.yml
├── prometheus/rules/pipeline.yml     alerts + recording rules
├── grafana/build_dashboards.py       SOURCE for the dashboards
├── grafana/dashboards/*.json         generated; do not hand-edit
└── grafana/provisioning/             datasource + dashboard providers
```

Full explanation, including what cannot be measured and why:
**[../MONITORING.md](../MONITORING.md)**.

## Start

```bash
../scripts/start.sh --monitoring
```

Prometheus `:9090`, Grafana `:3000` (admin/admin).

## The pipeline exporter

The collector and the compactor expose **no Prometheus metrics** — one is built
with a `SimpleMeterRegistry`, the other with a `LoggingMeterRegistry`. This
process is the only source for backlog, visibility lag, compaction counters,
catalog health and S3 growth.

```bash
# one poll, printed, no daemon — the fastest way to see the whole pipeline state
../.venv/bin/python monitoring/exporters/pipeline_exporter.py --once --port 9199
```

Every scrape records success or failure per source. A source that is down
produces a recorded failure, never a silently missing sample: a gap and a zero
look identical on a graph, and only one of them is true.

## Editing dashboards

The JSON is generated. Edit the builder and re-run it — Grafana reloads from
disk within 30 s.

```bash
python3 monitoring/grafana/build_dashboards.py
```

Hand-editing 2000 lines of JSON across seven files is how panels drift into
three different units for the same metric. The builder keeps one palette, one
set of panel helpers and one definition of each query.

## Adding a metric

1. Emit it — from the exporter (`Prom` class in `pipeline_exporter.py`) or the
   generator (`_PromMetrics` in `generator/src/metrics.py`).
2. Add a panel in `build_dashboards.py`, re-run it.
3. If it encodes a pass/fail criterion, add the rule to
   `prometheus/rules/pipeline.yml` **and** the matching check in the test script
   — the live view and the generated report must use the same definition, or
   they will disagree and only one will be right.
