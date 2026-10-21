#!/usr/bin/env python3
"""
Campaign analysis, following bench/EXCLUSION_RULES.md mechanically.

  python3 bench/analysis/summarize.py --results results \
      --out results/analysis/<name> [--ramp-cut spawn|none] [--include-smoke]

Reads results/run_index.tsv and the run folders (harness output only: no
database contents), then:
  1. applies the run-level exclusion rules (setup failure, interruption,
     contaminated node) and writes exclusions.csv with the rule per run
  2. summarises every remaining run on its own (runs.csv): latency per hop and
     batch size, load per stage, delivery per phase (rule 4: not reported if a
     count snapshot had errors), validation, idempotency, durability cases
  3. combines runs per condition (aggregate.csv): median, min, max and n over
     runs. No run is chosen to represent a condition (rule 8). Runs with a
     different harness hash are never pooled (rule 7).
Every option used is written to options.json.
"""
import argparse
import csv
import glob
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict

SETUP_STAGES = {"init", "stack_up", "stack_down", "cgroup_check", "preflight", "settings_snapshot"}


def pct(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return v[int(k)] if f == c else v[f] * (c - k) + v[c] * (k - f)


def jload(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def jlines(path):
    try:
        with open(path) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except OSError:
        return []


def read_index(path):
    final = {}
    with open(path) as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            if row["status"] != "started" or row["job_id"] not in final:
                final[row["job_id"]] = row
    return final


def exclusion(row, run_dir):
    """Rules 1-3. Returns (rule, reason) or (None, '')."""
    reason = row.get("reason", "")
    m = re.match(r"\[([^\]]+)\]", reason)
    stage = m.group(1) if m else ""
    tested = bool(glob.glob(os.path.join(run_dir, "**", "client_summary.json"), recursive=True))
    if row["status"] == "failed" and stage in SETUP_STAGES and not tested:
        return "R1 setup failure", reason
    if "SIGTERM" in reason:
        return "R2 interrupted", reason
    if glob.glob(os.path.join(run_dir, "foreign_containers_*.txt")) or "were running at the end" in reason:
        return "R3 contaminated node", reason
    if row["status"] == "started":
        return "R2 interrupted", "no final run-index row (job killed)"
    return None, ""


def latency_rows(run_dir):
    s = jload(os.path.join(run_dir, "latency", "client_summary.json"), {})
    for r in s.get("table", []):
        yield ("latency", f"hop={r.get('hop', 'default')} size={r['samples']}"), {
            "ok": r["ok"], "iterations": r["iterations"], "p50_ms": r["p50_ms"], "p95_ms": r["p95_ms"],
            "p99_ms": r["p99_ms"], "mean_ms": r["mean_ms"],
            "failures": r["iterations"] - r["ok"]}


def load_rows(run_dir, ramp_cut):
    for sdir in sorted(glob.glob(os.path.join(run_dir, "u*"))):
        s = jload(os.path.join(sdir, "client_summary.json"))
        if not s:
            continue
        reqs = jlines(os.path.join(sdir, "requests.jsonl"))
        t0 = s.get("t_spawn_complete") if ramp_cut == "spawn" and s.get("t_spawn_complete") else s.get("t_start")
        t_end = s.get("t_stage_end") or s.get("t_stop")
        sel = [r for r in reqs if t0 is None or r["ts"] >= t0]
        ok = [r for r in sel if r["cls"] == "ok"]
        window = (t_end - t0) if (t0 and t_end and t_end > t0) else None
        by_cls = defaultdict(int)
        for r in sel:
            by_cls[r["cls"]] += 1
        cu = jload(os.path.join(sdir, "client_usage.json"), {})
        lat = [r["latency_ms"] for r in ok]
        dropped = s.get("client_dropped") or 0
        offered = len(sel) + dropped
        yield ("load", f"stage={os.path.basename(sdir)}"), {
            "requests": len(sel), "ok": len(ok), "client_dropped": dropped, "offered": offered,
            # open loop: arrivals dropped at the client's in-flight cap count as failures
            "failure_rate": (1 - len(ok) / offered) if offered else None,
            "failure_rate_sent_only": (1 - len(ok) / len(sel)) if sel else None,
            **{f"n_{k}": v for k, v in by_cls.items() if k != "ok"},
            "ok_samples_per_s": (sum(r["samples"] for r in ok) / window) if window else None,
            "ok_requests_per_s": (len(ok) / window) if window else None,
            "p50_ms_ok": pct(lat, .5), "p95_ms_ok": pct(lat, .95), "p99_ms_ok": pct(lat, .99),
            "inflight_at_stage_end": s.get("inflight_at_stage_end"),
            "completed_after_stage_end": s.get("completed_after_stage_end"),
            "client_cpu_pct": cu.get("cpu_pct_avg"), "client_bound": cu.get("client_bound")}


def delivery_rows(run_dir):
    for d in jlines(os.path.join(run_dir, "delivery.jsonl")):
        if d.get("problems"):
            yield ("delivery", f"phase={d['label']}"), {"reported": False, "reason": "; ".join(d["problems"])[:120]}
            continue
        out = {"reported": True, "sent": d["sent"], "acked": d["acked"]}
        for k, v in d["stored"].items():
            out[f"stored_{k}"] = v
            out[f"acked_minus_{k}"] = d["acked"] - v if v is not None else None
        yield ("delivery", f"phase={d['label']}"), out


def other_rows(run_dir):
    for v in jlines(os.path.join(run_dir, "validation", "validation.jsonl")):
        yield ("validation", f"probe={v['probe']} {v['defect']}"), {
            "result": v.get("result"), "status": v.get("status"),
            **{f"stored_{k}": x for k, x in (v.get("stored_delta") or {}).items()}}
    i = jload(os.path.join(run_dir, "idempotency", "idempotency.json"))
    if i:
        yield ("idempotency", "repeats=%s" % i["repeats"]), {
            "result": i["result"], **{f"stored_{k}": x for k, x in (i.get("stored_delta") or {}).items()}}
    for c in jlines(os.path.join(run_dir, "durability", "cases.jsonl")):
        if "error" in c:
            yield ("durability", f"{c['target']}/{c['fault']}/rep{c['rep']}"), {"error": c["error"]}
            continue
        r = c.get("restore") or {}
        yield ("durability", f"{c['target']}/{c['fault']}"), {
            "rep": c["rep"], "fault_offset_s": c.get("fault_offset_s"), "self_recovered": c.get("self_recovered"),
            "recovery_s": c.get("recovery_s"), "restored_by_full_restart": r.get("recovered"),
            "sent": c.get("sent"), "acked": c.get("acked"),
            **{f"acked_minus_{k}": v for k, v in (c.get("acked_minus_stored") or {}).items()}}


def node_load(run_dir):
    out = {}
    try:
        for line in open(os.path.join(run_dir, "summary.txt")):
            m = re.match(r"node_state_(\w+): load=(\S+) \S+ \S+ other_jobs=(\d+)", line)
            if m:
                out[f"load1_{m.group(1)}"] = float(m.group(2))
                out[f"other_jobs_{m.group(1)}"] = int(m.group(3))
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ramp-cut", choices=["spawn", "none"], default="spawn",
                    help="load: exclude requests started before spawn completed (pre-registered choice)")
    ap.add_argument("--include-smoke", action="store_true")
    ap.add_argument("--include-pilot", action="store_true", help="analyse pilot runs (never pooled with the campaign)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    index = read_index(os.path.join(args.results, "run_index.tsv"))
    runs, excl = [], []
    for job, row in sorted(index.items()):
        run_dir = row.get("run_dir") or ""
        if not run_dir or not os.path.isdir(run_dir):
            excl.append({"job_id": job, "rule": "R1 setup failure", "reason": row.get("reason") or "no run folder"})
            continue
        st = jload(os.path.join(run_dir, "settings.json"), {})
        if (row.get("smoke") == "1" or st.get("smoke")) and not args.include_smoke:
            continue
        if (row.get("smoke") == "pilot" or st.get("pilot")) and not args.include_pilot:
            continue   # pilot runs are never part of the campaign analysis
        rule, why = exclusion(row, run_dir)
        if rule:
            excl.append({"job_id": job, "rule": rule, "reason": why, "run_dir": run_dir})
            continue
        harness = json.dumps(st.get("harness_sha256_16", {}), sort_keys=True)
        meta = {"job_id": job, "kind": row["kind"], "platform": row["platform"],
                "condition": os.path.basename(row.get("condition") or ""), "status": row["status"],
                "harness": hashlib.sha256(harness.encode()).hexdigest()[:10], "run_dir": os.path.basename(run_dir), **node_load(run_dir)}
        for (test, key), vals in [*latency_rows(run_dir), *load_rows(run_dir, args.ramp_cut),
                                  *delivery_rows(run_dir), *other_rows(run_dir)]:
            runs.append({**meta, "test": test, "key": key, **vals})

    def write(path, rows):
        cols = []
        for r in rows:
            cols += [k for k in r if k not in cols]
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    write(os.path.join(args.out, "runs.csv"), runs)
    write(os.path.join(args.out, "exclusions.csv"), excl)

    groups = defaultdict(list)
    for r in runs:
        groups[(r["test"], r["platform"], r["condition"], r["harness"], r["key"])].append(r)
    agg = []
    for (test, plat, cond, harness, key), rs in sorted(groups.items()):
        out = {"test": test, "platform": plat, "condition": cond, "harness": harness, "key": key,
               "n_runs": len({r["job_id"] for r in rs}), "jobs": " ".join(sorted({r["job_id"] for r in rs}))}
        numeric = sorted({k for r in rs for k, v in r.items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool)
                          and k not in ("rep",) and not k.startswith(("load1_", "other_jobs_"))})
        for k in numeric:
            vals = [r[k] for r in rs if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
            if vals:
                out[f"{k}_median"] = statistics.median(vals)
                out[f"{k}_min"] = min(vals)
                out[f"{k}_max"] = max(vals)
        for k in ("result", "self_recovered", "client_bound", "reported"):
            vals = [str(r[k]) for r in rs if k in r]
            if vals:
                out[f"{k}_counts"] = "; ".join(f"{v}:{vals.count(v)}" for v in sorted(set(vals)))
        agg.append(out)
    write(os.path.join(args.out, "aggregate.csv"), agg)
    with open(os.path.join(args.out, "options.json"), "w") as fh:
        json.dump({"ramp_cut": args.ramp_cut, "include_smoke": args.include_smoke,
                   "aggregation": "per-run summary, then median/min/max over runs (rule 8)",
                   "pooling": "only runs with identical harness hash (rule 7)"}, fh, indent=2)
    harnesses = {r["harness"] for r in runs}
    print(f"runs kept: {len({r['job_id'] for r in runs})}, excluded: {len(excl)}, "
          f"condition groups: {len(agg)}, harness versions: {len(harnesses)}")
    for e in excl:
        print(f"  excluded {e['job_id']}: {e['rule']} ({e['reason'][:80]})")
    print(f"written: {args.out}/runs.csv aggregate.csv exclusions.csv options.json")


if __name__ == "__main__":
    sys.exit(main())
