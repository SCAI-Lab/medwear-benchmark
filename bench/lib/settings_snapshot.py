#!/usr/bin/env python3
"""
settings.json: the configuration that was actually used, read from the running
stack. Replaces the round-3 snapshot, which recorded nothing for MEDWEAR and
filled missing variables with defaults (WRITES_ONLY "1", timeouts "60").

  settings_snapshot.py --platform medwear --project mwbench \
      --required "WRITES_ONLY BATCH_TIMEOUT ..." --record "USERS STAGE_SECONDS ..." \
      --out $RUN_DIR/settings.json

Rules: no defaults (a missing required variable exits 2); env values of
containers are never stored, only the keys; errors are recorded as strings.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from podman_util import container_for, inspect, proc_cgroup, project_containers, run  # noqa: E402

SECRET_RE = re.compile(r"PASS|SECRET|TOKEN|_KEY|KEYS|CREDENTIAL|PRIVATE")

VERSION_CMDS = {
    # service -> command run inside the container
    "postgres": ["postgres", "--version"],
    "influxdb": ["influxd", "version"],
    "minio": ["minio", "--version"],
    "keycloak": ["cat", "/opt/keycloak/version.txt"],
    "medwear-nginx": ["nginx", "-v"],
    "med-api": ["python", "-c", "import sys,importlib.metadata as m; print(sys.version.split()[0], *[f'{p}=={m.version(p)}' for p in ('fastapi','uvicorn','sqlalchemy','asyncpg','influxdb-client','pydantic')])"],
    "kafka-1": ["kafka-topics", "--version"],
    "zookeeper": ["sh", "-c", "echo ${CONFLUENT_VERSION:-unknown} ${COMPONENT:-}"],
    "schema-registry": ["sh", "-c", "echo ${CONFLUENT_VERSION:-unknown} ${COMPONENT:-}"],
    "gateway": ["sh", "-c", "java -version 2>&1 | head -1; ls /usr/share/radar-gateway/lib 2>/dev/null | grep -i radar-gateway || true"],
    "managementportal": ["sh", "-c", "java -version 2>&1 | head -1"],
}

API_INTERNALS = r"""
import json, os
out = {"env_DB_POOL_SIZE": os.environ.get("DB_POOL_SIZE"), "env_UVICORN_WORKERS": os.environ.get("UVICORN_WORKERS")}
try:
    from medwear_med_api.db import get_engine
    pool = get_engine(os.environ["BENCH_PROJECT"]).sync_engine.pool
    out.update(pool_class=type(pool).__name__, pool_size=pool.size(),
               max_overflow=getattr(pool, "_max_overflow", None), pool_timeout_s=getattr(pool, "_timeout", None))
except Exception as exc:
    out["pool_error"] = f"{type(exc).__name__}: {exc}"
procs = []
for d in os.listdir("/proc"):
    if d.isdigit():
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode().strip()
        except OSError:
            continue
        if "python" in cmd:
            procs.append(cmd[:200])
