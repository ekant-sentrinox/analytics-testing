# Network

Every IP and port here was discovered from the running environment or from the
service configuration files. None is assumed.

---

## Hosts

| Role | Private IP | Instance | Hostname |
|---|---|---|---|
| SERVER 1 — generator | `10.16.21.20` | `i-0257966a73c5176ec` t3.large | `ip-10-16-21-20.us-west-2.compute.internal` |
| SERVER 2 — collector | `10.16.24.204` | `i-0be2c5bc12842b924` t3.large | `ip-10-16-24-204.us-west-2.compute.internal` |
| SERVER 3 — compactor | `10.16.25.10` | `i-06f213280bc815721` t3.large | `ip-10-16-25-10.us-west-2.compute.internal` |

VPC `vpc-085019101c32ac705`, all in `us-west-2b`. All three share one security
group: **`sg-0ea9dd40012388877` (`analytics-performance-sg`)**.

## External endpoints

| | |
|---|---|
| PostgreSQL RDS | `analytics-perf-catalog.ctu62c8oii9y.us-west-2.rds.amazonaws.com:5432` → `10.16.30.116`, PostgreSQL 18.4, database `bench_catalog`, user `analytics` |
| S3 | `s3://sentri-analytics-performance-test/bench/`, `us-west-2`, IAM role `analytics-performance-s3-role` |

---

## Ports

| Port | Host | Service | Where the number comes from |
|---|---|---|---|
| 22 | all | SSH | — |
| **4317** | SERVER 2 | OTLP gRPC | `otel_collector.grpc_port`, `dazzleduck-sql-otel-collector/src/main/resources/reference.conf` |
| **8081** | SERVER 2 | collector health | `otel_collector.health.port`, same file |
| **8080** | SERVER 3 | compactor health | `dazzleduck_sql_compaction.health_port`, `dazzleduck-sql-ducklake-compactor/src/main/resources/application.conf` |
| 5432 | RDS | PostgreSQL | RDS endpoint |
| 443 | — | S3 | AWS |
| 9100 | all | node_exporter | optional, `scripts/deploy-node-exporter.sh` |
| 9101 | SERVER 1 | pipeline exporter | this project |
| 9102 | SERVER 1 | generator metrics | this project |
| 9090 | SERVER 1 | Prometheus | docker, optional |
| 3000 | SERVER 1 | Grafana | docker, optional |

---

## Current state — and the blocker

Measured from SERVER 1 with `scripts/connectivity-check.sh`:

```
PASS  10.16.24.204:22     SERVER 2 ssh                    — 6ms
FAIL  10.16.24.204:4317   SERVER 2 OTLP gRPC              — timed out after 6004ms
FAIL  10.16.24.204:8081   SERVER 2 health                 — timed out after 6003ms
PASS  10.16.25.10:22      SERVER 3 ssh                    — 4ms
FAIL  10.16.25.10:8080    SERVER 3 health                 — timed out after 6003ms
PASS  ...rds...:5432      PostgreSQL RDS catalog          — 7ms
```

**Timeout, not refusal.** That distinction is the whole diagnosis:

| symptom | meaning |
|---|---|
| connects in a few ms | fine |
| **connection refused**, fast | the route is fine, nothing is listening — a service problem |
| **timeout** | packets are being dropped — a security group problem |

Both services are confirmed listening on `0.0.0.0` and answering on their own
loopbacks:

```
$ ssh ec2-user@10.16.24.204 'ss -tlnp | grep -E "4317|8081"'
LISTEN 0 4096 *:4317  users:(("java",pid=...))
LISTEN 0 50   *:8081  users:(("java",pid=...))

$ ssh ec2-user@10.16.24.204 'curl -s http://127.0.0.1:8081/health'
{"status":"HEALTHY","uptimeSeconds":277,"grpcPort":4317,"knownQueues":1,"batchesProcessed":0}
```

No host firewall is involved — `firewalld` is inactive and there are no
iptables/nftables rules. The security group allows 22 only.

### Consequence

Only one of these actually matters:

- **4317 — a real blocker.** The generator cannot reach the collector, so no
  valid throughput or latency measurement is possible. An SSH forward
  (`scripts/tunnel.sh`) stands in for it today, which makes the pipeline fully
  testable but is explicitly not a performance path — runs through it are
  tagged and the report banners them.
- **8080 / 8081 — not a problem.** Closed is the intended state. Health is
  checked by each host's own agent over its loopback and aggregated over SSH,
  so nothing is lost. `connectivity-check.sh` reports these as **PASS —
  correct by design**, not as failures.
- Everything else already works: catalog, S3, DuckLake, compaction, the
  exporter's catalog/watermark/S3 queries, and both services.

### The fix — one rule, not three

Only **4317** needs to open. 8080, 8081 and 9100 stay closed: health is checked
by each host's own agent over its loopback, and the central exporter reads the
result over SSH. The full reasoning, with the measurements behind it, is in
[MONITORING_ARCHITECTURE.md](MONITORING_ARCHITECTURE.md).

