#!/bin/bash
#SBATCH --job-name=medwear_radar_bench
#SBATCH --nodes=1
#SBATCH --time=01:00:00
#SBATCH --output=%x_%j.out
# Identical request sent 5x; stored-sample delta recorded.
# Submit with: bench/submit.sh idempotency conditions/<condition>.env   (CPU/memory from the condition, one bench job at a time)
# The platform stack (PLATFORM=medwear|radar from the condition file) is created
# inside this job and destroyed at the end; see bench/lib/common.sh.
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
source "$BENCH/bench/lib/common.sh"
bench_init idempotency
stack_fresh
BENCH_STAGE="idempotency"
"$BENCH_PY" -u "$BENCH_TESTS/idempotency.py" --out-dir "$RUN_DIR/idempotency" --repeats "${REPEATS:-5}" \
    2>&1 | tee "$RUN_DIR/idempotency.log"
grep -E '^(stored delta|result):' "$RUN_DIR/idempotency.log" >> "$RUN_DIR/summary.txt" || true
bench_finish
