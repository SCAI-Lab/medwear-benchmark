#!/usr/bin/env python3
"""
Run the load generator and record its own resource use, so a saturated client
(it shares the node and the job allocation with the stack) is visible.

  client_usage.py --out $STAGE_DIR/client_usage.json -- <command...>

Writes user/sys CPU seconds, wall seconds, average CPU % and max RSS of the
command and its children. Locust runs as one process (one core), so an average
above CLIENT_SATURATION_PCT (default 90) is flagged as client_bound.
Exit code: that of the command.
"""
import json
import os
import resource
import subprocess
import sys
import time


def main():
    if "--" not in sys.argv or sys.argv[1] != "--out":
        raise SystemExit("usage: client_usage.py --out FILE -- command ...")
    out = sys.argv[2]
    cmd = sys.argv[sys.argv.index("--") + 1:]
    t0 = time.monotonic()
    rc = subprocess.call(cmd)
    wall = time.monotonic() - t0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = ru.ru_utime + ru.ru_stime
    pct = 100.0 * cpu / wall if wall > 0 else 0.0
    limit = float(os.environ.get("CLIENT_SATURATION_PCT") or 90)
    rec = {"command": cmd[:3], "exit_code": rc, "wall_s": round(wall, 1), "user_s": round(ru.ru_utime, 1),
           "sys_s": round(ru.ru_stime, 1), "cpu_pct_avg": round(pct, 1), "max_rss_mb": round(ru.ru_maxrss / 1024, 1),
           "client_bound": pct > limit, "saturation_threshold_pct": limit}
    with open(out, "w") as fh:
        json.dump(rec, fh, indent=2)
    print(f"[client] cpu_avg={rec['cpu_pct_avg']}% max_rss={rec['max_rss_mb']}MB wall={rec['wall_s']}s"
          + ("  CLIENT-BOUND" if rec["client_bound"] else ""))
    sys.exit(rc)


if __name__ == "__main__":
    main()
