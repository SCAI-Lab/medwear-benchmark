#!/usr/bin/env python3
"""
Register the benchmark key/value Avro schemas for a topic and print their IDs.

Schema IDs are taken from the registry of the stack that runs the test, not
hard-coded, as they differ between stacks (a wrong ID makes every request fail
with 422 schema_not_found).

  radar_schemas.py --registry http://127.0.0.1:8081 --topic bench_samples
  -> "<key_id> <value_id>"
"""
import argparse
import json
import sys
import urllib.request

KEY_SCHEMA = {
    "type": "record", "name": "ObservationKey", "namespace": "org.radarcns.kafka",
    "fields": [
        {"name": "projectId", "type": ["null", "string"], "default": None},
        {"name": "userId", "type": "string"},
        {"name": "sourceId", "type": "string"},
    ],
}
VALUE_SCHEMA = {
    "type": "record", "name": "BenchSample", "namespace": "org.radarbase.bench",
    "fields": [
        {"name": "time", "type": "double"},
        {"name": "value", "type": "double"},
    ],
}


def register(registry, subject, schema):
    body = json.dumps({"schema": json.dumps(schema)}).encode()
    req = urllib.request.Request(f"{registry}/subjects/{subject}/versions", data=body,
                                 headers={"Content-Type": "application/vnd.schemaregistry.v1+json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        return int(json.loads(resp.read())["id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--topic", required=True)
    args = ap.parse_args()
    try:
        k = register(args.registry, f"{args.topic}-key", KEY_SCHEMA)
        v = register(args.registry, f"{args.topic}-value", VALUE_SCHEMA)
    except Exception as exc:
        print(f"schema registration failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"{k} {v}")


if __name__ == "__main__":
    main()
