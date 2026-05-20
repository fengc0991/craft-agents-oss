#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
IMAGE="${IMAGE:-craft-agents-paiwork:local}"

cd "$ROOT"
docker build -f Dockerfile.paiwork -t "$IMAGE" .

echo "Built image: $IMAGE"
