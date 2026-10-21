#!/bin/bash
#SBATCH --job-name=medwear_radar_bench
#SBATCH --nodes=1
#SBATCH --time=12:00:00
#SBATCH --output=%x_%j.out
# Fault injection (restart / SIGKILL / freeze) of each store, ingress and auth service during writes.
# Submit with: bench/submit.sh durability conditions/<condition>.env   (CPU/memory from the condition, one bench job at a time)
# The platform stack (PLATFORM=medwear|radar from the condition file) is created
# inside this job and destroyed at the end; see bench/lib/common.sh.
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
source "$BENCH/bench/lib/common.sh"
bench_init durability DUR_REPS DUR_WRITE_SECONDS DUR_RATE_HZ
stack_fresh
counts_snapshot durability_before
profile_start "$RUN_DIR/profile.csv"
BENCH_STAGE="durability"
set +e
"$BENCH_PY" -u "$BENCH_TESTS/durability.py" --out-dir "$RUN_DIR/durability" 2>&1 | tee "$RUN_DIR/durability.log"
rc=${PIPESTATUS[0]}
set -e
profile_stop
counts_snapshot durability_after
delivery_report durability_total durability_before durability_after "$RUN_DIR/durability/client_summary.json"
[ "$rc" -eq 0 ] || fail "durability runner exited $rc (see durability.log)"
bench_finish
