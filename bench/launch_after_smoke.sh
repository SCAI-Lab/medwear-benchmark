#!/bin/bash
# Launch the campaign only if the exclusive-node smoke job(s) ended with status ok.
BENCH="${BENCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$BENCH" || exit 1
echo "[$(date -Is)] waiting for smoke jobs $1"
while squeue -h -j "$1" 2>/dev/null | grep -q .; do sleep 30; done
ok=1
for id in ${1//,/ }; do
    st=$(awk -F'\t' -v j="$id" '$2==j && $7!="started" {s=$7} END {print s}' results/run_index.tsv)
    echo "smoke $id: ${st:-no final row}"
    grep -h "^acceptance:" results/*_smoke_smoke_"$id"/summary.txt 2>/dev/null
    [ "$st" = ok ] || ok=0
done
if [ "$ok" = 1 ]; then
    echo "[$(date -Is)] smoke ok -> launching campaign"
    CAMPAIGN_SEED=20261023 bench/campaign.sh campaigns/medwear_sessions_round5.tsv
else
    echo "[$(date -Is)] NOT launching: a smoke run failed"
fi
echo "[$(date -Is)] LAUNCHER DONE"
