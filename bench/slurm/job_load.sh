#!/bin/bash
#SBATCH --job-name=medwear_radar_bench
#SBATCH --nodes=1
#SBATCH --time=06:00:00
#SBATCH --output=%x_%j.out
# Locust load stages (USERS), closed or open loop.
# Submit with: bench/submit.sh load conditions/<condition>.env   (CPU/memory from the condition, one bench job at a time)
# The platform stack (PLATFORM=medwear|radar from the condition file) is created
# inside this job and destroyed at the end; see bench/lib/common.sh.
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
source "$BENCH/bench/lib/common.sh"
bench_init load LOAD_MODE USERS SPAWN_RATE STAGE_SECONDS STAGE_ISOLATION
stack_fresh
i=0
for u in $USERS; do
    stage="u${u}"
    sdir="$RUN_DIR/$stage"
    mkdir -p "$sdir"
    [ "$i" -gt 0 ] && stage_isolate "$stage"
    counts_snapshot "${stage}_before"
    profile_start "$sdir/profile.csv"
    BENCH_STAGE="load $stage"
    if [ "$LOAD_MODE" = open ]; then
        # open loop: u devices, each sending DEVICE_RATE_HZ batches/s regardless of responses
        require_vars DEVICE_RATE_HZ MAX_INFLIGHT
        OPEN_RATE=$(awk -v u="$u" -v r="$DEVICE_RATE_HZ" 'BEGIN {print u * r}'); export OPEN_RATE
    fi
    log "stage $stage: $u devices/users, ${STAGE_SECONDS}s, mode=$LOAD_MODE${OPEN_RATE:+ rate=${OPEN_RATE}/s}, stop-timeout=${LOCUST_TIMEOUT}s"
    # stats are not reset at spawn; requests.jsonl has timestamps to cut ramp-up.
    # --stop-timeout lets requests in flight at stage end finish (or time out).
    STAGE_DIR="$sdir" "$BENCH_PY" "$BENCH_LIB/client_usage.py" --out "$sdir/client_usage.json" -- \
        $LOCUST_CMD -f "$BENCH_TESTS/locustfile_bench.py" --headless \
        -u "$u" -r "$SPAWN_RATE" -t "${STAGE_SECONDS}s" --stop-timeout "$LOCUST_TIMEOUT" \
        --host "${MEDWEAR_URL:-${RADAR_URL:-}}" --csv "$sdir/locust" \
        2>&1 | tee "$sdir/locust.log" || log "WARN: locust exited non-zero for $stage"
    profile_stop
    sleep "${SETTLE_SECONDS:-10}"
    counts_snapshot "${stage}_after"
    if [ -f "$sdir/client_summary.json" ]; then
        delivery_report "$stage" "${stage}_before" "${stage}_after" "$sdir/client_summary.json"
    else
        fail "stage $stage produced no client_summary.json (locust failed?)"
    fi
    i=$((i + 1))
done
bench_finish
