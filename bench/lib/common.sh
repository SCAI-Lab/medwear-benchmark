#!/bin/bash
# Shared helpers for the MEDWEAR benchmark jobs.
#
# Every job script does:
#   set -euo pipefail
#   BENCH="${BENCH:-$HOME/benchmark}"
#   source "$BENCH/bench/lib/common.sh"
#   bench_init <kind> <required var>...
#   stack_fresh
#   ...test...
#   bench_finish
#
# Design rules:
#   * the platform stack is created inside the Slurm job (podman, cgroupfs) and
#     destroyed at the end; volumes are per-job and hold only synthetic data
#   * jobs run with the CPU/memory allocation of the condition (BENCH_CPUS /
#     BENCH_MEM; BENCH_EXCLUSIVE=1 = --exclusive): the containers inherit the
#     job's cpuset and memory limit; the node state is recorded at start and end
#   * experiment-defining variables have no defaults; a missing one fails the job
#   * only the benchmark stack runs in a job; anything else running fails the job
#   * delivery is verified with counts only (never row contents)
#   * nothing in here prints a secret

BENCH="${BENCH:-$HOME/benchmark}"
BENCH_LIB="$BENCH/bench/lib"
BENCH_TESTS="$BENCH/bench/tests"
# site-specific tool paths (LOCUST_CMD, BENCH_PYTHON, PODMAN_COMPOSE); no secrets
# shellcheck disable=SC1091
[ -f "$BENCH/bench/site.env" ] && { set -a; source "$BENCH/bench/site.env"; set +a; }
BENCH_PY="${BENCH_PYTHON:-python3}"
LOCUST_CMD="${LOCUST_CMD:-python3 -m locust}"
PODMAN_COMPOSE="${PODMAN_COMPOSE:-podman-compose}"

MW_PROJECT=mwbench
MW_COMPOSE="$BENCH/bench/compose/medwear-bench.yml"
MW_ENV_FILE="$BENCH/bench/.env.medwear-bench"
export MW_BENCH_PORT="${MW_BENCH_PORT:-19080}"
export MW_BENCH_KC_PORT="${MW_BENCH_KC_PORT:-18180}"
export MW_BENCH_TLS_PORT="${MW_BENCH_TLS_PORT:-19443}"
export MW_BENCH_API_PORT="${MW_BENCH_API_PORT:-19081}"

PREFLIGHT_N="${PREFLIGHT_N:-20}"
PREFLIGHT_MAX_MS="${PREFLIGHT_MAX_MS:-100}"
RUN_INDEX="$BENCH/results/run_index.tsv"

BENCH_FAIL_REASON=""
BENCH_STAGE="init"

log()  { echo "[$(date -Is)] $*"; }
fail() { BENCH_FAIL_REASON="[$BENCH_STAGE] $*"; log "FATAL ($BENCH_STAGE): $*" >&2; exit 1; }

