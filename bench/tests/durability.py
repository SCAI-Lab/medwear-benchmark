#!/usr/bin/env python3
"""
Fault injection during writes, same fault list for both platforms.

Targets (compose services of the isolated stack), by role:
  role      medwear                      radar
  store     postgres, influxdb, minio    kafka-1, schema-registry, zookeeper
  ingress   med-api, medwear-nginx       gateway
  auth      keycloak                     managementportal
Faults:
  restart   graceful: podman restart -t 10 (SIGTERM, 10 s grace)
  sigkill   podman kill -s KILL, then podman start after DUR_KILL_RESTART_S
  freeze    SIGSTOP the container's whole process tree for DUR_FREEZE_S, then
            SIGCONT ("Kafka pause"). podman pause needs a freezer cgroup, which
            containers inside the Slurm job do not have.

For each (target, fault) DUR_REPS repetitions (>= 5 unless SMOKE), each with
the fault at a different offset in the write window (stratified over
[DUR_KILL_MIN_S, DUR_WRITE_SECONDS - 5], seeded by DUR_SEED). A single
writer sends 256-sample batches for DUR_WRITE_SECONDS, paced at DUR_RATE_HZ
batches/s (device-like; keeps MEDWEAR's per-user rate limit out of the test). After the
writer stops: wait for platform health, settle, count. Per case:
  sent / acked / stored samples (stored = count delta per store, same unit for
  PG, Influx and Kafka), lost_acked = acked - stored if positive.

  PLATFORM=medwear DUR_REPS=5 DUR_WRITE_SECONDS=30 BATCH_TIMEOUT=60 \
      durability.py --out-dir $RUN_DIR/durability
"""
import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from platforms import JsonlWriter, Tally, get_adapter, new_session, post, require_env  # noqa: E402
import counts  # noqa: E402
from podman_util import container_for, descendants, inspect, project_containers  # noqa: E402

TARGETS = {
    "medwear": {"postgres": "store", "influxdb": "store", "minio": "store",
                "med-api": "ingress", "medwear-nginx": "ingress", "keycloak": "auth"},
    "radar": {"kafka-1": "store", "schema-registry": "store", "zookeeper": "store",
              "gateway": "ingress", "managementportal": "auth"},
}
PROJECT = {"medwear": "mwbench", "radar": "rdbench"}
# start order for a full stack restart (stop runs in reverse)
START_ORDER = {
    "medwear": ["postgres", "influxdb", "minio", "keycloak", "med-api", "frontend", "medwear-nginx"],
    "radar": ["zookeeper", "kafka-1", "schema-registry", "managementportal", "gateway"],
}
FAULTS = ("restart", "sigkill", "freeze")
BATCH = 256


def podman(*args, timeout=180):
    r = subprocess.run(["podman", *args], capture_output=True, text=True, timeout=timeout)
    return {"cmd": " ".join(args), "rc": r.returncode, "stderr": r.stderr.strip()[:200]}


def signal_tree(name, sig):
    pid = inspect(name)["State"]["Pid"]
    pids = descendants(pid)
    for p in pids:
        try:
            os.kill(p, sig)
        except ProcessLookupError:
            pass
    return len(pids)


def inject(fault, name, env):
    t = time.time()
    if fault == "restart":
        steps = [podman("restart", "-t", "10", name)]
    elif fault == "sigkill":
        steps = [podman("kill", "-s", "KILL", name)]
        time.sleep(env["kill_restart_s"])
        steps.append(podman("start", name))
    elif fault == "freeze":
        n = signal_tree(name, signal.SIGSTOP)
        steps = [{"cmd": f"SIGSTOP {n} processes", "rc": 0}]
        time.sleep(env["freeze_s"])
        n = signal_tree(name, signal.SIGCONT)
        steps.append({"cmd": f"SIGCONT {n} processes", "rc": 0})
    else:
        raise ValueError(fault)
    return {"fault_start": t, "fault_end": time.time(), "steps": steps}


