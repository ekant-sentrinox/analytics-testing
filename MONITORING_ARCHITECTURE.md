# Monitoring architecture and port analysis

Every fact below was measured on the running environment on 2026-09-10, not
inferred from a diagram. Commands used: `ss -lntp`, `ss -tnp state established`,
`docker ps`, `pgrep -af java`, `/proc/<pid>/environ`, `systemctl list-units`,
`curl 127.0.0.1:<port>/health`, and greps over the deployed configuration.

**No security group was modified.** The recommendations at the end are the
minimum rules; applying them is your decision.

---

## 1. Current architecture

```
┌─────────────────────────────────┐
│ SERVER 1   10.16.21.20          │  i-0257966a73c5176ec  t3.large
│ generator + control node        │
│                                 │
│  generator (python, on demand)  │
│  bench-agent.service            │  host-local agent
│  bench-exporter.service :9101   │  central aggregation
│  prometheus (docker)     :9090  │
│  grafana    (docker)     :3000  │
│  node-exporter (docker)  :9100  │
└───────────────┬─────────────────┘
                │ OTLP/gRPC :4317   ← the only cross-VM data-plane hop
                ▼
┌─────────────────────────────────┐
│ SERVER 2   10.16.24.204         │  i-0be2c5bc12842b924  t3.large
│ collector + DuckDB              │
│                                 │
│  bench-collector.service        │  java, listens *:4317 and *:8081
│  bench-agent.service            │  checks 127.0.0.1:8081
└───────┬─────────────────┬───────┘
        │ :443            │ :5432
        ▼                 ▼
┌──────────────┐   ┌──────────────────┐
│ S3           │   │ PostgreSQL RDS   │
│ sentri-      │   │ 10.16.30.116     │
│ analytics-   │   │ bench_catalog    │
│ performance- │   │ DuckLake catalog │
│ test/bench/  │   │                  │
└──────▲───────┘   └────────▲─────────┘
       │ :443               │ :5432
┌──────┴─────────────────────┴────────┐
│ SERVER 3   10.16.25.10              │  i-06f213280bc815721  t3.large
│ compactor + DuckDB                  │
│                                     │
│  bench-compactor.service            │  java, listens *:8080
│  bench-agent.service                │  checks 127.0.0.1:8080
└─────────────────────────────────────┘

        SERVER 2  <──X──>  SERVER 3      no traffic, by design
```

All three share security group **`sg-0ea9dd40012388877`** (`analytics-performance-sg`)
in `vpc-085019101c32ac705`, all in `us-west-2b`. Today it permits **port 22 only**.

---

## 2. Actual port usage — measured

### Listening sockets

| Host | Port | Bind | Process | What it is |
|---|---|---|---|---|
| S1 | 22 | `0.0.0.0` | sshd | management |
| S1 | 9101 | `0.0.0.0` | python | pipeline exporter (this project) |
| S1 | 9090 | `0.0.0.0` | docker → prometheus | monitoring UI |
| S1 | 3000 | `0.0.0.0` | docker → grafana | monitoring UI |
| S1 | 9100 | `*` | docker → node-exporter | host metrics, S1 only |
| S1 | 4317 | **`127.0.0.1`** | ssh | interim forward to S2 (see §4) |
| **S2** | **4317** | **`*`** | **java (collector)** | **OTLP gRPC receiver** |
| **S2** | **8081** | **`*`** | **java (collector)** | **health, JSON** |
| S2 | 22 | `0.0.0.0` | sshd | management |
| **S3** | **8080** | **`*`** | **java (compactor)** | **health, JSON** |
| S3 | 22 | `0.0.0.0` | sshd | management |

### Established outbound connections

| From | To | Port | Purpose |
|---|---|---|---|
| S2 | `18.246.x.x` | 443 | S3 — Parquet writes |
| S3 | `10.16.30.116` | 5432 | RDS — catalog, held open by DuckDB |
| S3 | `44.254.15.95` | 443 | S3 |
| S1 | `10.16.30.116` | 5432 | RDS — backlog polling by the exporter |
| S1 | AWS | 443 | S3 object statistics |

