#!/usr/bin/env bash
# Stop (and remove) the Switchyard LLM autorouting proxy container started
# by start-switchyard-proxy.sh.
set -euo pipefail

CONTAINER_NAME="${SWITCHYARD_CONTAINER_NAME:-opencode-switchyard-proxy}"

echo "==> Stopping and removing container '$CONTAINER_NAME'"
podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
