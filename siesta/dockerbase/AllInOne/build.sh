#!/bin/bash
# Build (and optionally push) the Siesta all-in-one image from any directory.
#   siesta/dockerbase/AllInOne/build.sh [--push] [image:tag ...]
# Default tag: balaktsis/siesta:latest. Default platforms: linux/amd64,linux/arm64
# (override with PLATFORMS=linux/amd64 for a quick single-architecture build).
#
# Without --push the image is loaded into the local Docker image store; loading a
# multi-architecture image needs the containerd image store (Docker Desktop default,
# or "features": {"containerd-snapshotter": true} in daemon.json). With --push every
# tag is pushed to the registry (run `docker login` first).
#
# Building a platform other than the host's needs QEMU emulation, once per boot:
#   docker run --privileged --rm tonistiigi/binfmt --install arm64   # or amd64
set -euo pipefail

OUTPUT=--load
TAGS=()
for arg in "$@"; do
    case "$arg" in
        --push) OUTPUT=--push ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) TAGS+=("$arg") ;;
    esac
done
(( ${#TAGS[@]} )) || TAGS=("balaktsis/siesta:latest")

cd "$(dirname "$0")/../../.."

TAGS="$(IFS=,; echo "${TAGS[*]}")" \
REVISION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
    docker buildx bake -f siesta/dockerbase/AllInOne/docker-bake.hcl "$OUTPUT"
