#!/bin/bash
#SBATCH --job-name=medwear_bench
#SBATCH --nodes=1
#SBATCH --time=01:00:00
#SBATCH --output=%x_%j.out
# Shared defect list: each probe changes exactly one thing in a valid request.
# Submit with: bench/submit.sh validation conditions/<condition>.env   (CPU/memory from the condition, one bench job at a time)
# The platform stack (PLATFORM=medwear from the condition file) is created
# inside this job and destroyed at the end; see bench/lib/common.sh.
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
source "$BENCH/bench/lib/common.sh"
bench_init validation
stack_fresh
counts_snapshot validation_before
BENCH_STAGE="validation"
"$BENCH_PY" -u "$BENCH_TESTS/validation.py" --out-dir "$RUN_DIR/validation" 2>&1 | tee "$RUN_DIR/validation.log"
counts_snapshot validation_after
delivery_report validation validation_before validation_after "$RUN_DIR/validation/client_summary.json"
bench_finish