**No S2 ↔ S3 connection exists in either direction.** They coordinate only
through the shared catalog and bucket. Confirmed by `ss` on both hosts.

### Container inventory

`docker ps` — three containers, all on SERVER 1, all monitoring:
`bench-prometheus`, `bench-grafana`, `bench-node-exporter`. Docker is **not
installed** on SERVER 2 or SERVER 3; the collector and compactor run directly
under systemd.

---

## 3. Is port 4317 required? — yes, and it is not an OTEL telemetry port

You asked specifically not to open it just because it is "the OTEL port". It
isn't being opened for that reason. The evidence:

**What is listening.** SERVER 2's java process holds `*:4317`, from
`otel_collector.grpc_port = 4317` in its deployed `application.conf`. It is an
OTLP **receiver**.

**What is exporting.** Nothing. Checked on all three hosts:

- `OTEL_*` / `OTLP*` environment variables in every running java process
  (`/proc/<pid>/environ`) — **none**
- `-javaagent` on any command line — **none**, no auto-instrumentation
- OTLP endpoint or exporter keys anywhere in the deployed configuration — the
  only match on any host is S2's own `grpc_port = 4317` line

So there is **no application telemetry being shipped to a remote :4317**. If
that were the case, 4317 would be optional infrastructure and closing it would
cost only observability.

**What it actually is.** 4317 is the **data plane**. It is the arrow labelled
"Server 1 → Server 2" in your own diagram. The generator on SERVER 1 sends
`ExportLogsServiceRequest` over gRPC to the collector, which converts to Arrow,
writes Parquet to S3 and commits to the catalog. That request is the workload
under test.

**Verdict:** open, minimally — **SERVER 1 → SERVER 2 on TCP 4317 only**. Not
from S3, not from anywhere else, never `0.0.0.0/0`. Without it there is no
benchmark: the generator has nowhere to send.

**Verified consequence.** With an interim SSH forward standing in for that rule,
the full path works: 600/600 records accepted, zero loss, zero duplication,
Parquet on S3, compaction merging. So 4317 is not merely believed to be
required — it is the only cross-VM port whose absence stops the system.

---

## 4. Required data-plane ports

| Flow | Port | Status | Reason |
|---|---|---|---|
| S1 → S2 | 4317 | **needs a rule** | OTLP ingestion. The workload. Nothing works without it. |
| S2 → RDS | 5432 | working | Catalog commit on every bucket flush. Verified from S2 by its agent: 2.2 ms. |
| S2 → S3 | 443 | working | Parquet writes. Verified from S2: 122 ms. |
| S3 → RDS | 5432 | working | Snapshot read/write per merge. Verified from S3: 2.7 ms. |
| S3 → S3 bucket | 443 | working | Reads inputs, writes merged files. Verified from S3: 121 ms. |
| S2 ↔ S3 | — | **none needed** | Measured: no connection in either direction. They coordinate through the catalog and the bucket. Introducing traffic here would be inventing a dependency the design does not have. |

Egress on 443 to AWS and 80 to IMDS (`169.254.169.254`) is also required, plus
123 to `169.254.169.123` for chrony. All already permitted.

---

## 5. Required monitoring ports

**None between the VMs.**

That is the whole point of the design, and it is now implemented and verified.

| Need | How it is met | Port used |
|---|---|---|
| Collector health | `bench-agent` on S2 does `curl http://127.0.0.1:8081/health` | loopback |
| Compactor health | `bench-agent` on S3 does `curl http://127.0.0.1:8080/health` | loopback |
| CPU / memory / disk / network | agent reads `/proc` on each host | none |
| Process status, restarts, OOM | agent reads `systemctl show` and `dmesg` locally | none |
| S3 reachability | agent HTTPS HEADs the bucket **from that host** | 443 egress |
| RDS reachability | agent TCP-connects to the catalog **from that host** | 5432 egress |
| Aggregation | central exporter reads each agent's `state/health.json` over SSH | **22, already open** |
| Catalog backlog, visibility lag, S3 growth | exporter queries RDS and S3 directly from S1 | 5432, 443 |

Measuring a dependency **from the host that depends on it** is not just a
security convenience, it is more correct. S3 being reachable from the control
node says nothing about whether the collector can write to it.