```bash
# Data plane: generator -> collector, OTLP gRPC.
# Self-referencing, so only members of the security group can use it.
aws ec2 authorize-security-group-ingress \
  --region us-west-2 --group-id sg-0ea9dd40012388877 \
  --ip-permissions 'IpProtocol=tcp,FromPort=4317,ToPort=4317,UserIdGroupPairs=[{
      GroupId=sg-0ea9dd40012388877,
      Description="OTLP gRPC ingestion: generator -> collector (data plane)"}]'
```

Tighter, if the generator has its own security group — this also removes
SERVER 3's ability to reach the ingestion port at all:

```bash
--ip-permissions 'IpProtocol=tcp,FromPort=4317,ToPort=4317,UserIdGroupPairs=[{
    GroupId=sg-GENERATOR,Description="OTLP ingestion from the generator tier only"}]'
```

#### Deliberately not opened

| Port | Why not |
|---|---|
| 8081 (S2), 8080 (S3) | Health only, and it is checked on the host over `127.0.0.1`. Nothing consumes it across the network. Verified: `curl 127.0.0.1:8081/health` works on S2 while the port is unreachable from S1 — which is the desired state, not a fault. |
| 9100 on S2/S3 | node_exporter is not deployed there. `bench-agent` already reports CPU, memory, disk and network at 1 s, finer than a scrape. |
| anything S2 ↔ S3 | Measured: no traffic exists in either direction. They coordinate only through the catalog and the bucket. |
| 3000 / 9090 between VMs | Consumed on SERVER 1. For workstation access use an SSH forward (below) or a rule scoped to your own /32. |

Optional, only to reach the dashboards from a workstation — scope to your own
CIDR, not `0.0.0.0/0`:

```bash
aws ec2 authorize-security-group-ingress \
  --region us-west-2 --group-id sg-0ea9dd40012388877 \
  --ip-permissions \
    'IpProtocol=tcp,FromPort=3000,ToPort=3000,IpRanges=[{CidrIp=YOUR.CIDR/32,Description="Grafana"}]' \
    'IpProtocol=tcp,FromPort=9090,ToPort=9090,IpRanges=[{CidrIp=YOUR.CIDR/32,Description="Prometheus"}]'
```

The EC2 instance role has **no** `ec2:*` permissions, so this cannot be done
from the instances:

```
UnauthorizedOperation: ... not authorized to perform: ec2:DescribeSecurityGroups
```

Run it from a session that has EC2 rights, or use the console.

### Verifying

```bash
./scripts/connectivity-check.sh   # all six rows PASS
./scripts/health-check.sh --deep  # pushes real records through and reads them back
```

---

## Required flows

| From | To | Port | Why |
|---|---|---|---|
| SERVER 1 | SERVER 2 | 4317 | OTLP export — the pipeline. **The only cross-VM port that needs a rule.** |
| SERVER 1 | SERVER 2, 3 | 22 | deploy, result collection, fault injection, and monitoring aggregation |
| SERVER 2 | RDS | 5432 | catalog commit per flush |
| SERVER 3 | RDS | 5432 | catalog read/write per merge |
| SERVER 1 | RDS | 5432 | backlog polling, correctness validation |
| SERVER 2, 3 | S3 | 443 | Parquet read/write |
| SERVER 1 | S3 | 443 | object statistics, correctness validation |
| all | 169.254.169.254 | 80 | IMDS — instance role credentials |
| all | 169.254.169.123 | 123 | chrony |

Egress to the internet is also needed on first run: DuckDB downloads the
`httpfs`, `aws`, `ducklake` and `postgres` extensions into `~/.duckdb`, and
Maven fetches dependencies. Both are one-time and already done on these hosts.

---

## SSH

SERVER 1 is the control node. Out of the box it had an `authorized_keys` but no
private key of its own, so it could not reach 2 or 3 — which breaks deploy,
result collection and the recovery tests.

`scripts/setup-ssh.sh` creates a **dedicated** keypair
(`~/.ssh/id_bench_ed25519`, comment `analytics-distributed-test control node`)
and **appends** the public half to `authorized_keys` on servers 2 and 3. It
never rewrites `authorized_keys`, never replaces an existing key, and is
idempotent. Run without `--apply` first to see exactly what it would do.

```bash
./scripts/setup-ssh.sh            # report only
./scripts/setup-ssh.sh --apply
```

To revoke, remove that one line from `~/.ssh/authorized_keys` on servers 2 and 3.

---

## Diagnosing a port yourself

```bash
# refused vs dropped, with timing
time timeout 6 bash -c 'echo > /dev/tcp/10.16.24.204/4317'
#   exit 0            -> open
#   exit 1, fast      -> refused: route fine, service down
#   exit 124, ~6s     -> dropped: security group

# is anything listening, on the host itself?
ssh ec2-user@10.16.24.204 'ss -tlnp | grep 4317'

# is a host firewall in the way? (not on these hosts)
ssh ec2-user@10.16.24.204 'sudo systemctl is-active firewalld; sudo nft list ruleset | head'
```
