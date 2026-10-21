#!/bin/bash
#SBATCH --job-name=medwear_radar_bench
#SBATCH --nodes=1
#SBATCH --time=06:00:00
#SBATCH --output=%x_%j.out
# Per-batch ingest latency vs batch size.
# Submit with: bench/submit.sh latency conditions/<condition>.env   (CPU/memory from the condition, one bench job at a time)
# The platform stack (PLATFORM=medwear|radar from the condition file) is created
# inside this job and destroyed at the end; see bench/lib/common.sh.
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
source "$BENCH/bench/lib/common.sh"
bench_init latency SIZES ITERATIONS LATENCY_RATE_HZ
stack_fresh
counts_snapshot latency_before
profile_start "$RUN_DIR/profile.csv"
BENCH_STAGE="latency"
"$BENCH_PY" -u "$BENCH_TESTS/latency.py" --out-dir "$RUN_DIR/latency" 2>&1 | tee "$RUN_DIR/latency.log"
profile_stop
sleep "${SETTLE_SECONDS:-10}"
counts_snapshot latency_after
delivery_report latency latency_before latency_after "$RUN_DIR/latency/client_summary.json"
bench_finish
