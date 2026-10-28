#!/bin/bash
# Submit a campaign as randomised blocks: block r contains every (job, condition)
# line whose repetition count is >= r, in an order shuffled with CAMPAIGN_SEED
# (recorded). Blocks run one after another. Jobs are chained with
# --dependency=afterany (plus the singleton job name from submit.sh) so exactly
# one runs at a time, each with the CPU/memory of its condition file; a failed
# job does not stop the chain and stays in results/run_index.tsv.
#   CAMPAIGN_SEED=20261023 bench/campaign.sh campaigns/medwear_sessions_round5.tsv
set -euo pipefail
BENCH="${BENCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
: "${CAMPAIGN_SEED:?set CAMPAIGN_SEED (recorded in the manifest)}"
plan="$BENCH/bench/$1"
[ -f "$plan" ] || plan="$1"
rules="$BENCH/bench/EXCLUSION_RULES.md"
grep -q "RULES FINAL: yes" "$rules" || { echo "EXCLUSION_RULES.md is not marked 'RULES FINAL: yes'; write the rules before the runs" >&2; exit 1; }
ts=$(date +%Y%m%d_%H%M%S)
manifest="$BENCH/results/campaigns/${ts}_$(basename "$plan" .tsv).tsv"
mkdir -p "$(dirname "$manifest")"
{
    echo "# campaign $plan sha256=$(sha256sum "$plan" | cut -c1-16)"
    echo "# exclusion rules sha256=$(sha256sum "$rules" | cut -c1-16)"
    echo "# seed=$CAMPAIGN_SEED (randomised blocks)"
    printf 'order\tjob_id\tjob\tcondition\trep\n'
} > "$manifest"
mapfile -t lines < <(grep -vE '^\s*(#|$)' "$plan")
maxrep=0
for l in "${lines[@]}"; do r=$(echo "$l" | cut -f3); (( r > maxrep )) && maxrep=$r; done
prev=""; order=0
for rep in $(seq 1 "$maxrep"); do
    # lines of this block, shuffled reproducibly from (seed, block number)
    mapfile -t block < <(printf '%s\n' "${lines[@]}" | awk -F'\t' -v r="$rep" '$3 >= r' \
        | python3 -c "import random,sys; L=sys.stdin.read().splitlines(); random.Random(int(sys.argv[1])*1000+int(sys.argv[2])).shuffle(L); print('\n'.join(L))" "$CAMPAIGN_SEED" "$rep")
    for l in "${block[@]}"; do
        [ -n "$l" ] || continue
        job=$(echo "$l" | cut -f1); cond=$(echo "$l" | cut -f2)
        id=$(BENCH_AFTER="$prev" "$BENCH/bench/submit.sh" "$job" "$cond")
        order=$((order + 1))
        printf '%s\t%s\t%s\t%s\t%s\n' "$order" "$id" "$job" "$cond" "$rep" >> "$manifest"
        echo "submitted $id: block $rep  $job $cond"
        prev="$id"
    done
done
echo "manifest: $manifest"
