#!/usr/bin/env python3
"""
sent / HTTP-acked / stored, all in samples, for one test phase.

  delivery.py --label u10 --counts counts.jsonl --before u10_before --after u10_after \
      --out delivery.jsonl u10/client_summary.json [more summaries...]

client_summary.json (written by every bench client) carries sent_samples and
acked_samples, including warm-up requests, since those are stored too.
"""
import argparse
import json


def load_phase(path, phase):
    found = None
    with open(path) as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("phase") == phase:
                found = rec
    if found is None:
        raise SystemExit(f"phase {phase!r} not found in {path}")
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--counts", required=True)
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("summaries", nargs="+")
    args = ap.parse_args()

    sent = acked = 0
    for p in args.summaries:
        with open(p) as fh:
            s = json.load(fh)
        sent += int(s["sent_samples"])
        acked += int(s["acked_samples"])

    b, a = load_phase(args.counts, args.before), load_phase(args.counts, args.after)
    stores = ["pg_samples", "influx_points"]
    stored, problems = {}, list(b.get("errors", [])) + list(a.get("errors", []))
    for k in stores:
        if b.get(k) is None or a.get(k) is None:
            stored[k] = None
            problems.append(f"{k} missing")
        else:
            stored[k] = a[k] - b[k]
    ok = not problems and all(v == acked for v in stored.values()) and sent == acked
    rec = {"label": args.label, "unit": "samples", "sent": sent, "acked": acked, "stored": stored,
           "sent_eq_acked_eq_stored": ok, "problems": problems,
           "before_phase": args.before, "after_phase": args.after}
    with open(args.out, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    st = " ".join(f"stored_{k}={v}" for k, v in stored.items())
    verdict = "OK (sent=acked=stored)" if ok else "MISMATCH" + (f" {problems}" if problems else "")
    print(f"DELIVERY {args.label}: sent={sent} acked={acked} {st} samples -> {verdict}")


if __name__ == "__main__":
    main()
