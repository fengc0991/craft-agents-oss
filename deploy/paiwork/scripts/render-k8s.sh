#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
TEMPLATE="$ROOT/deploy/paiwork/k8s/deployment.yaml"
OUT="${OUT:-$ROOT/deploy/paiwork/k8s/rendered.yaml}"

: "${IMAGE:?Set IMAGE, for example IMAGE=registry.example/craft-agents-paiwork:1}"

export NAMESPACE="${NAMESPACE:-rabyte-data-pre-data}"
export IMAGE
export IMAGE_PULL_POLICY="${IMAGE_PULL_POLICY:-IfNotPresent}"
export PVC_SIZE="${PVC_SIZE:-20Gi}"
export NODE_PORT="${NODE_PORT:-30101}"
export CPU_REQUEST="${CPU_REQUEST:-500m}"
export MEMORY_REQUEST="${MEMORY_REQUEST:-1Gi}"
export CPU_LIMIT="${CPU_LIMIT:-4}"
export MEMORY_LIMIT="${MEMORY_LIMIT:-8Gi}"

python3 - "$TEMPLATE" "$OUT" <<'PY'
import os
import string
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as f:
    content = string.Template(f.read()).substitute(os.environ)
with open(dst, "w", encoding="utf-8") as f:
    f.write(content)
PY

echo "Rendered: $OUT"
