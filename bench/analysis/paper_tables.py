#!/usr/bin/env python3
"""
Tables of the paper's benchmark section, from the run folders of one or more
campaigns.

  python3 bench/analysis/paper_tables.py --results results \
      --manifest results/campaigns/20261020_093630_paper_round4.tsv:radar \
      --manifest results/campaigns/20261023_192526_medwear_sessions_round5.tsv:medwear \
      --out results/paper

A manifest given as PATH:PLATFORM contributes only the jobs of that platform
(first word of the condition name). Reads only harness output
(client_summary.json, validation.jsonl, requests.jsonl) of the listed jobs and
writes:
  latency.csv     per platform, hop and batch size: median over jobs of the
                  per-job p50 and p95 (ms), and their range over jobs
  validation.csv  per malformed request and platform: HTTP status and whether
                  anything was stored, with the number of jobs that agree
  load.csv        per condition and level N: share of requests acknowledged
                  with HTTP 200 (open loop: of offered arrivals, including those
                  dropped at the client's in-flight cap), acknowledged requests
                  per second, share answered with HTTP 5xx, and median latency
                  of acknowledged requests; median and range over jobs

Load is computed by summarize.load_rows with the pre-registered ramp cut
(bench/EXCLUSION_RULES.md): requests started before the last simulated device
spawned are excluded.
"""
import argparse
import csv
import glob
import json
import os
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import summarize  # noqa: E402  (pre-registered load metrics)


def manifest_jobs(spec):
    path, _, platform = spec.partition(":")
    jobs = []
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) >= 4 and p[1].isdigit():
                cond = os.path.basename(p[3]).removesuffix(".env")
                if not platform or cond.split("_")[0] == platform:
                    jobs.append((p[1], p[2], cond))
    return jobs


def run_dir(results, job):
    d = [x for r in results for x in glob.glob(os.path.join(r, f"*_{job}"))]
    return d[0] if len(d) == 1 else None


def jsonl(path):
    with open(path) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def write(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, action="append", help="folder of run folders (repeatable)")
    ap.add_argument("--manifest", required=True, action="append", help="PATH or PATH:PLATFORM (repeatable)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    lat, val, load = defaultdict(list), defaultdict(list), defaultdict(list)
    desc = {}
    for job, test, cond in (j for m in a.manifest for j in manifest_jobs(m)):
        d = run_dir(a.results, job)
        if d is None:
            raise SystemExit(f"run folder of job {job} not found")
        if test == "latency":
            s = json.load(open(os.path.join(d, "latency", "client_summary.json")))
            plat = s.get("platform", cond.split("_")[0])
            for r in s["table"]:
                lat[(plat, r.get("hop", "default"), r["samples"])].append((r["p50_ms"], r["p95_ms"], r["ok"], r["iterations"]))
        elif test == "validation":
            for r in jsonl(os.path.join(d, "validation", "validation.jsonl")):
                if "probe" not in r:
                    continue
                sd = {k: v for k, v in (r.get("stored_delta") or {}).items() if isinstance(v, (int, float))}
                stored = "n/a" if r.get("result") == "not_applicable" else ("yes" if any(v > 0 for v in sd.values()) else "no")
                val[(r["probe"], r["platform"])].append((r.get("status"), stored))
                desc[r["probe"]] = (r.get("defect", ""), r.get("description", ""))
        elif test == "load":
            for (_, key), m in summarize.load_rows(d, "spawn"):
                n = int(key.split("=u")[1])
                load[(cond, n)].append((100 * (1 - m["failure_rate"]), m["p50_ms_ok"] / 1000,
                                        m["ok_requests_per_s"], 100 * m.get("n_server_5xx", 0) / m["offered"]))

    write(os.path.join(a.out, "latency.csv"), [
        dict(platform=p, hop=h, samples_per_batch=n, n_jobs=len(v),
             p50_ms=round(st.median(x[0] for x in v), 1), p95_ms=round(st.median(x[1] for x in v), 1),
             p50_ms_min=round(min(x[0] for x in v), 1), p50_ms_max=round(max(x[0] for x in v), 1),
             p95_ms_min=round(min(x[1] for x in v), 1), p95_ms_max=round(max(x[1] for x in v), 1),
             requests_ok=sum(x[2] for x in v), requests=sum(x[3] for x in v))
        for (p, h, n), v in sorted(lat.items())])
    rows = []
    for (probe, plat), v in sorted(val.items()):
        status, stored = v[0]
        rows.append(dict(probe=probe, defect=desc[probe][0], description=desc[probe][1], platform=plat,
                         http_status="" if status is None else status, stored=stored,
                         n_jobs=len(v), jobs_agreeing=sum(x == v[0] for x in v)))
    write(os.path.join(a.out, "validation.csv"), rows)
    write(os.path.join(a.out, "load.csv"), [
        dict(condition=c, n=n, n_jobs=len(v),
             ok_pct=round(st.median(x[0] for x in v), 1),
             ok_pct_min=round(min(x[0] for x in v), 1), ok_pct_max=round(max(x[0] for x in v), 1),
             ok_per_s=round(st.median(x[2] for x in v), 1),
             http5xx_pct_min=round(min(x[3] for x in v), 1), http5xx_pct_max=round(max(x[3] for x in v), 1),
             p50_s=round(st.median(x[1] for x in v), 3))
        for (c, n), v in sorted(load.items())])
    print(f"wrote {a.out}/latency.csv, validation.csv, load.csv")


if __name__ == "__main__":
    main()