require_vars() {
    local missing=() v
    for v in "$@"; do
        [ -n "${!v:-}" ] || missing+=("$v")
    done
    [ ${#missing[@]} -eq 0 ] || fail "required variables unset: ${missing[*]}"
}

_is_secret_name() { [[ "$1" =~ (PASS|SECRET|TOKEN|_KEY|KEYS|CREDENTIAL|PW$|PRIVATE) ]]; }

# env snapshot with secret-like values replaced
write_redacted_env() {
    local out="$1" line k
    : > "$out"
    while IFS= read -r line; do
        k="${line%%=*}"
        if _is_secret_name "$k"; then echo "$k=<redacted>" >> "$out"; else echo "$line" >> "$out"; fi
    done < <(env | sort)
}

_index_row() {   # status reason
    mkdir -p "$(dirname "$RUN_INDEX")"
    [ -f "$RUN_INDEX" ] || printf 'time\tjob_id\tkind\tplatform\tcondition\tsmoke\tstatus\treason\trun_dir\n' > "$RUN_INDEX"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -Is)" "${SLURM_JOB_ID:-nojob}" "$BENCH_KIND" \
        "${PLATFORM:-?}" "${BENCH_CONDITION:-none}" "$( [ "${BENCH_PILOT:-0}" = 1 ] && echo pilot || echo "${SMOKE:-0}")" "$1" "${2//$'\t'/ }" "${RUN_DIR:-}" >> "$RUN_INDEX"
}

_on_exit() {
    local rc=$?
    set +e
    trap - EXIT
    if [ "$rc" -eq 0 ]; then
        _index_row ok ""
    else
        [ -n "$BENCH_FAIL_REASON" ] || BENCH_FAIL_REASON="[$BENCH_STAGE] exit code $rc"
        _index_row failed "$BENCH_FAIL_REASON"
        if [ -n "${RUN_DIR:-}" ]; then
            echo "FAILED: $BENCH_FAIL_REASON" >> "$RUN_DIR/summary.txt"
            echo "================ SUMMARY ($RUN_DIR) ================"
            cat "$RUN_DIR/summary.txt"
        fi
    fi
    profile_stop
    if [ -n "${RUN_DIR:-}" ] && [ -n "${PLATFORM:-}" ] && [ "${BENCH_STACK_STARTED:-0}" = 1 ]; then
        save_container_logs
        log_podman_ps end
    fi
    if [ "${KEEP_STACK:-0}" != 1 ] && [ "${BENCH_STACK_STARTED:-0}" = 1 ]; then
        stack_down
    fi
    if [ -n "${SLURM_JOB_ID:-}" ] && [ -n "${RUN_DIR:-}" ]; then
        cp "${SLURM_SUBMIT_DIR:-.}/${SLURM_JOB_NAME}_${SLURM_JOB_ID}.out" "$RUN_DIR/" 2>/dev/null
    fi
    exit "$rc"
}
_on_term() { BENCH_FAIL_REASON="[$BENCH_STAGE] received SIGTERM (time limit or scancel)"; exit 143; }

# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

apply_smoke_overrides() {
    # SMOKE=1: validate the plumbing of a job in a few minutes. Overrides the
    # condition file; recorded in settings.json as "smoke": true.
    [ "${SMOKE:-0}" = 1 ] || return 0
    export ITERATIONS=2 SIZES="100,1000" USERS="2" STAGE_SECONDS=30 SPAWN_RATE=2
    export STAGE_ISOLATION=drain DRAIN_SECONDS=5
    export DUR_REPS=1 DUR_WRITE_SECONDS=20 DUR_FAULTS=restart
    export DUR_TARGETS=med-api
    export DURATION=30
    log "SMOKE=1: iterations=2, sizes=$SIZES, stages ${STAGE_SECONDS}s, users=$USERS, durability 1 case"
}

bench_init() {
    BENCH_KIND="$1"; shift
    trap _on_exit EXIT
    trap _on_term TERM INT

    if [ -n "${BENCH_CONDITION:-}" ]; then
        local cond="$BENCH_CONDITION"
        [ -f "$cond" ] || cond="$BENCH/bench/$BENCH_CONDITION"
        [ -f "$cond" ] || fail "condition file not found: $BENCH_CONDITION"
        BENCH_CONDITION_FILE="$cond"
        set -a; source "$cond"; set +a
    fi

    require_vars PLATFORM
    [ "$PLATFORM" = medwear ] || fail "PLATFORM must be medwear (got '$PLATFORM')"
    apply_smoke_overrides

    local common=(WRITES_ONLY BATCH_TIMEOUT LOCUST_TIMEOUT BENCH_CPUS BENCH_MEM BENCH_EXCLUSIVE)
    local plat=(BENCH_PROJECT DB_POOL_SIZE UVICORN_WORKERS MEDWEAR_SRC_REV BATCH_RATE_LIMIT)
    export BENCH_REQUIRED="${common[*]} ${plat[*]} $*"
    # shellcheck disable=SC2086
    require_vars $BENCH_REQUIRED
    [ "$WRITES_ONLY" = 1 ] || fail "WRITES_ONLY=$WRITES_ONLY: only the write workload is implemented"
    [[ "$BENCH_PROJECT" =~ ^medwear_bench[a-z0-9_]*$ ]] || fail "BENCH_PROJECT must start with medwear_bench (got '$BENCH_PROJECT')"
    [ -f "$MW_ENV_FILE" ] || fail "missing $MW_ENV_FILE (create it with bench/make_bench_env.sh)"
    export MEDWEAR_SRC="$BENCH/bench/medwear_src/$MEDWEAR_SRC_REV"
    [ -f "$MEDWEAR_SRC/VERSION" ] || fail "MEDWEAR source $MEDWEAR_SRC (MEDWEAR_SRC_REV=$MEDWEAR_SRC_REV) missing or without VERSION"
    if [ -z "${SLURM_JOB_ID:-}" ] && [ "${ALLOW_NO_SLURM:-0}" != 1 ]; then
        fail "not running inside a Slurm job (submit with bench/submit.sh)"
    fi
    if [ -n "${SLURM_JOB_ID:-}" ] && [ "${SLURM_CPUS_PER_TASK:-}" != "$BENCH_CPUS" ]; then
        fail "job has ${SLURM_CPUS_PER_TASK:-?} CPUs but the condition asks for BENCH_CPUS=$BENCH_CPUS (submit with bench/submit.sh)"
    fi
    if [ -n "${SLURM_JOB_ID:-}" ] && [ "$BENCH_EXCLUSIVE" = 1 ] \
        && ! scontrol show job "$SLURM_JOB_ID" 2>/dev/null | grep -q "OverSubscribe=NO"; then
        fail "condition asks for an exclusive node but job $SLURM_JOB_ID is not exclusive (submit with bench/submit.sh)"
    fi

    local suffix=""; [ "${SMOKE:-0}" = 1 ] && suffix="_smoke"; [ "${BENCH_PILOT:-0}" = 1 ] && suffix="_pilot"
    export RUN_DIR="$BENCH/results/$(date +%Y%m%d_%H%M%S)_${PLATFORM}_${BENCH_KIND}${suffix}_${SLURM_JOB_ID:-nojob}"
    mkdir -p "$RUN_DIR"
    _index_row started ""
    [ -n "${BENCH_CONDITION_FILE:-}" ] && cp "$BENCH_CONDITION_FILE" "$RUN_DIR/condition.env"
    write_redacted_env "$RUN_DIR/run.env"

    # no site HTTP proxy for local traffic
    unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY
    export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"

    # Rootless podman defaults to the systemd cgroup manager, which puts the
    # containers under user@<uid>.service, outside the job allocation.
    export CONTAINERS_CONF_OVERRIDE="$BENCH_LIB/containers-bench.conf"

    export BENCH_JOB_START_EPOCH; BENCH_JOB_START_EPOCH=$(date +%s)
    export PLATFORM RUN_DIR BENCH BENCH_LIB
    node_state start
    log "RUN_DIR=$RUN_DIR"
}

# Co-located load is expected (no --exclusive); record it so it can be reported.
node_state() {   # phase
    local jobcg
    jobcg=$(cut -d: -f3 /proc/self/cgroup | sed -E 's#(/job_[0-9]+).*#\1#')
    {
        echo "job=${SLURM_JOB_ID:-nojob} node=$(hostname) kind=$BENCH_KIND platform=$PLATFORM smoke=${SMOKE:-0} phase=$1"
        echo "job cgroup: $(cat /proc/self/cgroup)"
        echo "allocation: cpus=${SLURM_CPUS_PER_TASK:-?} mem_per_node_mb=${SLURM_MEM_PER_NODE:-?} cpuset=$(cat "/sys/fs/cgroup$jobcg/cpuset.cpus.effective" 2>/dev/null || echo ?)"
        echo "nproc(visible)=$(nproc)"
        uptime
        echo "--- jobs on the node ---"
        squeue -h -w "$(hostname -s)" -o '%i %u %j %C %m %T' 2>/dev/null
        echo "--- other users' containers (conmon processes) ---"
        ps -eo user:32,comm --no-headers | awk -v me="$(id -un)" '$2=="conmon" && $1!=me {print $1}' | sort | uniq -c
        echo "--- top CPU processes on node (all users) ---"
        ps -eo user:12,pid,pcpu,pmem,comm --sort=-pcpu | head -15
    } > "$RUN_DIR/node_state_$1.txt" 2>&1
    echo "node_state_$1: load=$(cut -d' ' -f1-3 /proc/loadavg) other_jobs=$(squeue -h -w "$(hostname -s)" -o %i 2>/dev/null | grep -vcx "${SLURM_JOB_ID:-x}" || true)" >> "$RUN_DIR/summary.txt"
}

# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------

own_project()   { echo "$MW_PROJECT"; }

# container name of a compose service in our project
ctr() {
    podman ps -a --filter "label=com.docker.compose.project=$(own_project)" \
        --filter "label=com.docker.compose.service=$1" --format '{{.Names}}' | head -1
}

mw_compose() { "$PODMAN_COMPOSE" -p "$MW_PROJECT" -f "$MW_COMPOSE" "$@"; }

log_podman_ps() {   # phase
    podman ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}\t{{index .Labels "com.docker.compose.project"}}' \
        > "$RUN_DIR/podman_ps_$1.txt" 2>&1 || true
}

# Fail if anything except our own project is running (other stacks, stray
# containers). They share the node and distort results.
assert_no_foreign_containers() {   # phase
    local foreign
    foreign=$(podman ps --format '{{.Names}} {{index .Labels "com.docker.compose.project"}}' \
        | awk -v own="$(own_project)" '$2 != own {print $1}' \
        | grep -vE "${ALLOW_FOREIGN_CONTAINERS:-^$}" || true)
    if [ -n "$foreign" ]; then
        echo "$foreign" > "$RUN_DIR/foreign_containers_$1.txt"
        fail "containers outside project $(own_project) are running ($1): $(echo "$foreign" | tr '\n' ' ')"
    fi
    # Other users' containers are not ours to stop and share the node by
    # design (no --exclusive); they are recorded in node_state_<phase>.txt.
}

# Host ports the stack publishes must be free.
assert_ports_free() {
    local ports p busy=""
    ports="$MW_BENCH_PORT $MW_BENCH_TLS_PORT $MW_BENCH_API_PORT $MW_BENCH_KC_PORT"
    for p in $ports; do
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE ":$p\$" && busy="$busy $p"
    done
    [ -z "$busy" ] || fail "host ports already in use before stack start:$busy"
}

wait_until() {   # timeout_s description command...
    local timeout="$1" what="$2"; shift 2
    local t0; t0=$(date +%s)
    until "$@" >/dev/null 2>&1; do
        (( $(date +%s) - t0 > timeout )) && fail "timeout after ${timeout}s waiting for $what"
        sleep 3
    done
    log "ready: $what ($(( $(date +%s) - t0 ))s)"
}

_http_ok() { curl -s --noproxy '*' -o /dev/null -m 5 -w '%{http_code}' "$1" | grep -q '^200$'; }
_mw_health_ok() { curl -s --noproxy '*' -m 10 --cacert "$BENCH_CA_FILE" "https://127.0.0.1:$MW_BENCH_TLS_PORT/v1/health" | grep -q '"status": *"ok"'; }

stack_down() {
    BENCH_STAGE="stack_down"
    log "stack down ($(own_project), volumes removed)"
    mw_compose down -v >> "$RUN_DIR/stack.log" 2>&1 || true
    BENCH_STACK_STARTED=0
}

medwear_up() {
    mw_compose up -d postgres influxdb minio >> "$RUN_DIR/stack.log" 2>&1 || fail "compose up (stores) failed, see stack.log"
    local pg; pg=$(ctr postgres)
    wait_until 120 "postgres" podman exec "$pg" sh -c 'pg_isready -U "$POSTGRES_USER"'
    podman exec "$pg" sh -c 'psql -U "$POSTGRES_USER" -d postgres -tAc "CREATE DATABASE keycloak"' >> "$RUN_DIR/stack.log" 2>&1 \
        || fail "could not create keycloak database"
    mw_compose up -d minio-setup >> "$RUN_DIR/stack.log" 2>&1 || true
    podman wait "$(ctr minio-setup)" >> "$RUN_DIR/stack.log" 2>&1 || true
    mw_compose up -d keycloak >> "$RUN_DIR/stack.log" 2>&1 || fail "compose up keycloak failed"
    wait_until 300 "keycloak" _http_ok "http://127.0.0.1:$MW_BENCH_KC_PORT/realms/medwear/.well-known/openid-configuration"
    mw_compose up -d med-api >> "$RUN_DIR/stack.log" 2>&1 || fail "compose up med-api failed"
    # dedicated benchmark project DB, created with the API's own create_project_db()
    wait_until 120 "api container" podman exec "$(ctr med-api)" true
    podman exec -e BENCH_PROJECT "$(ctr med-api)" python -c \
        "import asyncio,os; from medwear_med_api.db import create_project_db; asyncio.run(create_project_db(os.environ['BENCH_PROJECT']))" \
        >> "$RUN_DIR/stack.log" 2>&1 || fail "create_project_db($BENCH_PROJECT) failed"
    mw_compose up -d frontend medwear-nginx >> "$RUN_DIR/stack.log" 2>&1 || fail "compose up nginx failed"
    # self-signed certificate generated in the nginx image: clients verify against it
    export BENCH_CA_FILE="$RUN_DIR/bench-ca.crt"
    podman cp "$(ctr medwear-nginx):/etc/nginx/certs/localhost.crt" "$BENCH_CA_FILE" >> "$RUN_DIR/stack.log" 2>&1 \
        || fail "could not copy the nginx certificate"
    wait_until 180 "medwear https /v1/health status ok" _mw_health_ok
    medwear_provision_user
    export MEDWEAR_URL="https://127.0.0.1:$MW_BENCH_TLS_PORT"
    export MEDWEAR_API_DIRECT_URL="http://127.0.0.1:$MW_BENCH_API_PORT"
    export MEASURED_ENDPOINT="$MEDWEAR_URL/v1/data/batch"
    export MEASURED_HOP="as deployed: client (host, 127.0.0.1) -> HTTPS mwbench-nginx:443 (published 127.0.0.1:$MW_BENCH_TLS_PORT) -> HTTP mwbench-api:8080; latency test also measures client -> mwbench-api:8080 directly (127.0.0.1:$MW_BENCH_API_PORT)"
    export HEALTH_URL="$MEDWEAR_URL/v1/health"
}

# Benchmark Keycloak user with only the 'device' client role, assigned only to
# the benchmark project. The password is random per job and never written out.
medwear_provision_user() {
    local kc; kc=$(ctr keycloak)
    export KC_REALM=medwear KC_CLIENT=med-api KC_ROLE=device BENCH_KC_USER=bench-loader
    BENCH_KC_PW=$(openssl rand -hex 24); export BENCH_KC_PW
    podman exec -e KC_REALM -e KC_CLIENT -e KC_ROLE -e BENCH_KC_USER -e BENCH_KC_PW "$kc" bash -c '
        set -e
        K=/opt/keycloak/bin/kcadm.sh
        $K config credentials --server http://localhost:8080 --realm master --user "$KEYCLOAK_ADMIN" --password "$KEYCLOAK_ADMIN_PASSWORD" >/dev/null
        $K create users -r "$KC_REALM" -s username="$BENCH_KC_USER" -s enabled=true -s emailVerified=true >/dev/null
        $K set-password -r "$KC_REALM" --username "$BENCH_KC_USER" --new-password "$BENCH_KC_PW" >/dev/null
        $K add-roles -r "$KC_REALM" --uusername "$BENCH_KC_USER" --cclientid "$KC_CLIENT" --rolename "$KC_ROLE" >/dev/null
    ' >> "$RUN_DIR/stack.log" 2>&1 || fail "keycloak user provisioning failed"
    BENCH_KC_CLIENT_SECRET=$(podman exec -e KC_REALM -e KC_CLIENT "$kc" bash -c '
        K=/opt/keycloak/bin/kcadm.sh
        CID=$($K get clients -r "$KC_REALM" -q clientId="$KC_CLIENT" --fields id --format csv --noquotes)
        $K get "clients/$CID/client-secret" -r "$KC_REALM" --fields value --format csv --noquotes' 2>/dev/null || true)
    export BENCH_KC_CLIENT_SECRET
    KC_TOKEN_LIFESPAN=$(podman exec -e KC_REALM "$kc" bash -c \
        '/opt/keycloak/bin/kcadm.sh get "realms/$KC_REALM" --fields accessTokenLifespan --format csv --noquotes' 2>/dev/null || echo "error")
    export KC_TOKEN_LIFESPAN
    export KC_TOKEN_URL="http://127.0.0.1:$MW_BENCH_KC_PORT/realms/$KC_REALM/protocol/openid-connect/token"
    podman exec -i -e BENCH_PROJECT -e BENCH_KC_USER "$(ctr postgres)" sh -c \
        'psql -U "$POSTGRES_USER" -d "$BENCH_PROJECT" -v ON_ERROR_STOP=1 -v u="$BENCH_KC_USER" -q' \
        >> "$RUN_DIR/stack.log" 2>&1 <<'SQL' || fail "user_project insert failed"
INSERT INTO user_project (username, project_id, role) VALUES (:'u', current_database(), 'collaborator');
SQL
    log "provisioned keycloak user $BENCH_KC_USER (role $KC_ROLE, project $BENCH_PROJECT, token lifespan ${KC_TOKEN_LIFESPAN}s)"
}

stack_up() {
    BENCH_STAGE="stack_up"
    log "stack up ($(own_project)) inside job ${SLURM_JOB_ID:-nojob}"
    BENCH_STACK_STARTED=1
    medwear_up
}

check_cgroups() {   # tag
    BENCH_STAGE="cgroup_check"
    "$BENCH_PY" "$BENCH_LIB/cgroup_check.py" check --project "$(own_project)" --job-id "${SLURM_JOB_ID:-}" \
        --out "$RUN_DIR/cgroups_$1.tsv" || fail "container cgroups are not inside job ${SLURM_JOB_ID:-} (see cgroups_$1.tsv)"
}

# 20 health requests; abort if the median is above PREFLIGHT_MAX_MS (a latency
# floor in the stack shows up here before any test runs).
preflight() {   # tag
    BENCH_STAGE="preflight"
    local out="$RUN_DIR/preflight_$1.tsv" i
    echo -e "time_namelookup\ttime_connect\ttime_starttransfer\ttime_total\thttp_code" > "$out"
    for i in $(seq 1 "$PREFLIGHT_N"); do
        curl -s --noproxy '*' -o /dev/null -m 30 ${BENCH_CA_FILE:+--cacert "$BENCH_CA_FILE"} \
            -w '%{time_namelookup}\t%{time_connect}\t%{time_starttransfer}\t%{time_total}\t%{http_code}\n' \
            "$HEALTH_URL" >> "$out" || echo -e "0\t0\t0\t30\t000" >> "$out"
    done
    local bad n
    bad=$(awk -F'\t' 'NR>1 && $5!="200"' "$out" | wc -l)
    n=$((PREFLIGHT_N))
    PREFLIGHT_MEDIAN_MS=$(awk -F'\t' 'NR>1 {print $4*1000}' "$out" | sort -n \
        | awk '{t[NR]=$1} END {m=(NR%2)?t[(NR+1)/2]:(t[NR/2]+t[NR/2+1])/2; printf "%.1f", m}')
    export PREFLIGHT_MEDIAN_MS
    log "preflight $HEALTH_URL: median ${PREFLIGHT_MEDIAN_MS} ms over $n requests, non-200: $bad"
    echo "preflight_$1 median_ms=$PREFLIGHT_MEDIAN_MS non200=$bad url=$HEALTH_URL" >> "$RUN_DIR/summary.txt"
    [ "$bad" -eq 0 ] || fail "preflight: $bad/$n health requests were not HTTP 200"
    awk -v m="$PREFLIGHT_MEDIAN_MS" -v lim="$PREFLIGHT_MAX_MS" 'BEGIN {exit !(m <= lim)}' \
        || fail "preflight median ${PREFLIGHT_MEDIAN_MS} ms > ${PREFLIGHT_MAX_MS} ms: the latency floor is still there, not running tests"
}

snapshot_settings() {
    BENCH_STAGE="settings_snapshot"
    local cond_vars=""
    [ -n "${BENCH_CONDITION_FILE:-}" ] && cond_vars=$(grep -oE '^[A-Z_][A-Z0-9_]*=' "$BENCH_CONDITION_FILE" | tr -d '=' | tr '\n' ' ')
    "$BENCH_PY" "$BENCH_LIB/settings_snapshot.py" --platform "$PLATFORM" --project "$(own_project)" \
        --required "$BENCH_REQUIRED" --record "$cond_vars" --out "$RUN_DIR/settings.json" \
        || fail "settings snapshot failed (see output above)"
}

throttle_snapshot() {   # phase
    [ "$1" = end ] && { [ "${BENCH_THROTTLE_END_DONE:-0}" = 1 ] && return 0; BENCH_THROTTLE_END_DONE=1; }
    "$BENCH_PY" "$BENCH_LIB/cgroup_check.py" throttle --project "$(own_project)" --phase "$1" \
        --out "$RUN_DIR/cpu_throttle.log" || log "WARN: throttle snapshot reported errors (see cpu_throttle.log)"
}

# down (previous bench volumes), foreign check, up, cgroup check, preflight, snapshot
stack_fresh() {
    local tag="${1:-start}"
    [ "${BENCH_STACK_STARTED:-0}" = 1 ] && stack_down
    if [ "$tag" = start ]; then
        stack_down
        log_podman_ps start
        assert_no_foreign_containers start
        assert_ports_free
    fi
    stack_up
    check_cgroups "$tag"
    preflight "$tag"
    if [ "$tag" = start ]; then
        snapshot_settings
        throttle_snapshot start
    else
        echo "stack recreated: $tag" >> "$RUN_DIR/summary.txt"
    fi
    BENCH_STAGE="test"
}

# Between load stages: fresh stack, or a logged drain period.
stage_isolate() {   # stage
    case "$STAGE_ISOLATION" in
        fresh) log "stage isolation: fresh stack before $1"; stack_fresh "before_$1" ;;
        drain) require_vars DRAIN_SECONDS
               log "stage isolation: drain ${DRAIN_SECONDS}s before $1"; sleep "$DRAIN_SECONDS"
               echo "drain ${DRAIN_SECONDS}s before $1" >> "$RUN_DIR/summary.txt" ;;
        *) fail "STAGE_ISOLATION must be fresh or drain (got '$STAGE_ISOLATION')" ;;
    esac
}

