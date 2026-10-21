#!/usr/bin/env python3
"""
Input validation probes: ONE shared defect list for both platforms.

Each probe starts from the same valid request (V00 is the positive control)
and changes exactly one thing. For every probe we record the HTTP status,
the failure class and the stored-sample delta (counts only), so "rejected"
and "accepted but not stored" / "accepted and stored" are distinguishable.
Defects with no equivalent on a platform are recorded as not_applicable with
the reason, never skipped silently.

  PLATFORM=radar validation.py --out-dir $RUN_DIR/validation
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from platforms import JsonlWriter, deep, get_adapter, new_session, post, require_env  # noqa: E402
import counts  # noqa: E402

N, HZ = 256, 256.0


def _del(d, *path):
    for p in path[:-1]:
        d = d[p]
    del d[path[-1]]


def _set(d, value, *path):
    for p in path[:-1]:
        d = d[p]
    d[path[-1]] = value


def body_mut(fn):
    """probe that mutates a deep copy of the valid payload"""
    def build(valid):
        p = deep(valid)
        fn(p)
        return {"payload": p}
    return build


NA = None
# id, defect, description, medwear builder, radar builder
# a builder returns kwargs for post(): payload / raw_body / headers / auth / url
DEFECTS = [
    ("V00", "valid", "valid request (positive control)",
     lambda v: {"payload": v}, lambda v: {"payload": v}),
    ("V01", "no_auth", "Authorization header missing",
     lambda v: {"payload": v, "auth": False}, lambda v: {"payload": v, "auth": False}),
    ("V02", "invalid_token", "Authorization: Bearer <malformed token>",
     lambda v: {"payload": v, "auth": False, "headers": {"Authorization": "Bearer invalid.token.value"}},
     lambda v: {"payload": v, "auth": False, "headers": {"Authorization": "Bearer invalid.token.value"}}),
    ("V03", "wrong_content_type", "valid body, Content-Type text/plain",
     lambda v: {"payload": v, "headers": {"Content-Type": "text/plain"}},
     lambda v: {"payload": v, "headers": {"Content-Type": "text/plain"}}),
    ("V04", "malformed_json", "body is not JSON",
     lambda v: {"raw_body": "{not json"}, lambda v: {"raw_body": "{not json"}),
    ("V05", "empty_body", "empty body",
     lambda v: {"raw_body": ""}, lambda v: {"raw_body": ""}),
    ("V06", "unknown_schema", "schema identifier does not exist",
     body_mut(lambda p: _set(p, "nonexistent-v0", "header", "schema_id")),
     body_mut(lambda p: _set(p, 999999, "value_schema_id"))),
    ("V07", "missing_timestamp", "timestamp field of the data removed",
     body_mut(lambda p: _del(p, "body", "effective_time_frame", "time_interval", "start_date_time")),
     body_mut(lambda p: _del(p, "records", 0, "value", "time"))),
    ("V08", "missing_sampling_rate", "sampling / acquisition rate removed",
     body_mut(lambda p: _del(p, "header", "acquisition_rate")),
     NA),
    ("V09", "non_numeric_sample", "first sample value is a string",
     body_mut(lambda p: _set(p, "abc", "body", "ecg_lead_data", 0)),
     body_mut(lambda p: _set(p, "abc", "records", 0, "value", "value"))),
    ("V10", "missing_subject", "subject identifier removed",
     body_mut(lambda p: _del(p, "patient_id")),
     body_mut(lambda p: _del(p, "records", 0, "key", "userId"))),
    ("V11", "missing_project", "project identifier removed",
     body_mut(lambda p: _del(p, "project_id")),
     body_mut(lambda p: _del(p, "records", 0, "key", "projectId"))),
    ("V12", "unknown_project", "project / topic that does not exist",
     body_mut(lambda p: _set(p, "medwear_bench_doesnotexist", "project_id")),
     lambda v: {"payload": v, "url": os.environ["RADAR_URL"] + "/radar-gateway/topics/bench_doesnotexist"}),
    ("V13", "empty_samples", "sample array empty",
     body_mut(lambda p: _set(p, [], "body", "ecg_lead_data")),
     body_mut(lambda p: _set(p, [], "records"))),
    ("V14", "time_reversed", "end time before start time",
     body_mut(lambda p: p["body"]["effective_time_frame"]["time_interval"].update(
         end_date_time=p["body"]["effective_time_frame"]["time_interval"]["start_date_time"],
         start_date_time=p["body"]["effective_time_frame"]["time_interval"]["end_date_time"])),
     NA),
    ("V15", "rate_count_mismatch", "declared rate inconsistent with sample count",
     body_mut(lambda p: _set(p, HZ * 2, "header", "acquisition_rate", "value")),
     NA),
]
NA_REASON = {
    ("radar", "missing_sampling_rate"): "no sampling-rate field: RADAR records carry per-sample timestamps",
    ("radar", "time_reversed"): "no batch time interval in RADAR records",
    ("radar", "rate_count_mismatch"): "no declared rate in RADAR records",
}


def stored(snap):
    if snap["errors"]:
        return None
    v = counts.stored_samples(snap)
    return v


def delta(a, b):
    if a is None or b is None:
        return None
    return {k: (b[k] - a[k]) if a.get(k) is not None and b.get(k) is not None else None for k in a}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--settle", type=float, default=2.0)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    platform = require_env("PLATFORM")
    timeout = float(require_env("BATCH_TIMEOUT"))
    adapter = get_adapter(platform, os.path.join(args.out_dir, "token_events.jsonl"))
    session = new_session()
    log = JsonlWriter(os.path.join(args.out_dir, "validation.jsonl"))
    valid = adapter.make_batch(N, "validation", hz=HZ)
    sent = acked = 0

    print(f"{'probe':<5} {'defect':<22} {'status':>6} {'class':<15} {'stored_delta':<40} verdict")
    print("-" * 110)
    for pid, defect, desc, mw, rd in DEFECTS:
        builder = mw if platform == "medwear" else rd
        rec = {"probe": pid, "defect": defect, "description": desc, "platform": platform}
        if builder is None:
            rec.update(result="not_applicable", reason=NA_REASON.get((platform, defect), "no equivalent"))
            log.write(rec)
            print(f"{pid:<5} {defect:<22} {'-':>6} {'-':<15} {'-':<40} not_applicable ({rec['reason']})")
            continue
        # fresh valid payload per probe (new uuid / timestamps) so V00 does not make later probes duplicates
        valid = adapter.make_batch(N, f"validation-{pid}", hz=HZ)
        kwargs = builder(valid)
        before = stored(counts.snapshot(platform))
        res = post(adapter, session, timeout=timeout, **kwargs)
        time.sleep(args.settle)
        after = stored(counts.snapshot(platform))
        d = delta(before, after)
        n_sent = N if "payload" in kwargs else 0
        sent += n_sent
        acked += n_sent if 200 <= res["status"] < 300 else 0
        if pid == "V00":
            verdict = "control_ok" if res["cls"] == "ok" and d and all(v == N for k, v in d.items() if k != "pg_batches") \
                else "CONTROL_FAILED"
        else:
            rejected = 400 <= res["status"] < 500
            accepted = 200 <= res["status"] < 300
            nothing_stored = d is not None and all(v == 0 for v in d.values() if v is not None)
            if rejected:
                verdict = "rejected_not_stored" if nothing_stored else "rejected_but_stored"
            elif accepted:
                verdict = "accepted_not_stored" if nothing_stored else "accepted_and_stored"
            else:   # 5xx, timeouts, connection errors: neither a rejection nor an acceptance
                verdict = f"{res['cls']}_" + ("not_stored" if nothing_stored else "but_stored")
        rec.update(result=verdict, status=res["status"], cls=res["cls"], response=res.get("body"),
                   stored_delta=d, latency_ms=res["latency_ms"])
        log.write(rec)
        print(f"{pid:<5} {defect:<22} {res['status']:>6} {res['cls']:<15} {json.dumps(d):<40} {verdict}")
    with open(os.path.join(args.out_dir, "client_summary.json"), "w") as fh:
        json.dump({"unit": "samples", "test": "validation", "sent_samples": sent, "acked_samples": acked}, fh)
    log.close()


if __name__ == "__main__":
    main()
