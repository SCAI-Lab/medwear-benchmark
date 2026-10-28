"""
Platform adapters shared by every benchmark client (latency, validation,
idempotency, durability, locust). Both platforms get:
  * the same synthetic signal and sample counts per request
  * the same client timeout handling (BATCH_TIMEOUT / LOCUST_TIMEOUT passed to
    the HTTP client; a response slower than the timeout counts as a timeout)
  * the same failure classes, with 401/403 and server-side timeouts separate
  * a token manager that refreshes before expiry and on 401, and logs every
    issue/refresh time (never the token)

Required environment (no defaults):
  medwear: MEDWEAR_URL BENCH_PROJECT KC_TOKEN_URL KC_CLIENT BENCH_KC_USER BENCH_KC_PW
           (BENCH_KC_CLIENT_SECRET if the client is confidential)
  radar:   RADAR_URL RADAR_TOPIC RADAR_KEY_SCHEMA_ID RADAR_VALUE_SCHEMA_ID
           MP_TOKEN_URL MP_CLIENT_ID MP_CLIENT_SECRET
"""
import copy
import json
import math
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

MAX_BATCH_SAMPLES = 50_000   # 100k batches dropped (one RADAR run took ~4 h)

FAILURE_CLASSES = ("ok", "client_timeout", "conn_error", "auth_401", "auth_403",
                   "server_timeout", "server_5xx", "client_4xx", "other")


def require_env(name):
    v = os.environ.get(name)
    if v is None or v == "":
        raise SystemExit(f"required environment variable {name} is unset")
    return v


def classify(status, exc=None, latency_ms=None, timeout_s=None):
    if exc is not None:
        return "client_timeout" if "Timeout" in type(exc).__name__ else "conn_error"
    if 200 <= status < 300:
        if latency_ms is not None and timeout_s and latency_ms > timeout_s * 1000:
            return "client_timeout"
        return "ok"
    if status == 401:
        return "auth_401"
    if status == 403:
        return "auth_403"
    if status in (408, 504):
        return "server_timeout"
    if status >= 500:
        return "server_5xx"
    if status >= 400:
        return "client_4xx"
    return "other"


def new_session():
    s = requests.Session()
    s.trust_env = False   # never route local traffic through a site HTTP proxy
    # MEDWEAR is measured as deployed (HTTPS via nginx): verify its bench
    # certificate instead of disabling verification
    if os.environ.get("BENCH_CA_FILE"):
        s.verify = os.environ["BENCH_CA_FILE"]
    return s


class JsonlWriter:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._fh = open(path, "a", buffering=1) if path else None

    def write(self, rec):
        if self._fh:
            with self._lock:
                self._fh.write(json.dumps(rec) + "\n")

    def close(self):
        if self._fh:
            self._fh.close()