# ---------------------------------------------------------------------------
# counts / delivery
# ---------------------------------------------------------------------------

counts_snapshot() {   # phase
    if ! "$BENCH_PY" "$BENCH_LIB/counts.py" --platform "$PLATFORM" --phase "$1" --out "$RUN_DIR/counts.jsonl"; then
        [[ "$1" == *start* || "$1" == *before* ]] && fail "count snapshot '$1' failed: cannot verify delivery"
        log "WARN: count snapshot '$1' reported errors (recorded in counts.jsonl)"
    fi
}

delivery_report() {   # label before after client_summary...
    local label="$1" before="$2" after="$3"; shift 3
    "$BENCH_PY" "$BENCH_LIB/delivery.py" --label "$label" --counts "$RUN_DIR/counts.jsonl" \
        --before "$before" --after "$after" --out "$RUN_DIR/delivery.jsonl" "$@" | tee -a "$RUN_DIR/summary.txt" || true
}

# ---------------------------------------------------------------------------
# profiling / logs
# ---------------------------------------------------------------------------

profile_start() {   # output.csv
    "$BENCH_PY" "$BENCH_LIB/profile_containers.py" --project "$(own_project)" --output "$1" \
        > "${1%.csv}.log" 2>&1 &
    BENCH_PROFILE_PID=$!
}
profile_stop() {
    if [ -n "${BENCH_PROFILE_PID:-}" ]; then
        kill "$BENCH_PROFILE_PID" 2>/dev/null || true
        wait "$BENCH_PROFILE_PID" 2>/dev/null || true
        BENCH_PROFILE_PID=""
    fi
}

