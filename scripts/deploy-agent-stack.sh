#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_MLFLOW=false
if [[ "${1:-}" == "--with-mlflow" ]]; then
  WITH_MLFLOW=true
fi

"$ROOT/scripts/deploy-vllm.sh"
kubectl apply -f "$ROOT/kubeadm/agent-stack/01-rbac.yaml"
kubectl apply -f "$ROOT/kubeadm/agent-stack/20-agent-api-config.yaml"
kubectl apply -f "$ROOT/kubeadm/agent-stack/21-agent-api-deployment.yaml"
if [[ "$WITH_MLFLOW" == true ]]; then
  kubectl apply -f "$ROOT/kubeadm/agent-stack/30-mlflow-optional.yaml"
fi
