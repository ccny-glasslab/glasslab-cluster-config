# Model Serving Notes

## Current Model Serving

Glasslab serves models from dedicated macOS inference hosts outside the
Kubernetes cluster. Each Mac runs one full model locally; `exo` is retired:

| Host | Model | mlx port | guard port |
|---|---|---|---|
| `.17` | `mlx-community/Qwen3-Coder-Next-4bit` | 52416 | 52417 |
| `.18` | `mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit` | 52416 | 52417 |

`mlx_lm.server` runs behind `model_guard.py`, a serializing proxy (single
queue, 64 MB body cap, 503+retry when busy); the orchestrator and the
rehearsal harness talk only to the guard. Weights live in the durable Hugging
Face cache under `/Users/glasslab/.cache/huggingface`; launchd daemons keep
the servers up across reboots. Install, (re)download, offline mode, and
verification steps live in `scripts/model-serve/README.md`.

The legacy in-cluster vLLM Deployment in the `glasslab-agents` namespace was
part of the Titanic v1 stack. It has been removed from this repository (issues
#157/#158), together with its manifests, planner, and smoke scripts.

## External Primary Inference

Inference hosts stay outside the Kubernetes worker set and expose a stable
internal or Tailscale-reachable OpenAI-compatible `/v1` endpoint. Point
OpenClaw at that endpoint during runtime export. The external-inference
decision note is `docs/glasslab-v2/mac-studio-inference.md`.

Example OpenClaw export override:

```bash
GLASSLAB_OPENCLAW_PROVIDER_BASE_URL="https://mac-studio.example.internal/v1" \
GLASSLAB_OPENCLAW_DEFAULT_MODEL="your-primary-model-id" \
./scripts/export-openclaw-config.sh
```

## Optional MLflow

Optional MLflow manifests belong under `kubeadm/glasslab-v2/mlflow/` if that
history layer is enabled; no manifests are tracked there yet. The old
`kubeadm/agent-stack/30-mlflow-optional.yaml` deployment was removed with the
v1 stack.
