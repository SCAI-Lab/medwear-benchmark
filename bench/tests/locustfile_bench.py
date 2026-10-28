"""
Locust write workload, identical for both platforms (PLATFORM=medwear|radar).

Mix: 3:1 ECG-like (256 samples @ 256 Hz) : PPG-like (100 samples @ 100 Hz).

LOAD_MODE=closed  each user sends, waits for the response, then waits
                  between(1,3) s, or constant_throughput(TARGET_RPS) if set.
LOAD_MODE=open    constant arrival rate OPEN_RATE requests/s in total,
                  independent of response times (requests are fired in their
                  own greenlets; above MAX_INFLIGHT arrivals are counted as
                  client_dropped, not sent).

Measurement notes:
  * LOCUST_TIMEOUT is passed to the HTTP client; slower responses are
    client_timeout, not successes
  * failure classes: client_timeout, conn_error, auth_401, auth_403,
    server_timeout (504/408), server_5xx, client_4xx
  * requests in flight when the stage time ends are logged
    (inflight_at_stage_end); run with --stop-timeout >= LOCUST_TIMEOUT so they
    complete and are counted (completed_after_stage_end)
  * token issue/refresh times logged (token_events.jsonl); 401 triggers refresh
  * every request is logged to requests.jsonl (time, latency, status, class,
    samples, server Date, request id)

Required env: PLATFORM WRITES_ONLY(=1) LOCUST_TIMEOUT LOAD_MODE STAGE_SECONDS STAGE_DIR
plus the platform variables documented in platforms.py.
"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gevent  # noqa: E402
from locust import HttpUser, between, constant, constant_throughput, events, task  # noqa: E402

from platforms import JsonlWriter, Tally, classify, get_adapter, require_env  # noqa: E402

if require_env("WRITES_ONLY") != "1":
    raise SystemExit("only WRITES_ONLY=1 is implemented identically for both platforms")
STAGE_DIR = require_env("STAGE_DIR")
os.makedirs(STAGE_DIR, exist_ok=True)
ADAPTER = get_adapter(require_env("PLATFORM"), os.path.join(STAGE_DIR, "token_events.jsonl"))
TIMEOUT = float(require_env("LOCUST_TIMEOUT"))
MODE = require_env("LOAD_MODE")
STAGE_SECONDS = float(require_env("STAGE_SECONDS"))
TARGET_RPS = float(os.environ.get("TARGET_RPS") or 0)
OPEN_RATE = float(os.environ.get("OPEN_RATE") or 0)
MAX_INFLIGHT = int(require_env("MAX_INFLIGHT")) if MODE == "open" else 0
if MODE not in ("closed", "open"):
    raise SystemExit("LOAD_MODE must be closed or open")
if MODE == "open" and OPEN_RATE <= 0:
    raise SystemExit("LOAD_MODE=open needs OPEN_RATE > 0")

KINDS = [("ECG", 256, 256.0)] * 3 + [("PPG", 100, 100.0)]
REQ_LOG = JsonlWriter(os.path.join(STAGE_DIR, "requests.jsonl"))
TALLY = Tally()
STATE = {"inflight": 0, "stage_ended": False, "inflight_at_stage_end": None, "completed_after_stage_end": 0,
         "client_dropped": 0, "t_start": None, "t_spawned": None, "t_stage_end": None}
OPEN_GREENLETS = set()


def send(user):
    kind, n, hz = random.choice(KINDS)
    payload = ADAPTER.make_batch(n, f"load-{os.getpid()}-{id(user) % 100000}", kind=kind, hz=hz)
    if hasattr(ADAPTER, "wire"):
        payload = ADAPTER.wire(payload)   # MEDWEAR sessions: registered once, before the measured request
    headers = {"Content-Type": ADAPTER.content_type, "Authorization": "Bearer " + ADAPTER.tokens.get()}
    STATE["inflight"] += 1
    t_wall = time.time()
    t0 = time.perf_counter()
    try:
        with user.client.post(ADAPTER.path, data=json.dumps(payload), headers=headers, timeout=TIMEOUT,
                              catch_response=True, name=f"POST ingest [{kind} {n}]") as resp:
            lat = (time.perf_counter() - t0) * 1000
            err = getattr(resp, "error", None) if not resp.status_code else None
            cls = classify(resp.status_code or 0, err, latency_ms=lat, timeout_s=TIMEOUT)
            if cls == "ok":
                resp.success()
            else:
                resp.failure(cls)
            if cls == "auth_401":
                ADAPTER.tokens.refresh("on_401")
            res = {"ts": t_wall, "latency_ms": round(lat, 2), "status": resp.status_code or 0, "cls": cls,
                   "samples": n, "kind": kind, "server_date": resp.headers.get("Date") if resp.headers else None,
                   "request_id": (resp.headers.get("X-Request-ID") or resp.headers.get("traceparent"))
                   if resp.headers else None,
                   "after_stage_end": STATE["stage_ended"]}
            if err is not None:
                res["error"] = type(err).__name__
            TALLY.add(res)
            REQ_LOG.write(res)
    finally:
        STATE["inflight"] -= 1
        if STATE["stage_ended"]:
            STATE["completed_after_stage_end"] += 1


if MODE == "closed":
    class BenchUser(HttpUser):
        wait_time = constant_throughput(TARGET_RPS) if TARGET_RPS > 0 else between(1, 3)

        def on_start(self):
            self.client.verify = os.environ.get("BENCH_CA_FILE") or True

        @task
        def ingest(self):
            send(self)
else:
    class OpenLoopUser(HttpUser):
        wait_time = constant(0)

        def on_start(self):
            self.client.verify = os.environ.get("BENCH_CA_FILE") or True

        @task
        def arrivals(self):
            users = max(1, self.environment.runner.user_count)
            interval = users / OPEN_RATE
            if STATE["inflight"] >= MAX_INFLIGHT:
                STATE["client_dropped"] += 1
            else:
                g = gevent.spawn(send, self)
                OPEN_GREENLETS.add(g)
                g.link(lambda gl: OPEN_GREENLETS.discard(gl))
            gevent.sleep(interval)


def _mark_stage_end():
    STATE["stage_ended"] = True
    STATE["t_stage_end"] = time.time()
    STATE["inflight_at_stage_end"] = STATE["inflight"]
    print(f"[bench] stage time reached: {STATE['inflight']} request(s) in flight")


@events.test_start.add_listener
def _on_start(environment, **_):
    ADAPTER.tokens.get()   # fail fast if auth is broken
    STATE["t_start"] = time.time()
    gevent.spawn_later(STAGE_SECONDS, _mark_stage_end)


@events.spawning_complete.add_listener
def _on_spawned(user_count, **_):
    STATE["t_spawned"] = time.time()


@events.test_stop.add_listener
def _on_stop(environment, **_):
    if not STATE["stage_ended"]:
        _mark_stage_end()
    if OPEN_GREENLETS:
        gevent.joinall(list(OPEN_GREENLETS), timeout=TIMEOUT + 5)
    s = TALLY.dump(os.path.join(STAGE_DIR, "client_summary.json"), tokens=ADAPTER.tokens, extra={
        "test": "load", "platform": ADAPTER.name, "mode": MODE, "timeout_s": TIMEOUT,
        "target_rps": TARGET_RPS, "open_rate": OPEN_RATE,
        "t_start": STATE["t_start"], "t_spawn_complete": STATE["t_spawned"], "t_stage_end": STATE["t_stage_end"],
        "t_stop": time.time(), "inflight_at_stage_end": STATE["inflight_at_stage_end"],
        "completed_after_stage_end": STATE["completed_after_stage_end"],
        "still_inflight_at_exit": STATE["inflight"], "client_dropped": STATE["client_dropped"]})
    print(f"[bench] requests={s['requests']} sent={s['sent_samples']} acked={s['acked_samples']} samples "
          f"inflight_at_stage_end={s['inflight_at_stage_end']} completed_after={s['completed_after_stage_end']} "
          f"still_inflight={s['still_inflight_at_exit']} classes={ {k: v for k, v in s['by_class'].items() if v} }")
    REQ_LOG.close()
