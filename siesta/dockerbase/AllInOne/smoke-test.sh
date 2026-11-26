#!/bin/bash
# End-to-end check of an all-in-one image: start it, wait for the API, index
# datasets/test.csv, run a detection query, check the namespace listing.
#   siesta/dockerbase/AllInOne/smoke-test.sh [image] [platform]
# Defaults: balaktsis/siesta:latest, the host platform. Under emulation (e.g.
# linux/arm64 on an amd64 host) expect 15-20 minutes instead of about one.
set -euo pipefail

IMAGE="${1:-balaktsis/siesta:latest}"
PLATFORM="${2:-}"
PORT="${SMOKE_PORT:-18000}"
WAIT="${SMOKE_WAIT:-900}"
NAME="siesta-smoke-$$"

cd "$(dirname "$0")/../../.."

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
fail() {
    echo "FAIL: $1" >&2
    docker logs "$NAME" 2>&1 | tail -40 >&2 || true
    exit 1
}
trap cleanup EXIT

docker run -d --name "$NAME" ${PLATFORM:+--platform "$PLATFORM"} \
    -e SIESTA_API_PORT="$PORT" -p "$PORT:$PORT" \
    -e MINIO_ROOT_PASSWORD=smoketest123 \
    -e SPARK_WORKER_CORES=2 -e SPARK_WORKER_MEMORY=4g \
    -e SPARK_DRIVER_MEMORY=2g -e SPARK_EXECUTOR_MEMORY=1g \
    "$IMAGE" >/dev/null
echo "Testing $IMAGE ($(docker exec "$NAME" uname -m))"

start=$(date +%s)
until curl -fsS "http://localhost:$PORT/health" >/dev/null 2>&1; do
    (( $(date +%s) - start < WAIT )) || fail "API not healthy after ${WAIT}s"
    docker inspect -f '{{.State.Running}}' "$NAME" | grep -q true || fail "container exited"
    sleep 3
done
echo "API healthy after $(( $(date +%s) - start ))s"

out=$(curl -sS -X POST "http://localhost:$PORT/indexing/run" \
    -F 'index_config={"log_name":"smoke","storage_namespace":"siesta","field_mappings":{"csv":{"trace_id":"trace_id","activity":"activity","position":"position","start_timestamp":"timestamp"}},"trace_level_fields":["trace_id"],"timestamp_fields":["start_timestamp"]}' \
    -F "log_file=@datasets/test.csv")
echo "index:  $out"
grep -q '"code":200' <<<"$out" || fail "indexing failed"

out=$(curl -sS -X POST "http://localhost:$PORT/querying/detection" -H 'Content-Type: application/json' \
    -d '{"log_name":"smoke","storage_namespace":"siesta","query":{"pattern":"A B"}}')
echo "query:  $out"
grep -q '"trace_id":"trace_1"' <<<"$out" || fail "detection query did not find trace_1"

out=$(curl -sS "http://localhost:$PORT/manager/namespaces")
echo "list:   $out"
grep -q '"smoke"' <<<"$out" || fail "indexed log missing from namespace listing"

! docker logs "$NAME" 2>&1 | grep -q "Traceback" || fail "Python traceback in container logs"
echo "PASS"
