#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
NAMESPACE="${NAMESPACE:-rabyte-data-pre-data}"
SECRET_NAME="${SECRET_NAME:-craft-agents-paiwork-secret}"
K8S_CONTEXT="${K8S_CONTEXT:-test-saas-acs-new}"

: "${IMAGE:?Set IMAGE, for example IMAGE=registry.example/craft-agents-paiwork:1}"
: "${CRAFT_SERVER_TOKEN:?Set CRAFT_SERVER_TOKEN}"

kubectl config use-context "$K8S_CONTEXT"
current_context="$(kubectl config current-context)"
if [ "$current_context" != "$K8S_CONTEXT" ]; then
  echo "Refusing to deploy: current kubectl context is $current_context, expected $K8S_CONTEXT" >&2
  exit 1
fi

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

secret_args=(
  --from-literal=CRAFT_SERVER_TOKEN="$CRAFT_SERVER_TOKEN"
)

if [ -n "${CRAFT_WEBUI_PASSWORD:-}" ]; then
  secret_args+=(--from-literal=CRAFT_WEBUI_PASSWORD="$CRAFT_WEBUI_PASSWORD")
fi
if [ -n "${RABYTE_LLM_API_KEY:-}" ]; then
  secret_args+=(--from-literal=RABYTE_LLM_API_KEY="$RABYTE_LLM_API_KEY")
fi
if [ -n "${SEALOS_LLM_API_KEY:-}" ]; then
  secret_args+=(--from-literal=SEALOS_LLM_API_KEY="$SEALOS_LLM_API_KEY")
fi
if [ -n "${PAI_OBS_API_KEY:-}" ]; then
  secret_args+=(--from-literal=PAI_OBS_API_KEY="$PAI_OBS_API_KEY")
fi
if [ -n "${PAI_OBS_BASE_URL:-}" ]; then
  secret_args+=(--from-literal=PAI_OBS_BASE_URL="$PAI_OBS_BASE_URL")
fi

kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" \
  "${secret_args[@]}" \
  --dry-run=client -o yaml | kubectl apply -f -

"$ROOT/deploy/paiwork/scripts/render-k8s.sh"
kubectl apply -f "$ROOT/deploy/paiwork/k8s/rendered.yaml"
kubectl -n "$NAMESPACE" rollout status deployment/craft-agents-paiwork --timeout="${ROLLOUT_TIMEOUT:-300s}"

echo "Deployment ready."
kubectl -n "$NAMESPACE" get svc craft-agents-paiwork
