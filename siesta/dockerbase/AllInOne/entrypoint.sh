#!/bin/bash
# Container entrypoint (runs as root): prepare /data, render configs, start supervisord.
set -euo pipefail

# Treat empty variables as unset (docker compose `${VAR:-}` passes empty strings,
# which Spark and sparkManager.py would otherwise read as real values).
while IFS='=' read -r name _; do
    if [[ "$name" =~ ^(SIESTA_|SPARK_|MINIO_) && -z "${!name}" ]]; then
        unset "$name"
    fi
done < <(env)

# The Siesta config addresses MinIO as "minio": sparkManager.py rewrites
# localhost/127.0.0.1 S3 endpoints to the Docker bridge IP, which is wrong here.
if ! grep -qE '(^|\s)minio(\s|$)' /etc/hosts; then
    echo "127.0.0.1 minio" >> /etc/hosts \
        || { echo "[siesta] cannot write /etc/hosts" >&2; exit 1; }
fi

mkdir -p /data/minio /data/output /data/spark/tmp /data/spark/work /tmp/matplotlib
chown spark:spark /data /data/minio /data/output /data/spark /data/spark/tmp /data/spark/work /tmp/matplotlib

/usr/local/bin/siesta-render-config

exec "$@"