Live proof, from `scripts/connectivity-check.sh`:

```
==> MONITORING PLANE — must stay closed
PASS  collector health closed      10.16.24.204:8081 not reachable from here — correct by design
PASS  compactor health closed      10.16.25.10:8080 not reachable from here — correct by design
PASS  collector health (on-host)   127.0.0.1:8081 answers on 10.16.24.204 — no inbound rule needed
PASS  compactor health (on-host)   127.0.0.1:8080 answers on 10.16.25.10 — no inbound rule needed
```

---

## 6. Ports that stay closed, and why

| Port | Host | Decision | Reason |
|---|---|---|---|
| **8081** | S2 | **stays closed** | Health only. Checked on the host over its loopback; the result is aggregated over SSH. No consumer needs it across the network. |
| **8080** | S3 | **stays closed** | Same. |
| **9100** | S2, S3 | **stays closed** | node_exporter is not deployed there and is not needed: `bench-agent` already reports CPU, memory, disk, network and per-process metrics at 1 s — finer than a Prometheus scrape — and the report reads its JSONL, not the TSDB. |
| 9090, 3000 | S1 | closed between VMs | Prometheus and Grafana are consumed on SERVER 1. If you want them from a workstation, scope a rule to your own CIDR — never `0.0.0.0/0`. |
| S2 ↔ S3, any port | — | **stays closed** | Measured: no traffic exists. |
| 4317 | S2 | **open, narrowly** | The one exception. Data plane. See §3. |

**A note on defence in depth.** Both health servers are constructed with
`new InetSocketAddress(port)`, which binds the **wildcard** address, and neither
service exposes a bind-address setting. So the security group is currently the
*only* thing keeping 8080/8081 off the network — there is no `bind: 127.0.0.1`
to fall back on. That makes keeping those rules closed more important, not less.
It is filed as a hardening request in
[IMPROVEMENTS.md](IMPROVEMENTS.md). If you want belt-and-braces before that
lands, a host-local `nftables` rule restricting those ports to `lo` would do it
without touching the application.

---

## 7. Recommended production monitoring architecture

```
  ┌────────────────────── DATA PATH ──────────────────────┐
  │                                                       │
  │   Generator ──:4317──> Collector ──:443──> S3         │
  │   (SERVER 1)           (SERVER 2) ──:5432─> RDS       │
  │                                                       │
  │                        Compactor ──:443──> S3         │
  │                        (SERVER 3) ──:5432─> RDS       │
  │                                                       │
  │   no SERVER 2 <-> SERVER 3 traffic                    │
  └───────────────────────────────────────────────────────┘

  ┌─────────────── HEALTH / MONITORING PATH ──────────────┐
  │                                                       │
  │   SERVER 2                        SERVER 3            │
  │   bench-agent                     bench-agent         │
  │     ├─ curl 127.0.0.1:8081          ├─ curl 127.0.0.1:8080
  │     ├─ /proc                        ├─ /proc          │
  │     ├─ systemctl, dmesg             ├─ systemctl, dmesg
  │     ├─ S3 HEAD  (egress 443)        ├─ S3 HEAD        │
  │     ├─ RDS TCP  (egress 5432)       ├─ RDS TCP        │
  │     └─> state/health.json           └─> state/health.json
  │              │                               │        │
  │              └───────── ssh :22 ─────────────┘        │
  │                          │                            │
  │                          ▼                            │
  │            SERVER 1  bench-exporter :9101             │
  │              + RDS query   -> backlog B2, catalog     │
  │              + DuckLake    -> visibility lag B3       │
  │              + S3 list     -> object count, bytes     │
  │                          │                            │
  │                          ▼                            │
  │              prometheus :9090 -> grafana :3000        │
  │                          │                            │
  │                          └─> results/<id>/raw/*.jsonl │
  │                              (the measurement record) │
  └───────────────────────────────────────────────────────┘

  ┌───────────────────── OTEL PATH ───────────────────────┐
  │   Not required. No OTLP exporter exists anywhere.     │
  │   :4317 on SERVER 2 is a RECEIVER and belongs to the  │
  │   data path above, not to telemetry.                  │
  └───────────────────────────────────────────────────────┘
```

