#!/bin/bash
# Create bench/.env.medwear-bench with fresh random secrets for the isolated
# MEDWEAR benchmark stack. Values are never printed. Refuses to overwrite.
set -euo pipefail
BENCH="${BENCH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
out="$BENCH/bench/.env.medwear-bench"
[ -e "$out" ] && { echo "$out exists; delete it first if you really want new secrets" >&2; exit 1; }
r() { openssl rand -hex "${1:-24}"; }
umask 077
PGPW=$(r); INFLUXTOK=$(r 32)
cat > "$out" <<ENV
# Isolated MEDWEAR benchmark stack (synthetic data only). Generated $(date -Is).
POSTGRES_USER=medwear
POSTGRES_PASSWORD=$PGPW
POSTGRES_DB=postgres
POSTGRES_HOST=medwear-postgres
POSTGRES_PORT=5432
DATABASE_URL=postgresql+asyncpg://medwear:$PGPW@medwear-postgres:5432/medwear_benchmark
DOCKER_INFLUXDB_INIT_MODE=setup
DOCKER_INFLUXDB_INIT_USERNAME=bench
DOCKER_INFLUXDB_INIT_PASSWORD=$(r)
DOCKER_INFLUXDB_INIT_ORG=medwear
DOCKER_INFLUXDB_INIT_BUCKET=medwear_bench
DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=$INFLUXTOK
INFLUX_URL=http://medwear-influxdb:8086
INFLUX_TOKEN=$INFLUXTOK
INFLUX_ORG=medwear
INFLUX_BUCKET=medwear_bench
MINIO_ROOT_USER=benchroot
MINIO_ROOT_PASSWORD=$(r)
MINIO_API_ACCESS_KEY=benchapi
MINIO_API_SECRET_KEY=$(r)
MINIO_ENDPOINT=medwear-minio:9000
KEYCLOAK_ADMIN=benchadmin
KEYCLOAK_ADMIN_PASSWORD=$(r)
KEYCLOAK_URL=http://medwear-keycloak:8080
KEYCLOAK_REALM=medwear
KEYCLOAK_CLIENT_ID=med-api
KEYCLOAK_CLIENT_SECRET=$(r)
KC_DB_USERNAME=medwear
KC_DB_PASSWORD=$PGPW
DEVICE_API_KEYS=
SERVER_HOST=localhost
SERVER_IP=127.0.0.1
ENV
chmod 600 "$out"
echo "wrote $out (mode 600)"
