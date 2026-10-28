#!/bin/bash
#SBATCH --job-name=medwear_bench
#SBATCH --nodes=1
#SBATCH --time=01:00:00
#SBATCH --output=%x_%j.out
# Idle resource usage of a freshly started stack.
# Submit with: bench/submit.sh baseline conditions/<condition>.env   (CPU/memory from the condition, one bench job at a time)
# The platform stack (PLATFORM=medwear from the condition file) is created
# inside this job and destroyed at the end; see bench/lib/common.sh.
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
source "$BENCH/bench/lib/common.sh"
bench_init baseline DURATION
stack_fresh
BENCH_STAGE="baseline"
"$BENCH_PY" "$BENCH_LIB/profile_containers.py" --project "$(own_project)" --duration "$DURATION" \
    --output "$RUN_DIR/profile.csv" 2>&1 | tee "$RUN_DIR/baseline.log"
bench_finish
