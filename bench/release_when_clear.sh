#!/bin/bash
# Release held bench jobs once none of YOUR other jobs/containers run (other
# users' work is allowed and recorded by the jobs), then wait for
# them and print their summaries. Run with nohup on the node; checks once a
# minute (squeue + podman ps only), gives up after MAX_WAIT_S.
#   nohup bench/release_when_clear.sh 13617,13618 > results/release_<ids>.log 2>&1 &
set -uo pipefail
BENCH="${BENCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$BENCH" || exit 1
ids="$1"; max="${MAX_WAIT_S:-43200}"; t0=$(date +%s)
echo "[$(date -Is)] waiting to release $ids"
while :; do
    others=$(squeue -h -u "$(id -un)" -o '%i %j %T' | grep -vE "^(${ids//,/|}) " | grep -c RUNNING)
    ctrs=$(podman ps --format '{{index .Labels "com.docker.compose.project"}}' | grep -vcxE 'mwbench|rdbench')
    [ "$others" -eq 0 ] && [ "$ctrs" -eq 0 ] && break
    if [ $(( $(date +%s) - t0 )) -gt "$max" ]; then
        echo "[$(date -Is)] gave up: running_jobs=$others foreign_containers=$ctrs"; exit 3
    fi
    sleep 60
done
echo "[$(date -Is)] node clear after $(( ($(date +%s) - t0) / 60 )) min, releasing $ids"
scontrol release "$ids"
while squeue -h -j "$ids" 2>/dev/null | grep -q .; do sleep 30; done
sacct -n -X -j "$ids" -o JobID,State,ExitCode,Elapsed
for id in ${ids//,/ }; do
    echo; echo "######## $id"
    f="results/slurm/medwear_radar_bench_$id.out"
    sed -n '/================ SUMMARY/,$p' "$f" | grep -v 'stack down'
    grep -m5 FATAL "$f"
done
echo "[$(date -Is)] DONE"
