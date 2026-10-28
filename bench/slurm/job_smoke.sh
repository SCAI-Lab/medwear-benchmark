#!/bin/bash
#SBATCH --job-name=medwear_bench
#SBATCH --nodes=1
#SBATCH --time=01:00:00
#SBATCH --output=%x_%j.out
# SMOKE=1 plumbing check: every test briefly, then the acceptance checks.
# Submit with: bench/submit.sh smoke conditions/<condition>.env   (CPU/memory from the condition, one bench job at a time)
# The platform stack (PLATFORM=medwear from the condition file) is created
# inside this job and destroyed at the end; see bench/lib/common.sh.
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
source "$BENCH/bench/lib/common.sh"
export SMOKE=1
bench_init smoke SIZES ITERATIONS LATENCY_RATE_HZ LOAD_MODE USERS SPAWN_RATE STAGE_SECONDS DUR_REPS DUR_WRITE_SECONDS DUR_RATE_HZ
stack_fresh

counts_snapshot latency_before
BENCH_STAGE="smoke latency"
"$BENCH_PY" -u "$BENCH_TESTS/latency.py" --out-dir "$RUN_DIR/latency" 2>&1 | tee "$RUN_DIR/latency.log"
sleep 5
counts_snapshot latency_after
delivery_report latency latency_before latency_after "$RUN_DIR/latency/client_summary.json"

sdir="$RUN_DIR/u${USERS}"
mkdir -p "$sdir"
counts_snapshot load_before
profile_start "$sdir/profile.csv"
BENCH_STAGE="smoke load"
if [ "$LOAD_MODE" = open ]; then
    require_vars DEVICE_RATE_HZ MAX_INFLIGHT
    OPEN_RATE=$(awk -v u="$USERS" -v r="$DEVICE_RATE_HZ" 'BEGIN {print u * r}'); export OPEN_RATE
fi
STAGE_DIR="$sdir" "$BENCH_PY" "$BENCH_LIB/client_usage.py" --out "$sdir/client_usage.json" -- \
        $LOCUST_CMD -f "$BENCH_TESTS/locustfile_bench.py" --headless \
    -u "$USERS" -r "$SPAWN_RATE" -t "${STAGE_SECONDS}s" --stop-timeout "$LOCUST_TIMEOUT" \
    --host "$MEDWEAR_URL" --csv "$sdir/locust" 2>&1 | tee "$sdir/locust.log" || true
profile_stop
sleep 5
counts_snapshot load_after
delivery_report load load_before load_after "$sdir/client_summary.json"

BENCH_STAGE="smoke validation"
"$BENCH_PY" -u "$BENCH_TESTS/validation.py" --out-dir "$RUN_DIR/validation" 2>&1 | tee "$RUN_DIR/validation.log"
BENCH_STAGE="smoke idempotency"
"$BENCH_PY" -u "$BENCH_TESTS/idempotency.py" --out-dir "$RUN_DIR/idempotency" 2>&1 | tee "$RUN_DIR/idempotency.log"
grep -E '^(stored delta|result):' "$RUN_DIR/idempotency.log" >> "$RUN_DIR/summary.txt" || true

counts_snapshot durability_before
BENCH_STAGE="smoke durability"
set +e
"$BENCH_PY" -u "$BENCH_TESTS/durability.py" --out-dir "$RUN_DIR/durability" 2>&1 | tee "$RUN_DIR/durability.log"
dur_rc=${PIPESTATUS[0]}
set -e
counts_snapshot durability_after
delivery_report durability durability_before durability_after "$RUN_DIR/durability/client_summary.json"
"$BENCH_PY" - "$RUN_DIR/durability/cases.jsonl" >> "$RUN_DIR/summary.txt" <<'PY'
import json, sys
for c in map(json.loads, open(sys.argv[1])):
    r = c.get("restore") or {}
    print(f"durability {c['target']}/{c['fault']}/rep{c['rep']}: self_recovered={c.get('self_recovered')} "
          f"recovery_s={c.get('recovery_s')} restored={r.get('recovered')} acked={c.get('acked')} "
          f"stored={c.get('stored_delta')} acked_minus_stored={c.get('acked_minus_stored')}")
PY
[ "$dur_rc" -eq 0 ] || fail "durability runner exited $dur_rc (see durability.log)"

throttle_snapshot end
BENCH_STAGE="acceptance"
"$BENCH_PY" "$BENCH_LIB/acceptance.py" "$RUN_DIR" | tee -a "$RUN_DIR/summary.txt"
bench_finish