# Logs come from the isolated stack (synthetic data only). Server-side timeouts
# are counted separately so they can be compared with client timeouts.
save_container_logs() {
    mkdir -p "$RUN_DIR/container_logs"
    local c
    for c in $(podman ps -a --filter "label=com.docker.compose.project=$(own_project)" --format '{{.Names}}'); do
        podman logs "$c" > "$RUN_DIR/container_logs/$c.log" 2>&1 || true
    done
    {
        echo "# server-side timeout indicators (counts of matching log lines)"
        for c in "$RUN_DIR"/container_logs/*.log; do
            [ -f "$c" ] || continue
            printf '%s\tupstream_timed_out=%s\thttp_504=%s\ttimeout_exceptions=%s\n' "$(basename "$c" .log)" \
                "$(grep -c 'upstream timed out' "$c" || true)" \
                "$(grep -cE '" 504 ' "$c" || true)" \
                "$(grep -ciE 'TimeoutException|timed out|DeadlineExceeded' "$c" || true)"
        done
    } > "$RUN_DIR/server_timeouts.tsv"
}

bench_finish() {
    BENCH_STAGE="finish"
    throttle_snapshot end
    node_state end
    log_podman_ps end_check
    echo "status=ok" >> "$RUN_DIR/summary.txt"
    echo "================ SUMMARY ($RUN_DIR) ================"
    cat "$RUN_DIR/summary.txt"
}
