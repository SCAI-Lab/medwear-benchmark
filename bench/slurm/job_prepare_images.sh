#!/bin/bash
#SBATCH --job-name=medwear_radar_bench
#SBATCH --nodes=1
#SBATCH --time=03:00:00
#SBATCH --output=%x_%j.out
# One-time pull/build of all bench images into the submitting user's podman,
# inside a Slurm allocation (no compute outside Slurm), through BENCH_PROXY if set;
# CPU/memory from the condition file passed to submit.sh;
# benchmark jobs themselves never pull.
#   bench/submit.sh prepare_images conditions/smoke_medwear_sessions.env
set -euo pipefail
BENCH="${BENCH:-$HOME/benchmark}"
[ -f "$BENCH/bench/site.env" ] && { set -a; source "$BENCH/bench/site.env"; set +a; }
export CONTAINERS_CONF_OVERRIDE="$BENCH/bench/lib/containers-bench.conf"
# MEDWEAR_SRC_REV (which MEDWEAR commit to build) comes from the condition file
cond="$BENCH/bench/${BENCH_CONDITION:?}"; [ -f "$cond" ] || cond="$BENCH_CONDITION"
set -a; source "$cond"; set +a
if [ -n "${BENCH_PROXY:-}" ]; then
    P="$BENCH_PROXY"
    export HTTPS_PROXY="$P" HTTP_PROXY="$P" https_proxy="$P" http_proxy="$P" NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1
fi
echo "[$(date -Is)] job ${SLURM_JOB_ID:-?} on $(hostname), cgroup $(cat /proc/self/cgroup)"
"$BENCH/bench/prepare_images.sh"
echo "[$(date -Is)] prepare_images done"