### Why pull-over-SSH rather than push

Considered and rejected:

| Option | Why not |
|---|---|
| Scrape `:8081` / `:8080` from Prometheus | Needs an inbound rule per service port purely to answer "are you alive". Exactly what you asked to avoid. |
| Prometheus Pushgateway on S1 | Still a new inbound port, just pointing the other way, plus Pushgateway's well-known staleness semantics. |
| node_exporter + a metrics port on S2/S3 | Another inbound port, and duplicates what the agent already collects at finer resolution. |
| Agents write to S3, S1 reads | Works with zero new rules, but adds seconds of latency and S3 request cost to every health check. Kept in reserve for a cross-VPC or no-SSH environment. |
| **Agent writes locally, exporter reads over SSH** | **Chosen.** No new ports. Port 22 is already required for deploy, result collection and fault injection, so monitoring adds no attack surface. |

SSH multiplexing (`ControlMaster` + `ControlPersist=300`) means a scrape is a
new channel on an existing TCP session, not a fresh handshake every 10 s.

### Not affecting the measurement

This runs during a throughput test on 2-vCPU hosts, so the cost is capped, not
hoped for:

| Component | Limits | Measured |
|---|---|---|
| `bench-agent` (each host) | `CPUQuota=10%`, `MemoryMax=192M`, `Nice=10`, `IOSchedulingClass=idle` | ~1% of one core |
| `bench-exporter` (S1 only) | `CPUQuota=25%`, `MemoryMax=512M`, `Nice=10` | S1 is not in the data path |
| prometheus / grafana | 0.5 CPU, 1 GiB / 512 MiB, S1 only | — |

Deliberate choices behind those numbers:

- **The S3 check is an unauthenticated HTTPS `HEAD`, not `aws s3api head-bucket`.**
  The AWS CLI v2 is a bundled Python application: cold start alone is seconds.
  It actually **timed out at 8 s** during this work, which is how the problem
  was found. The HEAD returns 403 in ~120 ms and proves DNS, route, TLS and
  bucket existence. IAM authorization is verified at preflight by
  `check-s3.sh`, and continuously by the pipeline itself — a broken role means
  the collector cannot write Parquet and the run fails loudly.
- **Dependency checks run at 15 s, system samples at 1 s.** An S3 HEAD every
  second from three hosts is pointless traffic during a measurement.
- **Snapshot staleness is a first-class metric.** An agent that dies leaves a
  file that reads "healthy" forever. `bench_agent_snapshot_age_seconds` is
  exported and a snapshot older than 90 s is reported as a *scrape failure*,
  not as health.
- **The TCP probe no longer writes a byte.** `echo > /dev/tcp/...` sent a
  newline into the gRPC port, and the collector logged nine
  `HTTP/2 client preface string missing or corrupt` errors as a direct result —
  the monitoring was manufacturing the errors the run was being audited for.
  It now opens and closes the socket without sending anything.

### What is monitored

| Requirement | Metric | Source |
|---|---|---|
| Collector health | `bench_collector_up`, `bench_health_check_latency_ms{role="collector"}` | loopback curl on S2 |
| Compactor health | `bench_compactor_up`, compaction counters | loopback curl on S3 |
| CPU | `bench_host_metric{metric="cpu_percent"}` | `/proc/stat`, per core in JSONL |
| Memory | `mem_used_bytes`, `mem_available_bytes`, page cache, dirty, swap | `/proc/meminfo` |
| Disk | `root_free_bytes`, read/write bytes/s, IOPS, util% per device | `/proc/diskstats`, `statvfs` |
| Network | `net_rx_bytes_per_s`, `net_tx_bytes_per_s`, packets | `/proc/net/dev` |
| S3 connectivity | `bench_dependency_up{dependency="s3"}` + latency | HTTPS HEAD from each host |
| RDS connectivity | `bench_dependency_up{dependency="rds"}` + latency | TCP connect from each host |
| Process status | `bench_service_unit_active`, `bench_process_rss_bytes`, `bench_process_cpu_percent`, JVM heap | `systemctl show`, `/proc`, `jcmd` |
| Restarts / errors | `bench_service_restarts_total`, `bench_oom_kills_total`, extracted error logs | systemd `NRestarts`, `dmesg` |
| Pipeline backlog (B2) | `bench_backlog_files`, `bench_backlog_small_files`, slope | RDS query from S1 |
| Visibility lag (B3) | `bench_watermark_lag_seconds` | DuckLake watermark |
| S3 growth | `bench_s3_objects`, `bench_s3_bytes` | ListObjectsV2 from S1 |

