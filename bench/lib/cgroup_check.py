#!/usr/bin/env python3
"""
Container cgroup placement check and CPU throttling snapshot.

  cgroup_check.py check    --project mwbench --job-id $SLURM_JOB_ID --out cgroups.tsv
      Exit 1 if any running container (or its conmon) is outside job_<id>.
  cgroup_check.py throttle --project mwbench --phase end --out cpu_throttle.log
      Appends cpu.stat (nr_periods, nr_throttled, throttled_usec, usage_usec)
      and cpu.max of each container's real cgroup, plus the job cgroup.
      Errors are written explicitly; nothing is silently reported as N/A.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from podman_util import inspect, proc_cgroup, project_containers  # noqa: E402

CG_ROOT = "/sys/fs/cgroup"


def job_cgroup_dir(path, job_id):
    marker = f"/job_{job_id}"
    i = path.find(marker + "/")
    if i < 0 and path.endswith(marker):
        i = len(path) - len(marker)
    return path[: i + len(marker)] if i >= 0 else None


def cmd_check(args):
    self_cg = proc_cgroup("self")
    rows, bad = [], 0
    containers = project_containers(args.project)
    if not containers:
        print(f"no running containers in project {args.project}", file=sys.stderr)
        return 1
    for name, svc in containers:
        info = inspect(name)
        for label, pid in (("container", info["State"].get("Pid")), ("conmon", info["State"].get("ConmonPid"))):
            if not pid:
                rows.append((name, svc, label, "-", "no pid", "no"))
                bad += 1
                continue
            try:
                cg = proc_cgroup(pid)
            except Exception as exc:
                rows.append((name, svc, label, str(pid), f"ERROR {exc}", "no"))
                bad += 1
                continue
            inside = bool(args.job_id) and f"/job_{args.job_id}/" in cg + "/"
            bad += 0 if inside else 1
            rows.append((name, svc, label, str(pid), cg, "yes" if inside else "no"))
    with open(args.out, "w") as fh:
        fh.write(f"# job_id={args.job_id} job_script_cgroup={self_cg}\n")
        fh.write("container\tservice\tprocess\tpid\tcgroup\tinside_job\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")
    for r in rows:
        print(f"  {r[0]:<34} {r[2]:<9} pid={r[3]:<8} inside_job={r[5]}  {r[4]}")
    if bad:
        print(f"{bad} process(es) outside job_{args.job_id}", file=sys.stderr)
    return 1 if bad else 0


def read_cpu(cg_dir):
    stat = {}
    with open(os.path.join(cg_dir, "cpu.stat")) as fh:
        for line in fh:
            k, v = line.split()
            stat[k] = v
    try:
        with open(os.path.join(cg_dir, "cpu.max")) as fh:
            cpu_max = fh.read().strip()
    except FileNotFoundError:
        cpu_max = "absent (root or cpu controller not enabled)"
    return stat, cpu_max


def cmd_throttle(args):
    lines = [f"# phase={args.phase} captured={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
             "# name\tcgroup\tnr_periods\tnr_throttled\tthrottled_usec\tusage_usec\tcpu.max\tnote"]
    errors = 0
    seen = {}
    try:
        containers = project_containers(args.project)
    except Exception as exc:
        containers = []
        lines.append(f"ERROR\t-\t-\t-\t-\t-\t-\tpodman ps failed: {exc}")
        errors += 1
    targets = []
    for name, _ in containers:
        try:
            pid = inspect(name)["State"]["Pid"]
            targets.append((name, proc_cgroup(pid)))
        except Exception as exc:
            lines.append(f"{name}\t-\t-\t-\t-\t-\t-\tERROR {type(exc).__name__}: {exc}")
            errors += 1
    job_id = os.environ.get("SLURM_JOB_ID", "")
    if job_id:
        jdir = job_cgroup_dir(proc_cgroup("self"), job_id)
        if jdir:
            targets.append((f"job_{job_id}", jdir))
    for name, cg in targets:
        shared = [n for n, c in seen.items() if c == cg]
        seen[name] = cg
        try:
            stat, cpu_max = read_cpu(CG_ROOT + cg)
            note = f"same cgroup as {shared[0]}" if shared else ""
            lines.append("\t".join([name, cg, stat.get("nr_periods", "missing"), stat.get("nr_throttled", "missing"),
                                    stat.get("throttled_usec", "missing"), stat.get("usage_usec", "missing"),
                                    cpu_max, note]))
        except Exception as exc:
            lines.append(f"{name}\t{cg}\t-\t-\t-\t-\t-\tERROR {type(exc).__name__}: {exc}")
            errors += 1
    with open(args.out, "a") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if errors else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--project", required=True)
    c.add_argument("--job-id", default="")
    c.add_argument("--out", required=True)
    t = sub.add_parser("throttle")
    t.add_argument("--project", required=True)
    t.add_argument("--phase", required=True)
    t.add_argument("--out", required=True)
    args = ap.parse_args()
    sys.exit(cmd_check(args) if args.cmd == "check" else cmd_throttle(args))


if __name__ == "__main__":
    main()
