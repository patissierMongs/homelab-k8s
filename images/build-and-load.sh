#!/bin/bash
set -euo pipefail

# Build local container images and import into K3s containerd
# Usage: ./build-and-load.sh [image-name]
# Without arguments, builds all images

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGES=("rag-worker" "aiops-bridge" "iot-simulator" "topology-exporter")

build_and_load() {
    local name="$1"
    local dir="$SCRIPT_DIR/$name"

    if [[ ! -d "$dir" ]]; then
        echo "SKIP: $dir does not exist"
        return
    fi

    echo "==> Building $name:latest ..."
    docker build -t "$name:latest" "$dir"

    echo "==> Importing $name:latest into K3s ..."
    docker save "$name:latest" | sudo k3s ctr images import -

    echo "==> Done: $name"
}

if [[ $# -gt 0 ]]; then
    build_and_load "$1"
else
    for img in "${IMAGES[@]}"; do
        build_and_load "$img"
    done
fi
