#!/bin/bash
# Build (and optionally push) the Siesta all-in-one image from the repository root.
#   siesta/dockerbase/AllInOne/build.sh [--push] [image:tag ...]
# Default tag: balaktsis/siesta:latest. With --push, every tag is pushed to the
# registry (run `docker login` first).
#
# The Spark base is always built locally from siesta/dockerbase/Spark/Spark.dockerfile
# as localhost/siesta-spark:4.0.1 (cached layers make this fast). It is never pulled:
# the Docker Hub image siesta/spark is unrelated and broken.
set -euo pipefail

PUSH=0
TAGS=()
for arg in "$@"; do
    case "$arg" in
        --push) PUSH=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) TAGS+=("$arg") ;;
    esac
done
(( ${#TAGS[@]} )) || TAGS=("balaktsis/siesta:latest")

cd "$(dirname "$0")/../../.."
BASE_IMAGE=localhost/siesta-spark:4.0.1
MINIO_IMAGE="${MINIO_IMAGE:-chainguard/minio:latest}"

echo "Building Spark base image $BASE_IMAGE"
DOCKER_BUILDKIT=1 docker build -f siesta/dockerbase/Spark/Spark.dockerfile \
    -t "$BASE_IMAGE" siesta/dockerbase/Spark

TAG_ARGS=()
for tag in "${TAGS[@]}"; do TAG_ARGS+=(-t "$tag"); done

echo "Building ${TAGS[*]}"
DOCKER_BUILDKIT=1 docker build \
    -f siesta/dockerbase/AllInOne/AllInOne.dockerfile \
    --build-arg BASE_IMAGE="$BASE_IMAGE" \
    --build-arg MINIO_IMAGE="$MINIO_IMAGE" \
    --label org.opencontainers.image.source="https://hub.docker.com/r/balaktsis/siesta" \
    --label org.opencontainers.image.revision="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    "${TAG_ARGS[@]}" .

if (( PUSH )); then
    for tag in "${TAGS[@]}"; do
        echo "Pushing $tag"
        docker push "$tag"
    done
fi
