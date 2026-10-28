#!/usr/bin/env python3
"""
Stored-data counts for delivery checks. Counts only, never row contents.

MEDWEAR (benchmark project DB + benchmark bucket of the isolated mwbench stack):
  pg_batches     SELECT count(*)                         FROM measurement_batch WHERE NOT is_deleted
  pg_samples     SELECT coalesce(sum(sample_count), 0)   (same rows)
                 Only rows with ingest_status 'complete' (MEDWEAR versions with
                 two-phase ingest; older schemas have no such column and every
                 row counts). pg_pending_batches: rows still waiting for InfluxDB.
  influx_points  count() of field "value" over the whole benchmark bucket
                 (one point per sample). Not filtered by project_id: when a
                 batch has a missing/unknown project the API stores it in the
                 user's project in PostgreSQL but tags the Influx points with the
                 original value, so a project filter missed them. The bucket is
                 dedicated to the isolated bench stack.

  counts.py --platform medwear --phase u10_before --out $RUN_DIR/counts.jsonl

Exit 1 if any count could not be read; the error is recorded, not hidden.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from podman_util import container_for, inspect, run  # noqa: E402

MW_PROJECT = "mwbench"


def _guard_project(name):
    if not re.fullmatch(r"medwear_bench[a-z0-9_]*", name or ""):
        raise RuntimeError(f"refusing to count outside a medwear_bench* database (got {name!r})")


def _api_env(key):
    info = inspect(container_for(MW_PROJECT, "med-api"))
    for item in info["Config"].get("Env") or []:
        k, _, v = item.partition("=")
        if k == key:
            return v
    raise RuntimeError(f"{key} not set in API container")


def medwear_counts(project):
    _guard_project(project)
    out, errors = {"project": project}, []
    pg = container_for(MW_PROJECT, "postgres")
    # to_jsonb(m)->>'ingest_status' is NULL on schemas without the column
    complete = "coalesce(to_jsonb(m)->>'ingest_status', 'complete') = 'complete'"
    sql = (f"SELECT count(*) FILTER (WHERE {complete}), "
           f"coalesce(sum(sample_count) FILTER (WHERE {complete}), 0), "
           f"count(*) FILTER (WHERE to_jsonb(m)->>'ingest_status' = 'pending') "
           f"FROM measurement_batch m WHERE NOT is_deleted")
    r = run(["podman", "exec", "-e", "BENCH_PROJECT", "-e", f"Q={sql}", pg, "sh", "-c",
             'psql -U "$POSTGRES_USER" -d "$BENCH_PROJECT" -At -F, -v ON_ERROR_STOP=1 -c "$Q"'],
            env_extra={"BENCH_PROJECT": project})
    m = re.fullmatch(r"(\d+),(\d+),(\d+)", r.stdout.strip())
    if r.returncode == 0 and m:
        out["pg_batches"], out["pg_samples"] = int(m.group(1)), int(m.group(2))
        out["pg_pending_batches"] = int(m.group(3))
    else:
        errors.append(f"pg: rc={r.returncode} {r.stderr.strip()[:200]}")
    try:
        bucket = _api_env("INFLUX_BUCKET")
        out["bucket"] = bucket
        if not re.fullmatch(r"medwear_bench[a-z0-9_]*", bucket):
            raise RuntimeError(f"refusing to count outside a medwear_bench* bucket (got {bucket!r})")
        # explicit far-future stop: Flux range() otherwise stops at now() and
        # silently omits points whose timestamps are still in the future
        flux = (f'from(bucket: "{bucket}") |> range(start: 0, stop: 2200-01-01T00:00:00Z) '
                f'|> filter(fn: (r) => r._field == "value") '
                f'|> group() |> count()')
        influx = container_for(MW_PROJECT, "influxdb")
        r = run(["podman", "exec", "-e", f"Q={flux}", influx, "sh", "-c", 'influx query --raw "$Q"'], timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"rc={r.returncode} {r.stderr.strip()[:200]}")
        out["influx_points"] = _parse_flux_count(r.stdout)
    except Exception as exc:
        errors.append(f"influx: {exc}")
    return out, errors


def _parse_flux_count(raw):
    header, total = None, 0
    for row in csv.reader(io.StringIO(raw)):
        if not row or row[0].startswith("#"):
            continue
        if "_value" in row:
            header = row.index("_value")
            continue
        if header is not None and len(row) > header and row[header].strip().isdigit():
            total += int(row[header])
    return total  # no rows -> no points -> 0


def snapshot(platform):
    if platform != "medwear":
        raise SystemExit(f"unknown platform {platform!r}")
    data, errors = medwear_counts(os.environ.get("BENCH_PROJECT", ""))
    return {"ts": time.time(), "platform": platform, "unit": "samples", **data, "errors": errors}


def stored_samples(snap):
    """Primary stored-sample figure(s) of a snapshot, by store."""
    return {"pg_samples": snap.get("pg_samples"), "influx_points": snap.get("influx_points"),
            "pg_batches": snap.get("pg_batches")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", required=True, choices=["medwear"])
    ap.add_argument("--phase", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    snap = snapshot(args.platform)
    snap["phase"] = args.phase
    with open(args.out, "a") as fh:
        fh.write(json.dumps(snap) + "\n")
    shown = {k: v for k, v in stored_samples(snap).items()}
    print(f"[counts] {args.phase}: {shown}" + (f"  ERRORS: {snap['errors']}" if snap["errors"] else ""))
    sys.exit(1 if snap["errors"] else 0)


if __name__ == "__main__":
    main()
