#!/bin/bash
# Submit one benchmark job.
#   bench/submit.sh <job> <condition.env> [extra sbatch args...]
#   bench/submit.sh smoke conditions/smoke_medwear.env
#   BENCH_AFTER=<jobid> bench/submit.sh ...      # also wait for that job (campaign order)
#   BENCH_PILOT=1 bench/submit.sh ...            # pilot run: labelled, excluded from the analysis
# <job>: latency load durability validation idempotency baseline smoke prepare_images
#
# CPU/memory from the condition file (BENCH_CPUS, BENCH_MEM); BENCH_EXCLUSIVE=1
# adds --exclusive (whole node, no other jobs). Slurm confines the job (and the
# containers it starts) to its allocation. All bench jobs share one job name with --dependency=singleton,
# so two bench jobs never run at the same time. Prints the Slurm job id.
set -euo pipefail
BENCH="${BENCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
job="$1"; cond="$2"; shift 2
script="$BENCH/bench/slurm/job_${job}.sh"
[ -f "$script" ] || { echo "no such job: $script" >&2; exit 1; }
cfile="$BENCH/bench/$cond"; [ -f "$cfile" ] || cfile="$cond"
[ -f "$cfile" ] || { echo "no such condition file: $cond" >&2; exit 1; }
read -r cpus mem excl < <(set -a; source "$cfile"; echo "${BENCH_CPUS:-} ${BENCH_MEM:-} ${BENCH_EXCLUSIVE:-}")
[ -n "$cpus" ] && [ -n "$mem" ] && [ -n "$excl" ] || { echo "$cond must set BENCH_CPUS, BENCH_MEM and BENCH_EXCLUSIVE" >&2; exit 1; }
xflag=(); [ "$excl" = 1 ] && xflag=(--exclusive)
dep="singleton"; [ -n "${BENCH_AFTER:-}" ] && dep="singleton,afterany:$BENCH_AFTER"
mkdir -p "$BENCH/results/slurm"
cd "$BENCH/results/slurm"
sbatch --parsable --job-name=medwear_radar_bench --dependency="$dep" \
    --nodes=1 --ntasks=1 --cpus-per-task="$cpus" --mem="$mem" "${xflag[@]}" \
    --comment="$job $(basename "$cond")" \
    --export=ALL,BENCH="$BENCH",BENCH_CONDITION="$cond",BENCH_PILOT="${BENCH_PILOT:-0}" "$@" "$script"