def stopped_containers(project):
    names = {n for n, _ in project_containers(project, running_only=False)}
    running = {n for n, _ in project_containers(project)}
    return names - running - {n for n in names if n.endswith("minio-setup")}


def wait_recovered(adapter, project, timeout_s):
    t0 = time.time()
    session = new_session()
    while time.time() - t0 < timeout_s:
        if not stopped_containers(project) and adapter.health_ok(session):
            return time.time() - t0
        time.sleep(2)
    return None


def full_restart(platform):
    """Stop every service of the stack (reverse order), start them again in
    dependency order. Used only to restore a stack that did not recover on its
    own, so the remaining cases can run; recorded per case."""
    project = PROJECT[platform]
    names = [container_for(project, svc) for svc in START_ORDER[platform]]
    steps = [podman("stop", "-t", "10", n) for n in reversed(names)]
    steps += [podman("start", n) for n in names]
    return steps


def stored(platform):
    snap = counts.snapshot(platform)
    return counts.stored_samples(snap), snap["errors"]


def run_case(adapter, platform, target, fault, rep, offset, env, log):
    project = PROJECT[platform]
    name = container_for(project, target)
    before, err_b = stored(platform)
    tally = Tally()
    stop_at = time.time() + env["write_seconds"]
    session = new_session()
    tag = f"dur-{target}-{fault}-{rep}"

    def writer():
        interval = 1.0 / env["rate_hz"]
        next_t = time.time()
        while time.time() < stop_at:
            res = post(adapter, session, adapter.make_batch(BATCH, tag), timeout=env["timeout"])
            tally.add(res)
            log.write({**res, "case": tag})
            if res["cls"] != "ok":
                time.sleep(env["fail_backoff_s"])   # device-like retry cadence, same on both platforms
            # paced like a device (DUR_RATE_HZ batches/s), never faster
            next_t += interval
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()

    th = threading.Thread(target=writer, daemon=True)
    t_start = time.time()
    th.start()
    time.sleep(offset)
    fault_info = inject(fault, name, env)
    th.join()
    recovery = wait_recovered(adapter, project, env["recovery_timeout"])
    restore = None
    if recovery is None:
        # did not recover on its own: record that, restore the whole stack so the
        # remaining cases can run, and record whether the restore worked
        t_r = time.time()
        steps = full_restart(platform)
        back = wait_recovered(adapter, project, env["recovery_timeout"])
        restore = {"method": "full stack restart (dependency order)", "steps": steps,
                   "recovered": back is not None, "seconds": round(time.time() - t_r, 1)}
    time.sleep(env["settle_s"])
    after, err_a = stored(platform)
    d = {k: (after[k] - before[k]) if after.get(k) is not None and before.get(k) is not None else None for k in before}
    sample_stores = {k: v for k, v in d.items() if k != "pg_batches"}
    lost = {k: (tally.acked_samples - v) if v is not None else None for k, v in sample_stores.items()}
    return {"platform": platform, "target": target, "role": TARGETS[platform][target], "fault": fault, "rep": rep,
            "fault_offset_s": round(offset, 2), "write_window_s": env["write_seconds"], "t_start": t_start,
            **fault_info, "self_recovered": recovery is not None, "recovery_s": recovery,
            "recovery_timeout_s": env["recovery_timeout"], "restore": restore,
            "unit": "samples", "sent": tally.sent_samples, "acked": tally.acked_samples,
            "requests_by_class": {k: v for k, v in tally.by_class.items() if v},
            "stored_delta": d, "acked_minus_stored": lost, "count_errors": err_b + err_a}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    platform = require_env("PLATFORM")
    reps = int(require_env("DUR_REPS"))
    if reps < 5 and os.environ.get("SMOKE") != "1":
        raise SystemExit("DUR_REPS must be >= 5 (n >= 5 per case) outside SMOKE mode")
    env = {"write_seconds": float(require_env("DUR_WRITE_SECONDS")), "timeout": float(require_env("BATCH_TIMEOUT")),
           "kill_restart_s": float(os.environ.get("DUR_KILL_RESTART_S") or 2),
           "freeze_s": float(os.environ.get("DUR_FREEZE_S") or 10),
           "recovery_timeout": float(os.environ.get("DUR_RECOVERY_TIMEOUT_S") or 300),
           "settle_s": float(os.environ.get("DUR_SETTLE_S") or 10),
           "kill_min_s": float(os.environ.get("DUR_KILL_MIN_S") or 2),
           "fail_backoff_s": float(os.environ.get("DUR_FAIL_BACKOFF_S") or 0.5),
           "rate_hz": float(require_env("DUR_RATE_HZ"))}
    seed = int(os.environ.get("DUR_SEED") or 20261016)
    targets = (os.environ.get("DUR_TARGETS") or ",".join(TARGETS[platform])).split(",")
    faults = (os.environ.get("DUR_FAULTS") or ",".join(FAULTS)).split(",")
    bad = [t for t in targets if t not in TARGETS[platform]] + [f for f in faults if f not in FAULTS]
    if bad:
        raise SystemExit(f"unknown targets/faults: {bad}")
    hi = env["write_seconds"] - 5
    if hi <= env["kill_min_s"]:
        raise SystemExit("DUR_WRITE_SECONDS too short for the fault window")
    rng = random.Random(seed)
    adapter = get_adapter(platform, os.path.join(args.out_dir, "token_events.jsonl"))
    reqlog = JsonlWriter(os.path.join(args.out_dir, "requests.jsonl"))
    cases = JsonlWriter(os.path.join(args.out_dir, "cases.jsonl"))
    total = Tally()
    with open(os.path.join(args.out_dir, "plan.json"), "w") as fh:
        json.dump({"targets": targets, "faults": faults, "reps": reps, "seed": seed, **env}, fh, indent=2)

    print(f"{'target':<16} {'fault':<8} {'rep':>3} {'t_fault':>7} {'sent':>7} {'acked':>7} {'stored':<44} {'recov_s':>8}")
    print("-" * 110)
    aborted = None
    for target in targets:
        for fault in faults:
            width = (hi - env["kill_min_s"]) / reps
            for rep in range(reps):
                offset = env["kill_min_s"] + (rep + rng.random()) * width
                try:
                    c = run_case(adapter, platform, target, fault, rep, offset, env, reqlog)
                except Exception as exc:
                    c = {"platform": platform, "target": target, "fault": fault, "rep": rep,
                         "error": f"{type(exc).__name__}: {exc}"}
                cases.write(c)
                total.sent_samples += c.get("sent", 0)
                total.acked_samples += c.get("acked", 0)
                rs = c.get("recovery_s")
                rec = ("%.1f" % rs) if rs is not None else ("restored" if (c.get("restore") or {}).get("recovered") else "NO")
                print(f"{target:<16} {fault:<8} {rep:>3} {c.get('fault_offset_s', 0):>7} {c.get('sent', '-'):>7} "
                      f"{c.get('acked', '-'):>7} {json.dumps(c.get('stored_delta', c.get('error'))):<44} "
                      f"{rec:>8}")
                if "error" in c:
                    aborted = f"{target}/{fault}/rep{rep}: {c['error']}"
                elif c.get("restore") and not c["restore"]["recovered"]:
                    aborted = f"{target}/{fault}/rep{rep}: no self-recovery and full stack restart failed"
                if aborted:
                    break
            if aborted:
                break
        if aborted:
            break
    total.dump(os.path.join(args.out_dir, "client_summary.json"), extra={"test": "durability", "aborted": aborted},
               tokens=adapter.tokens)
    if aborted:
        print(f"ABORTED: {aborted}")
        sys.exit(3)


if __name__ == "__main__":
    main()
