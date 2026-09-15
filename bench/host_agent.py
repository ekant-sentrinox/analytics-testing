#!/usr/bin/env python3
"""Host-local monitoring agent. Deployed to all three servers.

This is the whole monitoring data plane. It runs ON the machine it observes and
reaches nothing across the network except the endpoints that machine already
talks to, which is what lets ports 8080/8081 stay closed in the security group.

    health      curl http://127.0.0.1:<port>/health   -- LOOPBACK ONLY.
                No inbound rule is needed for a service to be health-checked;
                the check runs beside it.
    S3          HEAD the configured bucket using this host's instance role.
                Proves the data path from THIS host, which is the only place the
                answer is meaningful -- S3 being reachable from the control node
                says nothing about whether the collector can write.
    RDS         TCP connect to the catalog from THIS host, same reasoning.
    system      CPU, memory, disk, network, per-process RSS, JVM heap.
    lifecycle   systemd restart count, OOM kills, unit state.

Two outputs, for two different consumers:

    <output>.jsonl        append-only, 1 s, the measurement record. Survives the
                          run and is what the report is generated from.
    state/health.json     latest snapshot, written atomically. This is what the
                          central exporter pulls over SSH (port 22, already
                          open) instead of scraping 8080/8081 across the network.

Reads /proc directly rather than depending on psutil, because it must run under
the stock python3 (3.9) on a freshly provisioned host with nothing pip-installed.

Deliberately not a Prometheus server: nothing here should require an inbound
port. Aggregation is pull-over-SSH by design.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
SECTOR = 512


def read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return default


def cpu_times():
    out = {}
    for line in read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        parts = line.split()
        name = parts[0]
        vals = [int(v) for v in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        out[name] = (sum(vals), idle)
    return out


def stat_counters():
    ctxt = procs = 0
    for line in read("/proc/stat").splitlines():
        if line.startswith("ctxt "):
            ctxt = int(line.split()[1])
        elif line.startswith("processes "):
            procs = int(line.split()[1])
    return ctxt, procs


def meminfo():
    out = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        val = rest.strip().split()
        if val:
            out[key] = int(val[0]) * 1024
    total = out.get("MemTotal", 0)
    avail = out.get("MemAvailable", 0)
    return {
        "mem_total_bytes": total,
        "mem_available_bytes": avail,
        "mem_used_bytes": total - avail,
        "page_cache_bytes": out.get("Cached", 0),
        "dirty_bytes": out.get("Dirty", 0),
        "writeback_bytes": out.get("Writeback", 0),
        "swap_total_bytes": out.get("SwapTotal", 0),
        "swap_free_bytes": out.get("SwapFree", 0),
    }


def diskstats():
    out = {}
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        # Skip partitions and loop/ram devices; the whole-disk row is enough.
        if name.startswith(("loop", "ram", "dm-")) or re.search(r"p?\d+$", name):
            if not name.startswith("nvme") or re.search(r"p\d+$", name):
                continue
        out[name] = {
            "reads": int(f[3]), "read_bytes": int(f[5]) * SECTOR,
            "writes": int(f[7]), "write_bytes": int(f[9]) * SECTOR,
            "io_ms": int(f[12]),
        }
    return out


def netdev():
    out = {}
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo":
            continue
        f = rest.split()
        if len(f) < 16:
            continue
        out[name] = {"rx_bytes": int(f[0]), "rx_packets": int(f[1]),
                     "tx_bytes": int(f[8]), "tx_packets": int(f[9])}
    return out


def proc_stat(pid: int):
    raw = read(f"/proc/{pid}/stat")
    if not raw:
        return None
    # comm can contain spaces and parens; everything after the last ')' is safe.
    tail = raw[raw.rfind(")") + 2:].split()
    utime, stime = int(tail[11]), int(tail[12])
    rss_pages = int(tail[21])
    start_ticks = int(tail[19])
    return {
        "cpu_ticks": utime + stime,
        "rss_bytes": rss_pages * PAGE_SIZE,
        "num_threads": int(tail[17]),
        "start_ticks": start_ticks,
    }


def jvm_heap(pid: int):
    """Heap used/committed via jcmd. Best effort — absent is normal."""
    try:
        out = subprocess.check_output(["jcmd", str(pid), "GC.heap_info"],
                                      text=True, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:                                             # noqa: BLE001
        return None
    used = re.search(r"used\s+(\d+)K", out)
    total = re.search(r"total\s+(\d+)K", out)
    return {
        "heap_used_bytes": int(used.group(1)) * 1024 if used else None,
        "heap_total_bytes": int(total.group(1)) * 1024 if total else None,
    }


def pid_of(pattern: str):
    """Find the watched process — excluding ourselves.

    `pgrep -f` matches full command lines, and THIS agent's own command line
    contains the pattern (it is passed as --process-pattern). Without the
    exclusion the agent watches itself and reports a 12 MiB python process as
    the collector's RSS — observed exactly that on a live run. Also prefer the
    java process when several match, since a shell wrapper can match too.
    """
    try:
        out = subprocess.check_output(["pgrep", "-af", pattern], text=True,
                                      stderr=subprocess.DEVNULL, timeout=5)
    except Exception:                                             # noqa: BLE001
        return None
    me = os.getpid()
    candidates = []
    for line in out.splitlines():
        pid_s, _, cmd = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == me or "host_agent" in cmd or "--process-pattern" in cmd:
            continue
        candidates.append((0 if cmd.lstrip().startswith(("java", "/usr/bin/java")) or
                           "/bin/java " in cmd else 1, pid))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def oom_events(since_marker: dict):
    """Count OOM kills seen so far. dmesg needs privileges on AL2023; a failure
    here is reported once, not every second."""
    try:
        out = subprocess.check_output(["dmesg", "-T"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=5)
    except Exception:                                             # noqa: BLE001
        since_marker["unavailable"] = True
        return None
    return out.lower().count("out of memory: killed")


def disk_free(path: str):
    try:
        st = os.statvfs(path)
        return {"free_bytes": st.f_bavail * st.f_frsize,
                "total_bytes": st.f_blocks * st.f_frsize}
    except OSError:
        return {"free_bytes": None, "total_bytes": None}


# ---------------------------------------------------------------------------
# Dependency checks. All run FROM this host, which is the point: reachability
# measured from the control node answers a different and less useful question.
# ---------------------------------------------------------------------------

def check_health(url: str, timeout: float = 3.0) -> dict:
    """GET the service's own health endpoint over the loopback."""
    if not url:
        return {"configured": False}
    started = time.monotonic()
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode()
            elapsed = (time.monotonic() - started) * 1000
            out = {"configured": True, "ok": 200 <= resp.status < 300,
                   "http_status": resp.status, "latency_ms": round(elapsed, 2)}
            try:
                out["body"] = json.loads(body)
            except ValueError:
                out["body"] = body[:500]
            return out
    except Exception as exc:                                      # noqa: BLE001
        return {"configured": True, "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def check_tcp(host: str, port: int, timeout: float = 3.0) -> dict:
    """Plain TCP connect. Used for RDS: proves route, security group and
    listener without needing a database driver on the host."""
    if not host:
        return {"configured": False}
    import socket
    started = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"configured": True, "ok": True, "host": host, "port": int(port),
                    "latency_ms": round((time.monotonic() - started) * 1000, 2)}
    except Exception as exc:                                      # noqa: BLE001
        return {"configured": True, "ok": False, "host": host, "port": int(port),
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def check_s3(bucket: str, region: str, timeout: float = 5.0) -> dict:
    """Unauthenticated HTTPS HEAD against the bucket's virtual-hosted endpoint.

    Deliberately NOT `aws s3api head-bucket`. The AWS CLI v2 is a bundled Python
    application: cold start alone is several seconds, and burning 3-5 s of CPU
    every 15 s on a 2-vCPU host that is simultaneously the system under test
    would make the monitoring part of the measurement. (It also just timed out
    at 8 s in practice, which is how this was found.)

    A HEAD needs no credentials and no SigV4 signing. What comes back still
    tells us what we need:

        200 / 403     DNS, route, TLS and the bucket all exist. 403 is the
                      expected answer for an unauthenticated request to a
                      private bucket — it is a SUCCESS for a reachability check.
        404           the bucket does not exist
        timeout       no route to S3 from this host — the real failure we care
                      about detecting mid-run

    What this does NOT verify is IAM authorization. That is checked properly by
    scripts/check-s3.sh at preflight, and continuously and far more
    convincingly by the pipeline itself: if the instance role were broken, the
    collector could not write Parquet and the run would fail loudly.
    """
    if not bucket:
        return {"configured": False}
    import http.client
    host = f"{bucket}.s3.{region}.amazonaws.com"
    started = time.monotonic()
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        conn.request("HEAD", "/")
        status = conn.getresponse().status
        elapsed = round((time.monotonic() - started) * 1000, 2)
        reachable = status in (200, 301, 307, 403)
        out = {"configured": True, "ok": reachable, "bucket": bucket,
               "endpoint": host, "http_status": status, "latency_ms": elapsed,
               "checks": "reachability only, not IAM authorization"}
        if not reachable:
            out["error"] = f"unexpected HTTP {status}"
        return out
    except Exception as exc:                                      # noqa: BLE001
        return {"configured": True, "ok": False, "bucket": bucket, "endpoint": host,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                                     # noqa: BLE001
                pass


def systemd_unit(unit: str) -> dict:
    """Unit state and restart count. A restart during a measured run is a
    result, not a footnote, so it has to be captured continuously."""
    if not unit:
        return {"configured": False}
    try:
        out = subprocess.check_output(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState",
             "-p", "NRestarts", "-p", "MemoryCurrent", "-p", "ExecMainStartTimestampMonotonic"],
            text=True, stderr=subprocess.DEVNULL, timeout=5)
    except Exception as exc:                                      # noqa: BLE001
        return {"configured": True, "ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    kv = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        kv[k] = v
    def as_int(key):
        try:
            n = int(kv.get(key, ""))
            return None if n in (0, 18446744073709551615) else n
        except ValueError:
            return None
    return {
        "configured": True,
        "ok": kv.get("ActiveState") == "active",
        "active_state": kv.get("ActiveState"),
        "sub_state": kv.get("SubState"),
        "restarts": int(kv.get("NRestarts") or 0),
        "memory_current_bytes": as_int("MemoryCurrent"),
    }


def write_atomic(path: str, payload: dict) -> None:
    """Write via a temp file and rename. The central exporter reads this file
    on its own schedule; a partial read would look like a scrape failure."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, default=str)
    os.replace(tmp, path)


class Agent:
    def __init__(self, args):
        self.args = args
        self.stop = False
        self.prev_cpu = cpu_times()
        self.prev_disk = diskstats()
        self.prev_net = netdev()
        self.prev_ctxt, self.prev_procs = stat_counters()
        self.prev_proc = None
        self.prev_t = time.monotonic()
        self.pid = None
        self.pid_start_ticks = None
        self.restarts = 0
        self.oom_marker = {}
        # Dependency checks run on their own, slower clock. At 1 s they would
        # be a load source of their own — an S3 HEAD every second from three
        # hosts is pointless traffic during a throughput measurement.
        self._dep_last = 0.0
        self._deps = {"health": {}, "s3": {}, "rds": {}, "unit": {}}

    def _resolve_pid(self):
        if not self.args.process_pattern:
            return
        pid = pid_of(self.args.process_pattern)
        if pid is None:
            self.pid = None
            return
        info = proc_stat(pid)
        if info is None:
            self.pid = None
            return
        # A changed start time means the process was replaced, i.e. restarted.
        if self.pid is not None and (pid != self.pid
                                     or info["start_ticks"] != self.pid_start_ticks):
            self.restarts += 1
        self.pid = pid
        self.pid_start_ticks = info["start_ticks"]

    def _refresh_deps(self, now_mono: float) -> bool:
        """Re-run the dependency checks if their interval has elapsed."""
        if now_mono - self._dep_last < self.args.check_interval:
            return False
        self._dep_last = now_mono
        a = self.args
        self._deps = {
            "health": check_health(a.health_url),
            "s3": check_s3(a.s3_bucket, a.aws_region),
            "rds": check_tcp(a.rds_host, a.rds_port),
            "unit": systemd_unit(a.systemd_unit),
        }
        return True

    def sample(self):
        now_mono = time.monotonic()
        elapsed = max(1e-6, now_mono - self.prev_t)
        now = time.time()
        dep_refreshed = self._refresh_deps(now_mono)

        cpu_now = cpu_times()
        cpu_pct = {}
        for name, (total, idle) in cpu_now.items():
            ptotal, pidle = self.prev_cpu.get(name, (total, idle))
            dt, di = total - ptotal, idle - pidle
            cpu_pct[name] = round(100.0 * (dt - di) / dt, 2) if dt > 0 else 0.0
        self.prev_cpu = cpu_now

        disk_now = diskstats()
        disk = {}
        for name, cur in disk_now.items():
            prev = self.prev_disk.get(name, cur)
            disk[name] = {
                "read_bytes_per_s": (cur["read_bytes"] - prev["read_bytes"]) / elapsed,
                "write_bytes_per_s": (cur["write_bytes"] - prev["write_bytes"]) / elapsed,
                "read_iops": (cur["reads"] - prev["reads"]) / elapsed,
                "write_iops": (cur["writes"] - prev["writes"]) / elapsed,
                "util_percent": min(100.0, (cur["io_ms"] - prev["io_ms"]) / (elapsed * 10.0)),
            }
        self.prev_disk = disk_now

        net_now = netdev()
        net = {}
        for name, cur in net_now.items():
            prev = self.prev_net.get(name, cur)
            net[name] = {
                "rx_bytes_per_s": (cur["rx_bytes"] - prev["rx_bytes"]) / elapsed,
                "tx_bytes_per_s": (cur["tx_bytes"] - prev["tx_bytes"]) / elapsed,
                "rx_pps": (cur["rx_packets"] - prev["rx_packets"]) / elapsed,
                "tx_pps": (cur["tx_packets"] - prev["tx_packets"]) / elapsed,
            }
        self.prev_net = net_now

        ctxt, procs = stat_counters()
        load1, load5, load15 = (read("/proc/loadavg").split() + ["0"] * 3)[:3]

        self._resolve_pid()
        proc = None
        if self.pid:
            cur = proc_stat(self.pid)
            if cur:
                prev = self.prev_proc
                cpu = (100.0 * (cur["cpu_ticks"] - prev["cpu_ticks"]) / CLK_TCK / elapsed
                       if prev else 0.0)
                proc = {"pid": self.pid, "cpu_percent": round(cpu, 2),
                        "rss_bytes": cur["rss_bytes"], "threads": cur["num_threads"]}
                if self.args.jvm:
                    proc["jvm"] = jvm_heap(self.pid)
                self.prev_proc = cur

        sample = {
            "ts": now,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "host": self.args.role or os.uname().nodename,
            "hostname": os.uname().nodename,
            "cpu_percent": cpu_pct.pop("cpu", 0.0),
            "cpu_per_core": cpu_pct,
            "load": {"1m": float(load1), "5m": float(load5), "15m": float(load15)},
            "context_switches_per_s": (ctxt - self.prev_ctxt) / elapsed,
            "processes_forked_per_s": (procs - self.prev_procs) / elapsed,
            "memory": meminfo(),
            "disk_io": disk,
            "disk_space": {p: disk_free(p) for p in self.args.watch_path},
            "network": net,
            "process": proc,
            "process_restarts": self.restarts,
            "oom_kills": oom_events(self.oom_marker),
            # Health and dependency reachability, all measured from THIS host.
            "checks": self._deps,
            "checks_refreshed": dep_refreshed,
        }
        self.prev_ctxt, self.prev_procs = ctxt, procs
        self.prev_t = now_mono
        return sample

    def health_snapshot(self, sample: dict) -> dict:
        """The compact document the central exporter pulls over SSH.

        Only what a monitoring system needs — not the full 1 s sample. Keeping
        it small matters: it is read across an SSH connection on every scrape.
        """
        checks = sample.get("checks") or {}
        mem = sample.get("memory") or {}
        proc = sample.get("process") or {}
        root = (sample.get("disk_space") or {}).get("/") or {}
        disk_io = sample.get("disk_io") or {}
        net = sample.get("network") or {}
        return {
            "role": sample.get("host"),
            "hostname": sample.get("hostname"),
            "ts": sample.get("ts"),
            "iso": sample.get("iso"),
            "agent_ok": True,
            "service": {
                "health": checks.get("health") or {},
                "unit": checks.get("unit") or {},
                "pid": proc.get("pid"),
                "rss_bytes": proc.get("rss_bytes"),
                "cpu_percent": proc.get("cpu_percent"),
                "jvm": proc.get("jvm"),
                "restarts": sample.get("process_restarts"),
                "oom_kills": sample.get("oom_kills"),
            },
            "dependencies": {"s3": checks.get("s3") or {}, "rds": checks.get("rds") or {}},
            "system": {
                "cpu_percent": sample.get("cpu_percent"),
                "load_1m": (sample.get("load") or {}).get("1m"),
                "mem_used_bytes": mem.get("mem_used_bytes"),
                "mem_available_bytes": mem.get("mem_available_bytes"),
                "swap_total_bytes": mem.get("swap_total_bytes"),
                "root_free_bytes": root.get("free_bytes"),
                "root_total_bytes": root.get("total_bytes"),
                "disk_write_bytes_per_s": sum(
                    (d.get("write_bytes_per_s") or 0) for d in disk_io.values()),
                "disk_read_bytes_per_s": sum(
                    (d.get("read_bytes_per_s") or 0) for d in disk_io.values()),
                "net_rx_bytes_per_s": sum((n.get("rx_bytes_per_s") or 0) for n in net.values()),
                "net_tx_bytes_per_s": sum((n.get("tx_bytes_per_s") or 0) for n in net.values()),
            },
        }

    def run(self):
        os.makedirs(os.path.dirname(self.args.output) or ".", exist_ok=True)
        # First sample only establishes deltas; discard it rather than emitting
        # a row where every rate is measured against process start.
        self.sample()
        if self.args.once:
            # A single-shot run has nothing to diff against, so take a second
            # sample after one interval and report that.
            time.sleep(min(1.0, self.args.interval))
            sample = self.sample()
            if self.args.state:
                write_atomic(self.args.state, self.health_snapshot(sample))
            with open(self.args.output, "a", buffering=1) as fh:
                fh.write(json.dumps(sample, default=str) + "\n")
            if self.args.print_state:
                print(json.dumps(self.health_snapshot(sample), indent=2, default=str))
            return 0

        time.sleep(self.args.interval)
        with open(self.args.output, "a", buffering=1) as fh:
            next_tick = time.monotonic()
            while not self.stop:
                sample = self.sample()
                fh.write(json.dumps(sample, default=str) + "\n")
                if self.args.state:
                    write_atomic(self.args.state, self.health_snapshot(sample))
                next_tick += self.args.interval
                sleep = next_tick - time.monotonic()
                if sleep < 0:
                    next_tick = time.monotonic()
                    sleep = 0
                time.sleep(sleep)
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", required=True, help="JSONL path")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--role", default=os.environ.get("BENCH_ROLE", ""),
                    help="generator | collector | compactor")
    ap.add_argument("--process-pattern", default="",
                    help="pgrep -f pattern for the process to watch")
    ap.add_argument("--jvm", action="store_true", help="also sample JVM heap via jcmd")
    ap.add_argument("--watch-path", action="append", default=[],
                    help="filesystem path to report free space for (repeatable)")
    ap.add_argument("--once", action="store_true")

    # --- host-local health and dependency checks --------------------------
    ap.add_argument("--state", default="",
                    help="path for the atomically-written latest snapshot that the "
                         "central exporter pulls over SSH")
    ap.add_argument("--print-state", action="store_true",
                    help="with --once, print the snapshot to stdout")
    ap.add_argument("--health-url", default="",
                    help="LOOPBACK health endpoint, e.g. http://127.0.0.1:8081/health. "
                         "Checked from this host, so no inbound rule is required.")
    ap.add_argument("--systemd-unit", default="",
                    help="unit to report state and restart count for")
    ap.add_argument("--s3-bucket", default=os.environ.get("S3_BUCKET", ""))
    ap.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-west-2"))
    ap.add_argument("--rds-host", default=os.environ.get("PG_HOST", ""))
    ap.add_argument("--rds-port", type=int, default=int(os.environ.get("PG_PORT", 5432)))
    ap.add_argument("--check-interval", type=float, default=15.0,
                    help="seconds between dependency checks; deliberately slower than "
                         "--interval so they do not become a load source")
    args = ap.parse_args(argv)
    if not args.watch_path:
        args.watch_path = ["/"]

    agent = Agent(args)

    def _stop(*_a):
        agent.stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    return agent.run()


if __name__ == "__main__":
    sys.exit(main())