out["python_processes"] = sorted(procs)
import importlib.metadata as _md
out["packages"] = sorted(f"{d.metadata['Name']}=={d.version}" for d in _md.distributions())
spawned = [p for p in procs if "multiprocessing" in p]
out["uvicorn_worker_processes"] = len(spawned) if spawned else 1
print(json.dumps(out))
"""


def sh(cmd, timeout=120):
    try:
        r = run(cmd, timeout=timeout)
        txt = (r.stdout + r.stderr).strip()
        return txt if r.returncode == 0 else f"ERROR rc={r.returncode}: {txt[:300]}"
    except Exception as exc:
        return f"ERROR {type(exc).__name__}: {exc}"


def container_record(name, service):
    info = inspect(name)
    img_id = info.get("Image", "")
    img = {}
    r = run(["podman", "image", "inspect", img_id])
    if r.returncode == 0:
        ii = json.loads(r.stdout)[0]
        img = {"id": ii.get("Id"), "digest": ii.get("Digest"), "repo_digests": ii.get("RepoDigests"),
               "repo_tags": ii.get("RepoTags"), "created": ii.get("Created")}
    else:
        img = {"error": r.stderr.strip()[:200]}
    cfg = info.get("Config", {})
    hc = info.get("HostConfig", {})
    pid = info["State"].get("Pid")
    try:
        cg = proc_cgroup(pid) if pid else "no pid"
    except Exception as exc:
        cg = f"ERROR {exc}"
    rec = {
        "service": service,
        "image": info.get("ImageName") or cfg.get("Image"),
        "image_detail": img,
        "container_id": info.get("Id"),
        "created": info.get("Created"),
        "started_at": info["State"].get("StartedAt"),
        "restart_count": info.get("RestartCount"),
        "env_keys": sorted({e.split("=", 1)[0] for e in (cfg.get("Env") or [])}),
        "entrypoint": cfg.get("Entrypoint"),
        "cmd": cfg.get("Cmd"),
        "nano_cpus": hc.get("NanoCpus"),
        "memory_limit": hc.get("Memory"),
        "network_mode": hc.get("NetworkMode"),
        "pid": pid,
        "cgroup": cg,
    }
    if service in VERSION_CMDS:
        rec["version"] = sh(["podman", "exec", name] + VERSION_CMDS[service])
    return rec


def public_env(info, keys):
    env = dict(e.split("=", 1) for e in (info["Config"].get("Env") or []) if "=" in e)
    return {k: ("<redacted>" if SECRET_RE.search(k) else env.get(k, "<unset>")) for k in keys}


def nginx_settings(name):
    txt = sh(["podman", "exec", name, "nginx", "-T"])
    if txt.startswith("ERROR"):
        return {"error": txt}
    out, loc = {}, "server"
    keys = ("proxy_read_timeout", "proxy_send_timeout", "proxy_connect_timeout", "client_max_body_size",
            "keepalive_timeout", "worker_processes", "worker_connections")
    for raw in txt.splitlines():
        line = raw.strip()
        m = re.match(r"location\s+(.+?)\s*\{", line)
        if m:
            loc = m.group(1)
            continue
        for k in keys:
            if line.startswith(k + " "):
                out.setdefault(loc, {})[k] = line[len(k):].strip().rstrip(";")
    return out


def yaml_subset(text, wanted):
    """Grep-level extraction of 'key: value' lines (no PyYAML on the host)."""
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        m = re.match(r"\s*([A-Za-z0-9_.]+):\s*(\S.*)$", line)
        if m and any(w in m.group(1) for w in wanted) and not SECRET_RE.search(m.group(1).upper()):
            out[m.group(1)] = m.group(2).strip()
    return out


def medwear_details(project):
    d = {}
    api = container_for(project, "med-api")
    api_info = inspect(api)
    r = run(["podman", "exec", "-e", "BENCH_PROJECT", api, "python", "-c", API_INTERNALS],
            env_extra={"BENCH_PROJECT": os.environ["BENCH_PROJECT"]})
    try:
        d["api_internals"] = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        d["api_internals"] = {"error": f"rc={r.returncode} {r.stderr.strip()[:300]}"}
    d["api_env"] = public_env(api_info, ["DB_POOL_SIZE", "UVICORN_WORKERS", "BATCH_RATE_LIMIT", "INFLUX_BUCKET", "INFLUX_ORG",
                                         "INFLUX_URL", "INFLUX_CHUNK_SIZE", "POSTGRES_HOST", "KEYCLOAK_URL",
                                         "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID", "LOG_LEVEL"])
    lbl = (api_info.get("Config", {}).get("Labels") or {})
    d["api_image_constraints_sha256"] = lbl.get("bench.api_constraints_sha256", "not built with constraints")
    d["nginx"] = nginx_settings(container_for(project, "medwear-nginx"))
    kc_info = inspect(container_for(project, "keycloak"))
    d["keycloak_env"] = public_env(kc_info, ["KC_DB_POOL_MAX_SIZE", "KC_DB_POOL_MIN_SIZE", "KC_PROXY", "KC_HOSTNAME"])
    d["keycloak_access_token_lifespan_s"] = os.environ.get("KC_TOKEN_LIFESPAN", "not provisioned")
    d["benchmark_project_db"] = os.environ.get("BENCH_PROJECT")
    d["benchmark_user"] = os.environ.get("BENCH_KC_USER")
    d["benchmark_user_role"] = os.environ.get("KC_ROLE")
    pg = container_for(project, "postgres")
    d["postgres_settings"] = sh(["podman", "exec", pg, "sh", "-c",
                                 'psql -U "$POSTGRES_USER" -d postgres -At -F= -c "SELECT name, setting FROM pg_settings '
                                 "WHERE name IN ('max_connections','shared_buffers','synchronous_commit','fsync',"
                                 "'wal_level','checkpoint_timeout','work_mem')\""])
    return d


def radar_details(project):
    d = {}
    gw = container_for(project, "gateway")
    gw_yml = sh(["podman", "exec", gw, "cat", "/etc/radar-gateway/gateway.yml"])
    d["gateway_config"] = (yaml_subset(gw_yml, ["timeout", "Timeout", "maxRequests", "maxRequestSize", "poolSize",
                                                "acks", "linger", "batch", "retries", "max.block", "compression",
                                                "idempotence", "bootstrap", "baseUri", "managementPortalUrl",
                                                "checkSourceId"])
                           if not gw_yml.startswith("ERROR") else {"error": gw_yml})
    kafka = container_for(project, "kafka-1")
    d["kafka_env"] = public_env(inspect(kafka), ["KAFKA_NUM_PARTITIONS", "KAFKA_DEFAULT_REPLICATION_FACTOR",
                                                 "KAFKA_COMPRESSION_TYPE", "KAFKA_NUM_NETWORK_THREADS",
                                                 "KAFKA_NUM_IO_THREADS", "KAFKA_LOG_RETENTION_HOURS",
                                                 "KAFKA_ADVERTISED_LISTENERS"])
    d["kafka_broker_config"] = sh(["podman", "exec", kafka, "sh", "-c",
                                   "kafka-configs --bootstrap-server localhost:9092 --entity-type brokers --entity-name 1 "
                                   "--describe --all | grep -E ' (num\\.(network|io)\\.threads|num\\.partitions|"
                                   "default\\.replication\\.factor|min\\.insync\\.replicas|log\\.flush\\.interval\\.(messages|ms)|"
                                   "compression\\.type|log\\.retention\\.hours)='"])
    d["topic"] = os.environ.get("RADAR_TOPIC")
    d["topic_describe"] = sh(["podman", "exec", kafka, "kafka-topics", "--bootstrap-server", "localhost:9092",
                              "--describe", "--topic", os.environ.get("RADAR_TOPIC", "")])
    d["schema_ids"] = {"key": os.environ.get("RADAR_KEY_SCHEMA_ID"), "value": os.environ.get("RADAR_VALUE_SCHEMA_ID")}
    mp = container_for(project, "managementportal")
    d["managementportal_env"] = public_env(inspect(mp), ["SPRING_PROFILES_ACTIVE", "SERVER_PORT", "JAVA_OPTS",
                                                         "SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE"])
    return d


def harness_fingerprint(bench):
    out = {}
    root = os.path.join(bench, "bench")
    for dirpath, _, files in os.walk(root):
        for f in sorted(files):
            if f.startswith(".env"):
                continue
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, bench)] = hashlib.sha256(fh.read()).hexdigest()[:16]
    return out


def git_state(path):
    head = sh(["git", "-C", path, "rev-parse", "HEAD"])
    diff = subprocess.run(["git", "-C", path, "diff", "HEAD"], capture_output=True)
    status = sh(["git", "-C", path, "status", "--porcelain", "--untracked-files=no"])
    return {"head": head, "dirty_files": status.splitlines() if not status.startswith("ERROR") else status,
            "diff_sha256": hashlib.sha256(diff.stdout).hexdigest() if diff.returncode == 0 else "ERROR"}


def client_versions():
    out = {"python": sh([os.environ.get("BENCH_PYTHON", "python3"), "--version"])}
    out["requests"] = sh([os.environ.get("BENCH_PYTHON", "python3"), "-c", "import requests; print(requests.__version__)"])
    out["locust"] = sh(os.environ.get("LOCUST_CMD", "python3 -m locust").split() + ["--version"])
    return out


def job_cgroup_limits():
    out = {}
    try:
        cg = proc_cgroup("self")
        job = os.environ.get("SLURM_JOB_ID", "")
        i = cg.find(f"/job_{job}")
        base = "/sys/fs/cgroup" + (cg[: i + len(f"/job_{job}")] if job and i >= 0 else cg)
        out["path"] = base
        for f in ("cpu.max", "cpuset.cpus.effective", "memory.max", "memory.swap.max"):
            try:
                with open(os.path.join(base, f)) as fh:
                    out[f] = fh.read().strip()
            except OSError as exc:
                out[f] = f"ERROR {exc.strerror}"
    except Exception as exc:
        out["error"] = str(exc)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--required", required=True)
    ap.add_argument("--record", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    required = args.required.split()
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"required variables unset: {missing}", file=sys.stderr)
        sys.exit(2)
    record = sorted(set(required) | set(args.record.split()) | {"SMOKE", "TARGET_RPS", "OPEN_RATE", "MAX_INFLIGHT",
                                                               "DRAIN_SECONDS", "SETTLE_SECONDS", "DUR_FAIL_BACKOFF_S",
                                                               "DUR_RECOVERY_TIMEOUT_S", "DUR_FREEZE_S", "DUR_KILL_RESTART_S",
                                                               "DUR_RATE_HZ"})
    errors = []
    s = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": args.platform,
        "compose_project": args.project,
        "smoke": os.environ.get("SMOKE") == "1",
        "pilot": os.environ.get("BENCH_PILOT") == "1",
        "slurm": {k: os.environ.get(k) for k in ("SLURM_JOB_ID", "SLURM_JOB_NAME", "SLURM_JOB_NODELIST",
                                                 "SLURM_CPUS_ON_NODE", "SLURM_JOB_CPUS_PER_NODE", "SLURM_MEM_PER_NODE")},
        "job_cgroup_limits": job_cgroup_limits(),
        "condition_file": os.environ.get("BENCH_CONDITION"),
        # values exactly as set; unset ones are recorded as null, never defaulted
        "experiment": {v: (os.environ.get(v) if not SECRET_RE.search(v) else "<redacted>") for v in record},
        "measured_endpoint": os.environ.get("MEASURED_ENDPOINT"),
        "measured_hop": os.environ.get("MEASURED_HOP"),
        "health_url": os.environ.get("HEALTH_URL"),
        "preflight_median_ms": os.environ.get("PREFLIGHT_MEDIAN_MS"),
        "client": client_versions(),
        "harness_sha256_16": harness_fingerprint(os.environ.get("BENCH", os.path.expanduser("~/benchmark"))),
        "containers": {},
    }
    if args.platform == "medwear":
        # MEDWEAR code = git archive of one commit (bench/medwear_src/<rev>/VERSION)
        vf = os.path.join(os.environ.get("MEDWEAR_SRC", ""), "VERSION")
        try:
            with open(vf) as fh:
                s["medwear_source"] = dict(l.rstrip("\n").split("=", 1) for l in fh if "=" in l)
        except OSError as exc:
            errors.append(f"medwear source VERSION: {exc}")
    try:
        containers = project_containers(args.project)
    except Exception as exc:
        containers = []
        errors.append(f"podman ps: {exc}")
    job_start = float(os.environ.get("BENCH_JOB_START_EPOCH", "0"))
    for name, svc in containers:
        try:
            s["containers"][name] = container_record(name, svc)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    s["stack_created_inside_job"] = {n: c.get("created") for n, c in s["containers"].items()}
    s["job_start_epoch"] = job_start
    try:
        s["platform_details"] = medwear_details(args.project) if args.platform == "medwear" else radar_details(args.project)
    except Exception as exc:
        errors.append(f"platform details: {type(exc).__name__}: {exc}")
    s["errors"] = errors
    with open(args.out, "w") as fh:
        json.dump(s, fh, indent=2, default=str)
    print(f"settings.json written ({len(s['containers'])} containers, {len(errors)} errors)")
    if args.platform == "medwear":
        ai = s.get("platform_details", {}).get("api_internals", {})
        print(f"  API (read inside container): pool_size={ai.get('pool_size')} max_overflow={ai.get('max_overflow')} "
              f"workers={ai.get('uvicorn_worker_processes')}")
    for e in errors:
        print(f"  ERROR {e}", file=sys.stderr)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