class TokenManager:
    """Shared bearer token; refresh at 70 % of lifetime or on 401 (single-flight)."""

    def __init__(self, fetch, log_path=None):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._log = JsonlWriter(log_path)
        self.token = None
        self.issued_at = 0.0
        self.expires_at = 0.0
        self._last_attempt = 0.0
        self.refreshes = 0
        self.failures = 0

    def get(self):
        if self.token is None or time.time() > self.issued_at + 0.7 * (self.expires_at - self.issued_at):
            self.refresh("initial" if self.token is None else "scheduled")
        return self.token

    def refresh(self, reason, min_interval=5.0):
        with self._lock:
            now = time.time()
            if reason == "on_401" and now - self._last_attempt < min_interval:
                return
            if reason == "scheduled" and now <= self.issued_at + 0.7 * (self.expires_at - self.issued_at):
                return   # another greenlet refreshed meanwhile
            self._last_attempt = now
            try:
                tok, expires_in = self._fetch()
                self.token, self.issued_at, self.expires_at = tok, now, now + expires_in
                self.refreshes += 1
                self._log.write({"ts": now, "event": "issued", "reason": reason, "expires_in_s": expires_in})
            except Exception as exc:
                self.failures += 1
                self._log.write({"ts": now, "event": "fetch_failed", "reason": reason,
                                 "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                if self.token is None:
                    raise


def _signal(n, hz, amp=500.0):
    return [round(math.sin(2 * math.pi * 1.2 * i / hz) * amp + random.gauss(0, 5), 4) for i in range(n)]


class Medwear:
    name = "medwear"
    content_type = "application/json"
    path = "/v1/data/batch"

    def __init__(self, token_log=None, base_url=None):
        self.base = base_url or require_env("MEDWEAR_URL")
        self.project = require_env("BENCH_PROJECT")
        if not re.fullmatch(r"medwear_bench[a-z0-9_]*", self.project):
            raise SystemExit(f"BENCH_PROJECT must be a medwear_bench* project (got {self.project!r})")
        self.url = self.base + self.path
        self.health_url = self.base + "/v1/health"
        self.tokens = TokenManager(self._fetch_token, token_log)
        # MEDWEAR_SESSIONS=1 (MEDWEAR with POST /v1/data/session): stream
        # metadata is registered once per simulated device and signal; batches
        # then carry only session_id, their uuid and the body. Registration
        # happens before the measured request and is logged to sessions.jsonl.
        self.use_sessions = os.environ.get("MEDWEAR_SESSIONS") == "1"
        self._sessions = {}
        self._sessions_lock = threading.Lock()
        self._session_log = JsonlWriter(
            os.path.join(os.path.dirname(token_log), "sessions.jsonl") if token_log else None)

    def wire(self, payload):
        """Request body for a make_batch() payload."""
        if not self.use_sessions or not isinstance(payload, dict) or "header" not in payload:
            return payload
        h = payload["header"]
        key = (payload["patient_id"], payload["device_id"], payload["signal_type"], h["schema_id"],
               h["acquisition_rate"]["value"], payload["body"]["unit"])
        with self._sessions_lock:
            sid = self._sessions.get(key)
            if sid is None:
                sid = self._register_session(key, h)
                if sid is None:
                    return payload   # registration failed: full batch, nothing lost
                self._sessions[key] = sid
        return {"session_id": sid, "project_id": self.project, "uuid": h["uuid"], "body": payload["body"]}

    def _register_session(self, key, header):
        reg = {"session_id": str(uuid.uuid4()), "patient_id": key[0], "device_id": key[1],
               "signal_type": key[2], "unit": key[5], "project_id": self.project,
               "header": {"schema_id": key[3], "acquisition_rate": header["acquisition_rate"]}}
        s = new_session()
        for attempt in range(1, 6):
            t0 = time.perf_counter()
            status, err = 0, None
            try:
                r = s.post(self.base + "/v1/data/session", json=reg, timeout=30,
                           headers={"Authorization": "Bearer " + self.tokens.get()})
                status = r.status_code
            except requests.RequestException as exc:
                err = type(exc).__name__
            self._session_log.write({"ts": time.time(), "session_id": reg["session_id"], "device_id": key[1],
                                     "signal_type": key[2], "attempt": attempt, "status": status, "error": err,
                                     "latency_ms": round((time.perf_counter() - t0) * 1000, 2)})
            if status == 200:
                return reg["session_id"]
            if status == 401:
                self.tokens.refresh("on_401")
            time.sleep(min(2 ** attempt, 10))
        return None

    def _fetch_token(self):
        data = {"grant_type": "password", "client_id": require_env("KC_CLIENT"),
                "username": require_env("BENCH_KC_USER"), "password": require_env("BENCH_KC_PW")}
        if os.environ.get("BENCH_KC_CLIENT_SECRET"):
            data["client_secret"] = os.environ["BENCH_KC_CLIENT_SECRET"]
        r = new_session().post(require_env("KC_TOKEN_URL"), data=data, timeout=30)
        r.raise_for_status()
        j = r.json()
        return j["access_token"], int(j.get("expires_in", 60))

    def make_batch(self, n, tag, kind="ECG", hz=256.0, batch_uuid=None):
        if n > MAX_BATCH_SAMPLES:
            raise ValueError(f"{n} samples > MAX_BATCH_SAMPLES={MAX_BATCH_SAMPLES}")
        # the batch covers the n/hz seconds that END now (data already recorded,
        # as a wearable uploads it); no timestamps in the future
        end = datetime.now(timezone.utc)
        start = end - timedelta(seconds=n / hz)
        header = {"uuid": batch_uuid or str(uuid.uuid4()), "schema_id": "ecg-v1" if kind == "ECG" else "ppg-v1",
                  "source_creation_date_time": end.isoformat(), "acquisition_rate": {"value": hz, "unit": "Hz"}}
        tf = {"time_interval": {"start_date_time": start.isoformat(), "end_date_time": end.isoformat()}}
        if kind == "ECG":
            body = {"ecg_lead_data": _signal(n, hz), "signal_quality": [0.95] * n, "unit": "uV",
                    "effective_time_frame": tf}
        else:
            body = {"signal_data": _signal(n, hz, 20.0), "unit": "ADC", "effective_time_frame": tf}
        return {"header": header, "body": body, "signal_type": kind, "patient_id": tag,
                "project_id": self.project, "device_id": f"bench-{tag}"}

    @staticmethod
    def samples_in(payload):
        b = payload.get("body", {})
        return len(b.get("ecg_lead_data") or b.get("signal_data") or [])

    def health_ok(self, session=None):
        try:
            r = (session or new_session()).get(self.health_url, timeout=10)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False


class Radar:
    name = "radar"
    content_type = "application/vnd.kafka.avro.v2+json"

    def __init__(self, token_log=None, base_url=None):
        self.base = base_url or require_env("RADAR_URL")
        self.topic = require_env("RADAR_TOPIC")
        self.key_id = int(require_env("RADAR_KEY_SCHEMA_ID"))
        self.value_id = int(require_env("RADAR_VALUE_SCHEMA_ID"))
        self.path = f"/radar-gateway/topics/{self.topic}"
        self.url = self.base + self.path
        self.health_url = self.base + "/radar-gateway/health"
        self.tokens = TokenManager(self._fetch_token, token_log)

    def _fetch_token(self):
        r = new_session().post(require_env("MP_TOKEN_URL"), data={"grant_type": "client_credentials"},
                               auth=(require_env("MP_CLIENT_ID"), require_env("MP_CLIENT_SECRET")), timeout=30)
        r.raise_for_status()
        j = r.json()
        return j["access_token"], int(j.get("expires_in", 60))

    def make_batch(self, n, tag, kind="ECG", hz=256.0, batch_uuid=None):
        if n > MAX_BATCH_SAMPLES:
            raise ValueError(f"{n} samples > MAX_BATCH_SAMPLES={MAX_BATCH_SAMPLES}")
        t0 = time.time() - n / hz      # same window as MEDWEAR: ends now
        sig = _signal(n, hz, 0.5 if kind == "ECG" else 20.0)
        key = {"projectId": {"string": "bench"}, "userId": tag, "sourceId": f"bench-{tag}"}
        return {"key_schema_id": self.key_id, "value_schema_id": self.value_id,
                "records": [{"key": dict(key), "value": {"time": t0 + i / hz, "value": sig[i]}} for i in range(n)]}

    @staticmethod
    def samples_in(payload):
        return len(payload.get("records") or [])

    def health_ok(self, session=None):
        try:
            return (session or new_session()).get(self.health_url, timeout=10).status_code == 200
        except Exception:
            return False


def get_adapter(platform, token_log=None, base_url=None):
    if platform == "medwear":
        return Medwear(token_log, base_url)
    if platform == "radar":
        return Radar(token_log, base_url)
    raise SystemExit(f"unknown PLATFORM {platform!r}")


def latency_hops(platform):
    """(name, base_url) per measured hop. MEDWEAR as deployed (nginx + TLS) and
    the API directly, so the cost of the proxy/TLS step can be reported;
    RADAR as deployed has one hop (the gateway, plain HTTP)."""
    if platform == "medwear":
        return [("nginx_tls", require_env("MEDWEAR_URL")), ("api_direct", require_env("MEDWEAR_API_DIRECT_URL"))]
    return [("gateway", require_env("RADAR_URL"))]


def post(adapter, session, payload=None, *, timeout, raw_body=None, headers=None, auth=True, url=None):
    """One ingest request. Returns a result dict; never raises for HTTP/network errors."""
    h = {"Content-Type": adapter.content_type}
    if auth:
        h["Authorization"] = "Bearer " + adapter.tokens.get()
    for k, v in (headers or {}).items():
        if v is None:
            h.pop(k, None)
        else:
            h[k] = v
    if raw_body is None and hasattr(adapter, "wire"):
        body = json.dumps(adapter.wire(payload))   # session registration, if any, happens here, before t0
    else:
        body = raw_body if raw_body is not None else json.dumps(payload)
    samples = adapter.samples_in(payload) if isinstance(payload, dict) else 0
    t_wall = time.time()
    t0 = time.perf_counter()
    try:
        r = session.post(url or adapter.url, data=body, headers=h, timeout=(min(10.0, timeout), timeout))
    except requests.RequestException as exc:
        lat = (time.perf_counter() - t0) * 1000
        return {"ts": t_wall, "latency_ms": round(lat, 2), "status": 0, "cls": classify(0, exc),
                "error": type(exc).__name__, "samples": samples}
    lat = (time.perf_counter() - t0) * 1000
    cls = classify(r.status_code, latency_ms=lat, timeout_s=timeout)
    res = {"ts": t_wall, "latency_ms": round(lat, 2), "status": r.status_code, "cls": cls, "samples": samples,
           "server_date": r.headers.get("Date"),
           "request_id": r.headers.get("X-Request-ID") or r.headers.get("X-Trace-ID") or r.headers.get("traceparent")}
    if cls != "ok":
        res["body"] = r.text[:200]
    if cls == "auth_401" and auth:
        adapter.tokens.refresh("on_401")
    return res


class Tally:
    """sent / acked samples and failure classes, written as client_summary.json."""

    def __init__(self):
        self.by_class = {c: 0 for c in FAILURE_CLASSES}
        self.requests = self.sent_samples = self.acked_samples = 0
        self.warmup_requests = 0

    def add(self, res, warmup=False):
        self.requests += 1
        self.warmup_requests += 1 if warmup else 0
        self.sent_samples += res["samples"]
        self.by_class[res["cls"]] = self.by_class.get(res["cls"], 0) + 1
        if 200 <= res["status"] < 300:   # acked by HTTP, even if slower than the timeout
            self.acked_samples += res["samples"]

    def dump(self, path, extra=None, tokens=None):
        out = {"unit": "samples", "requests": self.requests, "warmup_requests": self.warmup_requests,
               "sent_samples": self.sent_samples, "acked_samples": self.acked_samples, "by_class": self.by_class}
        if tokens is not None:
            out["token_refreshes"] = tokens.refreshes
            out["token_fetch_failures"] = tokens.failures
        out.update(extra or {})
        with open(path, "w") as fh:
            json.dump(out, fh, indent=2)
        return out


def deep(payload):
    return copy.deepcopy(payload)
