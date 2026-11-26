#!/bin/bash
# Start the Siesta API once MinIO and the Spark master are up.
# SIESTA_MINIO_ADDR / SIESTA_MASTER_ADDR (host:port) are set by supervisord.conf.
set -euo pipefail

wait_for() {
    local name="$1" check="$2" tries=120
    until eval "$check" >/dev/null 2>&1; do
        tries=$((tries - 1))
        if (( tries == 0 )); then
            echo "[siesta-api] $name not reachable, giving up" >&2
            exit 1
        fi
        sleep 1
    done
}

wait_for minio        "curl -fsS -o /dev/null http://${SIESTA_MINIO_ADDR}/minio/health/live"
wait_for spark-master "exec 3<>/dev/tcp/${SIESTA_MASTER_ADDR%:*}/${SIESTA_MASTER_ADDR##*:}"

export USER=spark LOGNAME=spark
cd "${SIESTA_PROJECT_ROOT:-/workspace}"
echo "[siesta-api] starting with config ${SIESTA_CONFIG}"
exec python3.12 main.py --config "${SIESTA_CONFIG}"
