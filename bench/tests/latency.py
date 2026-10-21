#!/usr/bin/env python3
"""
Per-batch ingest latency vs batch size, same procedure for both platforms.

  PLATFORM=medwear SIZES=100,1000,10000 ITERATIONS=30 BATCH_TIMEOUT=600 \
      latency.py --out-dir $RUN_DIR/latency

* every platform is measured as deployed; MEDWEAR additionally directly at the
  API, alternating hops each iteration, so the nginx+TLS step can be reported
* one requests.Session per hop, 5 unrecorded warm-up requests per size and hop
  (logged with warmup=true and counted in sent/acked, because they are stored)
* BATCH_TIMEOUT is passed to the client; a slower response is a timeout
* every request is logged to requests.jsonl with the server Date header and
  request id (if any) so client and server logs can be joined
* percentiles are over successful requests; failures are counted per class
"""
import argparse
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from platforms import MAX_BATCH_SAMPLES, JsonlWriter, Tally, get_adapter, latency_hops, new_session, post, require_env  # noqa: E402

WARMUP = 5


def pct(vals, p):
    if not vals:
        return float("nan")
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return vals[int(k)] if f == c else vals[f] * (c - k) + vals[c] * (k - f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    sizes = [int(s) for s in require_env("SIZES").split(",")]
    too_big = [s for s in sizes if s > MAX_BATCH_SAMPLES]
    if too_big:
        raise SystemExit(f"sizes {too_big} exceed MAX_BATCH_SAMPLES={MAX_BATCH_SAMPLES}")
    iterations = int(require_env("ITERATIONS"))
    # at most LATENCY_RATE_HZ requests/s (both platforms): sequential requests from
    # one user must not run into MEDWEAR's per-user rate limit (300/min)
    min_gap = 1.0 / float(require_env("LATENCY_RATE_HZ"))
    last = [0.0]

    def paced_post(*a, **kw):
        wait = last[0] + min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()
        return post(*a, **kw)
    timeout = float(require_env("BATCH_TIMEOUT"))
    platform = require_env("PLATFORM")
    hops = latency_hops(platform)
    token_log = os.path.join(args.out_dir, "token_events.jsonl")
    first = get_adapter(platform, token_log, hops[0][1])
    adapters = {name: (first if i == 0 else get_adapter(platform, token_log, url)) for i, (name, url) in enumerate(hops)}
    for a in adapters.values():
        a.tokens = first.tokens          # one token for all hops
    sessions = {name: new_session() for name, _ in hops}
    log = JsonlWriter(os.path.join(args.out_dir, "requests.jsonl"))
    tally = Tally()

    hdr = (f"{'hop':<11} {'samples':>8} {'iters':>6} {'ok':>4} {'tmo':>4} {'auth':>4} {'fail':>4} "
           f"{'mean_ms':>10} {'p50_ms':>10} {'p95_ms':>10} {'p99_ms':>10} {'max_ms':>10} {'samp/s':>10}")
    print(hdr)
    print("-" * len(hdr))
    table = []
    for n in sizes:
        tag = f"lat-{n}"
        for name, _ in hops:
            for i in range(WARMUP):
                res = paced_post(adapters[name], sessions[name], adapters[name].make_batch(n, tag), timeout=timeout)
                tally.add(res, warmup=True)
                log.write({**res, "hop": name, "size": n, "iter": i, "warmup": True})
        lats = {name: [] for name, _ in hops}
        cls_count = {name: {} for name, _ in hops}
        for i in range(iterations):
            # alternate hop order every iteration so drift affects all hops alike
            order = hops if i % 2 == 0 else list(reversed(hops))
            for name, _ in order:
                res = paced_post(adapters[name], sessions[name], adapters[name].make_batch(n, tag), timeout=timeout)
                tally.add(res)
                log.write({**res, "hop": name, "size": n, "iter": i, "warmup": False})
                cls_count[name][res["cls"]] = cls_count[name].get(res["cls"], 0) + 1
                if res["cls"] == "ok":
                    lats[name].append(res["latency_ms"])
                else:
                    print(f"  ! {name} {n} #{i+1}: {res['cls']} status={res['status']} "
                          f"{res.get('body', res.get('error', ''))[:100]}", file=sys.stderr)
        for name, _ in hops:
            L, cc = lats[name], cls_count[name]
            auth = cc.get("auth_401", 0) + cc.get("auth_403", 0)
            tmo = cc.get("client_timeout", 0) + cc.get("server_timeout", 0)
            other = iterations - len(L) - auth - tmo
            row = {"hop": name, "samples": n, "iterations": iterations, "ok": len(L), "by_class": cc,
                   "mean_ms": statistics.mean(L) if L else None, "p50_ms": pct(L, .5) if L else None,
                   "p95_ms": pct(L, .95) if L else None, "p99_ms": pct(L, .99) if L else None,
                   "max_ms": max(L) if L else None}
            table.append(row)
            if L:
                m = row["mean_ms"]
                print(f"{name:<11} {n:>8} {iterations:>6} {len(L):>4} {tmo:>4} {auth:>4} {other:>4} {m:>10.1f} "
                      f"{row['p50_ms']:>10.1f} {row['p95_ms']:>10.1f} {row['p99_ms']:>10.1f} {row['max_ms']:>10.1f} "
                      f"{n / (m / 1000):>10.0f}")
            else:
                print(f"{name:<11} {n:>8} {iterations:>6} {0:>4} {tmo:>4} {auth:>4} {other:>4} " + " ".join(["--".rjust(10)] * 6))
        if len(hops) == 2 and lats[hops[0][0]] and lats[hops[1][0]]:
            a, b = lats[hops[0][0]], lats[hops[1][0]]
            print(f"{'  step':<11} {n:>8}  {hops[0][0]} - {hops[1][0]}: p50 {pct(a, .5) - pct(b, .5):+.1f} ms, "
                  f"p95 {pct(a, .95) - pct(b, .95):+.1f} ms")
    s = tally.dump(os.path.join(args.out_dir, "client_summary.json"),
                   extra={"test": "latency", "platform": platform, "timeout_s": timeout,
                          "hops": [h[0] for h in hops], "table": table}, tokens=first.tokens)
    print(f"client: requests={s['requests']} sent={s['sent_samples']} acked={s['acked_samples']} samples, "
          f"classes={ {k: v for k, v in s['by_class'].items() if v} }")
    log.close()


if __name__ == "__main__":
    main()
