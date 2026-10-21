#!/usr/bin/env python3
"""
Per-container CPU / memory sampler (1 Hz) for the isolated bench stacks.

Inside a Slurm job the containers run with cgroups disabled and share the job
cgroup, so cgroup files would give the job total for every container. This
sampler therefore sums /proc of each container's process tree:
  cpu_pct   = delta(utime+stime of all processes) / delta(wall) * 100
  rss_mb    = sum of VmRSS (shared pages are counted per process)
The job-level total from the shared cgroup is written as row "_job_cgroup".

  profile_containers.py --project mwbench --output $RUN_DIR/profile.csv
Stops on SIGTERM/SIGINT and prints a per-container summary.
"""
import argparse
import csv
import math
import os
import signal
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from podman_util import descendants, inspect, proc_cgroup, project_containers  # noqa: E402

TICK = os.sysconf("SC_CLK_TCK")


def tree_usage(root_pid):
    cpu_ticks, rss_kb = 0, 0
    for pid in descendants(root_pid):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            cpu_ticks += int(fields[11]) + int(fields[12])
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss_kb += int(line.split()[1])
                        break
        except (OSError, IndexError, ValueError):
            continue
    return cpu_ticks / TICK, rss_kb / 1024


def job_cgroup_usage():
    job = os.environ.get("SLURM_JOB_ID")
    if not job:
        return None
    cg = proc_cgroup("self")
    i = cg.find(f"/job_{job}")
    if i < 0:
        return None
    base = "/sys/fs/cgroup" + cg[: i + len(f"/job_{job}")]
    with open(f"{base}/cpu.stat") as fh:
        usec = next(int(l.split()[1]) for l in fh if l.startswith("usage_usec"))
    with open(f"{base}/memory.current") as fh:
        mem = int(fh.read()) / (1024 * 1024)
    return usec / 1e6, mem


def pct(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return vals[int(k)] if f == c else vals[f] * (c - k) + vals[c] * (k - f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    def roots():
        out = {}
        for name, _ in project_containers(args.project):
            try:
                pid = inspect(name)["State"]["Pid"]
                if pid:
                    out[name] = pid
            except Exception as exc:
                print(f"WARN {name}: {exc}", file=sys.stderr)
        return out

    pids = roots()
    if not pids:
        print(f"no running containers in {args.project}", file=sys.stderr)
        sys.exit(2)
    prev = {n: tree_usage(p)[0] for n, p in pids.items()}
    try:
        prev_job = job_cgroup_usage()
    except Exception as exc:
        print(f"WARN job cgroup: {exc}", file=sys.stderr)
        prev_job = None
    prev_t = time.monotonic()
    series = {}
    t_start = prev_t
    with open(args.output, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "container", "cpu_pct", "rss_mb", "source"])
        while not stop["flag"]:
            time.sleep(args.interval)
            now = time.monotonic()
            dt = now - prev_t
            wall = time.time()
            # containers can be restarted (durability): re-resolve pids each round
            cur_pids = roots()
            for name, pid in cur_pids.items():
                cpu_s, rss = tree_usage(pid)
                last = prev.get(name, cpu_s)
                cpu_pct = max(0.0, (cpu_s - last) / dt * 100) if pid == pids.get(name) else 0.0
                prev[name] = cpu_s
                w.writerow([f"{wall:.3f}", name, f"{cpu_pct:.2f}", f"{rss:.1f}", "proc"])
                series.setdefault(name, ([], []))
                series[name][0].append(cpu_pct)
                series[name][1].append(rss)
            pids = cur_pids
            try:
                job = job_cgroup_usage()
                if job and prev_job:
                    jp = (job[0] - prev_job[0]) / dt * 100
                    w.writerow([f"{wall:.3f}", "_job_cgroup", f"{jp:.2f}", f"{job[1]:.1f}", "cgroup"])
                    series.setdefault("_job_cgroup", ([], []))
                    series["_job_cgroup"][0].append(jp)
                    series["_job_cgroup"][1].append(job[1])
                prev_job = job
            except Exception as exc:
                print(f"WARN job cgroup: {exc}", file=sys.stderr)
            fh.flush()
            prev_t = now
            if args.duration and now - t_start >= args.duration:
                break

    hdr = f"{'container':<34} {'cpu_mean%':>10} {'cpu_p95%':>10} {'cpu_max%':>10} {'rss_mean_MB':>12} {'rss_max_MB':>12}"
    print(hdr)
    print("-" * len(hdr))
    for name, (cs, ms) in series.items():
        print(f"{name:<34} {statistics.mean(cs):>10.2f} {pct(cs, 0.95):>10.2f} {max(cs):>10.2f} "
              f"{statistics.mean(ms):>12.1f} {max(ms):>12.1f}")


if __name__ == "__main__":
    main()
