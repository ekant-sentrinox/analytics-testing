#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Capture the environment of all three servers as JSON on stdout.
#
#   scripts/collect-env.sh
#
# Written into results/<test-id>/environment.json for every run. Any figure in
# a report is only meaningful next to the machine it came from, and t3.large
# instances are burstable — CPU credit state can change a result between runs,
# so it is captured too.
# ---------------------------------------------------------------------------
. "$(cd "$(dirname "$0")" && pwd)/lib/common.sh"

load_env
PY="$(python_bin)"

# Runs on each host and prints one JSON object.
read -r -d '' PROBE <<'REMOTE' || true
python3 - "$1" <<'PY'
import json, os, platform, re, subprocess, sys

role = sys.argv[1] if len(sys.argv) > 1 else "unknown"

def sh(cmd, timeout=8):
    try:
        return subprocess.check_output(cmd, shell=True, text=True,
                                       stderr=subprocess.DEVNULL, timeout=timeout).strip()
    except Exception:
        return ""

def meminfo(key):
    for line in open("/proc/meminfo"):
        if line.startswith(key):
            return int(line.split()[1]) * 1024
    return 0

cpu_model = ""
for line in open("/proc/cpuinfo"):
    if line.startswith("model name"):
        cpu_model = line.split(":", 1)[1].strip()
        break

st = os.statvfs("/")
imds_tok = sh('curl -sX PUT "http://169.254.169.254/latest/api/token" '
              '-H "X-aws-ec2-metadata-token-ttl-seconds: 60"', 5)
def imds(path):
    if not imds_tok:
        return ""
    return sh(f'curl -s -H "X-aws-ec2-metadata-token: {imds_tok}" '
              f'http://169.254.169.254/latest/meta-data/{path}', 5)

out = {
    "role": role,
    "host": {
        "hostname": platform.node(),
        "os": sh("grep ^PRETTY_NAME= /etc/os-release | cut -d'\"' -f2"),
        "kernel": platform.release(),
        "cpu_model": cpu_model,
        "logical_cores": os.cpu_count(),
        "physical_cores": int(sh("lscpu -p=Core,Socket | grep -v '^#' | sort -u | wc -l") or 0),
        "ram_bytes": meminfo("MemTotal"),
        "swap_bytes": meminfo("SwapTotal"),
        # Not exposed on virtualised instances; recorded as "" rather than guessed.
        "cpu_governor": sh("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
    },
    "instance": {
        "type": imds("instance-type"),
        "id": imds("instance-id"),
        "az": imds("placement/availability-zone"),
        # t3 is burstable: a depleted CPU credit balance silently halves
        # throughput and looks exactly like a software regression.
        "burstable": imds("instance-type").startswith(("t2.", "t3.", "t4g.")),
    },
    "disk": {
        "root_total_bytes": st.f_blocks * st.f_frsize,
        "root_free_bytes": st.f_bavail * st.f_frsize,
        "device": sh("findmnt -no SOURCE /"),
        "rotational": sh("cat /sys/block/nvme0n1/queue/rotational") == "1",
        "tmp_is_tmpfs": sh("findmnt -no FSTYPE /tmp") == "tmpfs",
    },
    "java": {
        "version": sh("java -version 2>&1 | head -1"),
        "home": os.environ.get("JAVA_HOME", ""),
    },
    "python": {"version": platform.python_version()},
    "duckdb_cli": sh("duckdb --version"),
    "clock": {
        "ntp_source": sh("chronyc tracking | grep 'Reference ID' | awk '{print $4}'"),
        "last_offset_s": sh("chronyc tracking | grep 'Last offset' | awk '{print $4}'"),
        "stratum": sh("chronyc tracking | grep Stratum | awk '{print $3}'"),
    },
    "container": {"runtime": "none", "note": "processes run directly under systemd, not containerised"},
}
print(json.dumps(out))
PY
REMOTE

capture() {                     # capture <role> <host|local>
  local role="$1" host="$2"
  if [ "$host" = "local" ]; then
    bash -c "$PROBE" _ "$role" 2>/dev/null || echo '{}'
  else
    remote_stdin "$host" "bash -s -- $role" <<< "$PROBE" 2>/dev/null || echo '{}'
  fi
}

GEN="$(capture generator local)"
COL="$(capture collector "$COLLECTOR_SSH_HOST")"
COM="$(capture compactor "$COMPACTOR_SSH_HOST")"

# Config actually in force, read back from the deployed files rather than from
# the templates — what is running is what matters.
COL_CONF="$(remote "$COLLECTOR_SSH_HOST" \
  "sed 's/password=[^ \\\"]*/password=***/g' /opt/analytics-bench/collector/conf/application.conf" 2>/dev/null || echo '')"
COM_CONF="$(remote "$COMPACTOR_SSH_HOST" \
  "sed 's/password=[^ \\\"]*/password=***/g' /opt/analytics-bench/compactor/conf/application.conf" 2>/dev/null || echo '')"
GIT_COMMIT="$(remote "$COLLECTOR_SSH_HOST" "git -C /home/$SSH_USER/dazzleduck-sql-server rev-parse --short HEAD" 2>/dev/null || echo unknown)"

GEN="$GEN" COL="$COL" COM="$COM" COL_CONF="$COL_CONF" COM_CONF="$COM_CONF" \
GIT_COMMIT="$GIT_COMMIT" "$PY" - <<'PY'
import hashlib, json, os, time

def j(name):
    try:
        return json.loads(os.environ.get(name, "") or "{}")
    except Exception:
        return {}

doc = {
    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "topology": {
        "generator": os.environ.get("GENERATOR_HOST"),
        "collector": os.environ.get("COLLECTOR_HOST"),
        "compactor": os.environ.get("COMPACTOR_HOST"),
    },
    "servers": {"generator": j("GEN"), "collector": j("COL"), "compactor": j("COM")},
    "storage": {"kind": "s3", "bucket": os.environ.get("S3_BUCKET"),
                "prefix": os.environ.get("S3_PREFIX"), "region": os.environ.get("AWS_REGION")},
    "catalog": {"kind": "postgres", "host": os.environ.get("PG_HOST"),
                "database": os.environ.get("PG_DATABASE"), "user": os.environ.get("PG_USER")},
    "ducklake": {"catalog": os.environ.get("DUCKLAKE_CATALOG"),
                 "schema": os.environ.get("DUCKLAKE_SCHEMA"),
                 "table": os.environ.get("DUCKLAKE_LOGS_TABLE"),
                 "data_inlining_row_limit": 0},
    "dazzleduck": {"git_commit": os.environ.get("GIT_COMMIT")},
    "effective_config": {
        "collector_application_conf": os.environ.get("COL_CONF", ""),
        "compactor_application_conf": os.environ.get("COM_CONF", ""),
    },
}
# One hash over the whole environment, so a result row can cite it and a later
# reader can tell whether two runs are comparable at all.
payload = json.dumps(doc, sort_keys=True, default=str).encode()
doc["env_hash"] = hashlib.sha256(payload).hexdigest()[:16]
print(json.dumps(doc, indent=2, default=str))
PY
