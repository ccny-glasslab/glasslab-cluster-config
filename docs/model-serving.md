# Model Serving Notes

## Current Model Serving

Glasslab no longer serves models from inside the Kubernetes cluster. Model
serving runs on dedicated inference hosts; see
`docs/glasslab-v2/mac-studio-inference.md` and `scripts/model-serve/README.md`
for the current path.

The former in-cluster vLLM Deployment in the `glasslab-agents` namespace was
part of the legacy Titanic v1 stack. It has been removed from this repository
(issues #157/#158), together with its manifests, planner, and smoke scripts.

## External Primary Inference

The cleanest path is to keep inference hosts outside the Kubernetes worker set
and expose a stable internal or Tailscale-reachable OpenAI-compatible `/v1`
endpoint.

Example OpenClaw export override:

```bash
GLASSLAB_OPENCLAW_PROVIDER_BASE_URL="https://mac-studio.example.internal/v1" \
GLASSLAB_OPENCLAW_DEFAULT_MODEL="your-primary-model-id" \
./scripts/export-openclaw-config.sh
```
