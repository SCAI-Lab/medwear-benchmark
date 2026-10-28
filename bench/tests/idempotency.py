#!/usr/bin/env python3
"""
Duplicate-submission probe: send one identical request REPEATS times and
record status codes plus the stored-sample delta (PG batches/samples and
Influx points). Idempotent storage = delta of exactly one batch.

  PLATFORM=medwear idempotency.py --out-dir $RUN_DIR/idempotency --repeats 5
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from platforms import Tally, get_adapter, new_session, post, require_env  # noqa: E402
import counts  # noqa: E402

N = 256


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--settle", type=float, default=3.0)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    platform = require_env("PLATFORM")
    timeout = float(require_env("BATCH_TIMEOUT"))
    adapter = get_adapter(platform, os.path.join(args.out_dir, "token_events.jsonl"))
    session = new_session()
    tally = Tally()
    payload = adapter.make_batch(N, "idempotency")   # fixed uuid / timestamps for all repeats

    b = counts.snapshot(platform)
    attempts = []
    for i in range(args.repeats):
        res = post(adapter, session, payload, timeout=timeout)
        tally.add(res)
        attempts.append({"attempt": i + 1, "status": res["status"], "cls": res["cls"], "latency_ms": res["latency_ms"],
                         "response": res.get("body")})
        print(f"  attempt {i+1}: HTTP {res['status']} ({res['cls']}) {res['latency_ms']:.1f} ms")
        time.sleep(0.5)
    time.sleep(args.settle)
    a = counts.snapshot(platform)
    sb, sa = counts.stored_samples(b), counts.stored_samples(a)
    d = {k: (sa[k] - sb[k]) if sa.get(k) is not None and sb.get(k) is not None else None for k in sb}
    errors = b["errors"] + a["errors"]
    sample_keys = [k for k in d if k != "pg_batches"]
    if errors or any(d[k] is None for k in sample_keys):
        verdict = f"inconclusive (count errors: {errors})"
    elif all(d[k] == N for k in sample_keys):
        verdict = "stored once"
    else:
        verdict = f"stored {d} for {args.repeats} identical submissions of {N} samples"
    out = {"platform": platform, "samples_per_request": N, "repeats": args.repeats, "attempts": attempts,
           "stored_delta": d, "count_errors": errors, "result": verdict}
    with open(os.path.join(args.out_dir, "idempotency.json"), "w") as fh:
        json.dump(out, fh, indent=2)
    tally.dump(os.path.join(args.out_dir, "client_summary.json"), extra={"test": "idempotency"}, tokens=adapter.tokens)
    print(f"stored delta: {d}")
    print(f"result: {verdict}")


if __name__ == "__main__":
    main()
