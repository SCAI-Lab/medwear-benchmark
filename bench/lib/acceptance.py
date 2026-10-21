#!/usr/bin/env python3
"""
Acceptance checks for a smoke run (SMOKE=1), from the round-3 review:
  1 settings.json has real values (no unset experiment variables), container
    versions and image digests, and (MEDWEAR) pool size / workers read from
    inside the API container
  2 all container cgroups inside the job; preflight health median < limit
  3 cpu_throttle.log has numbers or an explicit ERROR, never a silent N/A
  4 start and end counts present; sent = acked = stored for the clean phases
  5 no audit.csv and no database dumps in the result folder

  acceptance.py $RUN_DIR      (prints PASS/FAIL per check, exit 1 on any FAIL)
"""
import json
import os
import re
import sys


def main():
    run = sys.argv[1]
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))

    try:
        s = json.load(open(os.path.join(run, "settings.json")))
    except Exception as exc:
        s = None
        check("1 settings.json readable", False, str(exc))
    if s:
        unset = [k for k, v in s["experiment"].items() if v is None and k in os.environ.get("BENCH_REQUIRED", "").split()]
        check("1a required experiment variables recorded", not unset, f"unset: {unset}" if unset else "")
        c = s.get("containers", {})
        no_digest = [n for n, r in c.items() if not (r.get("image_detail") or {}).get("digest")]
        check("1b image digest for every container", bool(c) and not no_digest, f"missing: {no_digest}")
        no_ver = [n for n, r in c.items() if "version" in r and str(r["version"]).startswith("ERROR")]
        check("1c versions read from running containers", not no_ver, f"errors: {no_ver}")
        if s["platform"] == "medwear":
            ai = s.get("platform_details", {}).get("api_internals", {})
            ok = ai.get("pool_size") is not None and ai.get("uvicorn_worker_processes") is not None
            match = str(ai.get("pool_size")) == str(s["experiment"].get("DB_POOL_SIZE"))
            check("1d API pool/workers read inside container and match condition", ok and match,
                  f"pool_size={ai.get('pool_size')} max_overflow={ai.get('max_overflow')} "
                  f"workers={ai.get('uvicorn_worker_processes')} DB_POOL_SIZE={s['experiment'].get('DB_POOL_SIZE')}")
        check("1e no snapshot errors", not s.get("errors"), str(s.get("errors")))

    try:
        rows = [l.rstrip("\n").split("\t") for l in open(os.path.join(run, "cgroups_start.tsv")) if not l.startswith("#")]
        outside = [r[0] for r in rows[1:] if r[-1] != "yes"]
        check("2a container cgroups inside job", rows[1:] != [] and not outside, f"outside: {outside}")
    except Exception as exc:
        check("2a container cgroups inside job", False, str(exc))
    summ = open(os.path.join(run, "summary.txt")).read() if os.path.exists(os.path.join(run, "summary.txt")) else ""
    m = re.search(r"preflight_start median_ms=([\d.]+)", summ)
    lim = float(os.environ.get("PREFLIGHT_MAX_MS", "100"))
    check("2b preflight median < limit", bool(m) and float(m.group(1)) <= lim, m.group(0) if m else "no preflight line")

    try:
        txt = open(os.path.join(run, "cpu_throttle.log")).read()
        data = [l for l in txt.splitlines() if l and not l.startswith("#")]
        silent = [l for l in data if "N/A" in l]
        numeric = [l for l in data if re.match(r"[^\t]+\t[^\t]+\t\d+\t\d+\t\d+", l)]
        explicit_err = [l for l in data if "ERROR" in l]
        check("3 cpu_throttle.log numbers or explicit errors", bool(data) and not silent and
              len(numeric) + len(explicit_err) == len(data), f"{len(numeric)} numeric, {len(explicit_err)} ERROR")
    except Exception as exc:
        check("3 cpu_throttle.log numbers or explicit errors", False, str(exc))

    try:
        phases = [json.loads(l) for l in open(os.path.join(run, "counts.jsonl"))]
        errs = [p["phase"] for p in phases if p.get("errors")]
        check("4a start/end counts present without errors", len(phases) >= 2 and not errs, f"errors in {errs}")
        deliv = {d["label"]: d for d in map(json.loads, open(os.path.join(run, "delivery.jsonl")))}
        clean = [k for k in ("latency", "load") if k in deliv]
        bad = [k for k in clean if not deliv[k]["sent_eq_acked_eq_stored"]]
        check("4b sent = acked = stored (latency, load)", bool(clean) and not bad,
              "; ".join(f"{k}: sent={deliv[k]['sent']} acked={deliv[k]['acked']} stored={deliv[k]['stored']}" for k in clean))
    except Exception as exc:
        check("4 counts / delivery", False, str(exc))

    forbidden = []
    for dirpath, _, files in os.walk(run):
        for f in files:
            if f == "audit.csv" or re.search(r"\.(sql|dump|bak)$", f) or "audit" in f.lower():
                forbidden.append(os.path.relpath(os.path.join(dirpath, f), run))
    check("5 no audit.csv / DB dumps in result folder", not forbidden, f"found: {forbidden}")

    print("---------------- ACCEPTANCE (smoke) ----------------")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"acceptance: {len(results) - n_fail}/{len(results)} passed")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