Still genuinely unavailable, and no monitoring design can fix it from outside:
**B1, the collector's in-memory queue depth.** `writer.pending_batches` and
`writer.pending_buckets` are registered in a `SimpleMeterRegistry` with no
exporter, and `/health` publishes only `batchesProcessed`. The first observable
sign of queue saturation is `RESOURCE_EXHAUSTED`, by which point the queue is
full. Closing it needs a change in the collector —
[IMPROVEMENTS.md](IMPROVEMENTS.md) finding 1.

---

## 8. Minimum security-group rules

**One rule.** Self-referencing, so only members of the group can use it.

```bash
# Data plane: generator -> collector, OTLP gRPC.
# The single cross-VM port the architecture requires.
aws ec2 authorize-security-group-ingress \
  --region us-west-2 \
  --group-id sg-0ea9dd40012388877 \
  --ip-permissions 'IpProtocol=tcp,FromPort=4317,ToPort=4317,UserIdGroupPairs=[{
      GroupId=sg-0ea9dd40012388877,
      Description="OTLP gRPC ingestion: generator -> collector (data plane)"}]'
```

Tighter still, if the generator gets its own security group — recommended for a
real deployment, since it removes SERVER 3's ability to reach the ingestion port
at all:

```bash
--ip-permissions 'IpProtocol=tcp,FromPort=4317,ToPort=4317,UserIdGroupPairs=[{
    GroupId=sg-GENERATOR,
    Description="OTLP ingestion from the generator tier only"}]'
```

### Explicitly not recommended

| Rule | Why not |
|---|---|
| 8081 from anywhere | Health is checked on the host. Nothing consumes it remotely. |
| 8080 from anywhere | Same. |
| 9100 on S2/S3 | node_exporter is not deployed there and the agent already covers it at finer resolution. |
| any S2 ↔ S3 rule | No such traffic exists. |
| `0.0.0.0/0` on anything | Never. |

### Optional, only for human access to dashboards

Scope to your own address, never a wildcard:

```bash
aws ec2 authorize-security-group-ingress \
  --region us-west-2 --group-id sg-0ea9dd40012388877 \
  --ip-permissions \
    'IpProtocol=tcp,FromPort=3000,ToPort=3000,IpRanges=[{CidrIp=YOUR.IP/32,Description="Grafana"}]' \
    'IpProtocol=tcp,FromPort=9090,ToPort=9090,IpRanges=[{CidrIp=YOUR.IP/32,Description="Prometheus"}]'
```

Or skip it entirely and use a local forward from your workstation, which needs
no rule at all:

```bash
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 ec2-user@10.16.21.20
```

### Until the 4317 rule exists

`scripts/tunnel.sh start` forwards `127.0.0.1:4317` on SERVER 1 to SERVER 2 over
SSH. The health ports are deliberately **not** forwarded — they are not needed.

This makes the pipeline fully testable today, and it is honest about what it
costs: the run is tagged `BENCH_TRANSPORT=ssh-tunnel` in `metadata.json`, and
`report.md` prints a banner stating that **functional and correctness results
are valid while throughput and latency are not** — SSH adds encryption, a
userspace hop and TCP-over-TCP, and a single `ssh` process is single-threaded.
It is a functional workaround, not a performance path.

---

## 9. Verifying all of this

```bash
./scripts/connectivity-check.sh   # data / management / monitoring planes separately
./scripts/health-check.sh         # stage by stage, loopback health via SSH
./scripts/deploy-agent.sh         # (re)install the agents and the exporter
./scripts/status.sh               # one screen
```

Current result: **13/13 checks passing**, with 8080 and 8081 confirmed closed
across the network and answering on their own loopbacks.
