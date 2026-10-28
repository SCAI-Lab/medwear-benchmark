#!/bin/bash
# Pull and build every image of the bench stack into YOUR podman storage,
# once, before any job (jobs never pull: no mid-run downloads, fixed digests).
# Run on the login shell of the node, not inside Slurm:
#   bench/prepare_images.sh                # direct internet
#   HTTPS_PROXY=http://<proxy>:<port> HTTP_PROXY=$HTTPS_PROXY bench/prepare_images.sh
set -euo pipefail
BENCH="${BENCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PODMAN_COMPOSE="${PODMAN_COMPOSE:-podman-compose}"
# short image names in Dockerfiles -> docker.io, no TTY prompt (batch job)
export CONTAINERS_REGISTRIES_CONF="$BENCH/bench/lib/registries-bench.conf"
export MW_BENCH_PORT=19080 MW_BENCH_KC_PORT=18180 DB_POOL_SIZE=5 UVICORN_WORKERS=1
BARGS=()
for v in HTTP_PROXY HTTPS_PROXY NO_PROXY; do [ -n "${!v:-}" ] && BARGS+=(--build-arg "$v=${!v}" --build-arg "${v,,}=${!v}"); done

# minio/minio and minio/mc are no longer downloadable from Docker Hub or quay.io;
# a locally present image is used as is (its digest goes into settings.json).
pull_if_missing() { if podman image exists "$1"; then echo "present: $1"; else podman pull "$1"; fi; }

# MEDWEAR code: bench/medwear_src/$MEDWEAR_SRC_REV (git archive of one commit)
: "${MEDWEAR_SRC_REV:?set MEDWEAR_SRC_REV (comes from the condition file)}"
MW_SRC="$BENCH/bench/medwear_src/$MEDWEAR_SRC_REV"
[ -f "$MW_SRC/VERSION" ] || { echo "missing $MW_SRC/VERSION" >&2; exit 1; }
echo "MEDWEAR source: $(grep '^commit=' "$MW_SRC/VERSION")"

echo "== MEDWEAR stores"
for img in docker.io/library/postgres:15-alpine docker.io/library/influxdb:2.7 docker.io/minio/minio:latest \
           docker.io/minio/mc:latest docker.io/keycloak/keycloak:23.0; do pull_if_missing "$img"; done
echo "== MEDWEAR images built from the medwear tree (nginx/frontend always rebuilt, layer cache keeps it fast)"
# API: the medwear Dockerfile with every pip install constrained to
# bench/compose/api-constraints.txt (the medwear requirements are unpinned; an
# unpinned build pulled sqlmodel 0.0.48, which rejects the API's naive datetimes).
# The derived Dockerfile only adds "ARG PIP_CONSTRAINT"; pip reads it from the env.
CONS_DIR="$BENCH/bench/compose"
DERIVED=$(mktemp)
awk '{print} /^FROM /{print "ARG PIP_CONSTRAINT"}' "$MW_SRC/docker/medwear-api.Dockerfile" > "$DERIVED"
podman build "${BARGS[@]}" --no-cache -v "$CONS_DIR:/bench-constraints:ro,z" \
    --build-arg PIP_CONSTRAINT=/bench-constraints/api-constraints.txt \
    --label bench.api_constraints_sha256="$(sha256sum "$CONS_DIR/api-constraints.txt" | cut -c1-16)" \
    -t "localhost/mwbench-api:$MEDWEAR_SRC_REV" -f "$DERIVED" "$MW_SRC"
rm -f "$DERIVED"
echo "API image packages vs constraints:"
podman run --rm --entrypoint pip "localhost/mwbench-api:$MEDWEAR_SRC_REV" freeze --all 2>/dev/null \
    | grep -vE "^(-e |medwear|#)|@ file://" | sort -f > /tmp/mwbench-api-freeze.$$
grep -vE "^#" "$CONS_DIR/api-constraints.txt" | sort -f | diff - /tmp/mwbench-api-freeze.$$ && echo "  identical" || echo "  DIFFERENT (see above)"
rm -f /tmp/mwbench-api-freeze.$$
podman build "${BARGS[@]}" -t "localhost/mwbench-frontend:$MEDWEAR_SRC_REV" "$MW_SRC/frontend"
podman build "${BARGS[@]}" --build-arg SERVER_HOST=localhost --build-arg SERVER_IP=127.0.0.1 \
    -t "localhost/mwbench-nginx:$MEDWEAR_SRC_REV" "$MW_SRC/docker/nginx"

echo "== done"
podman images --format '{{.Repository}}:{{.Tag}} {{.Digest}}' | grep -E 'mwbench|postgres|influxdb|minio|keycloak'
