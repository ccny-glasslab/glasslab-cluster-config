# GPU Runner Deployment

This directory contains Kubernetes manifests for deploying the GPU runner with contrastive learning support.

## Prerequisites

- GPU node (node02) with NVIDIA GPU and NVIDIA runtime enabled
- PVC `glasslab-shared-artifacts` available
- MinIO and PostgreSQL accessible
- Image `glasslab/runner:gpu-v1` built and accessible on cluster nodes

## Deployment

The aggregate manifest intentionally contains no `Secret`. Either create the
named `glasslab-v2-runner` Secret in the `glasslab-v2` namespace before
deployment, or create an ignored local manifest ending in `.local.yaml` from
the documentation-only schema in `40-secret.example.yaml`.

```bash
export GLASSLAB_GPU_RUNNER_SECRET_FILE=/secure/path/gpu-runner-secret.local.yaml
./scripts/deploy-gpu-runner.sh --apply
```

If the named Secret already exists in the cluster, omit the environment
variable:

```bash
./scripts/deploy-gpu-runner.sh --apply
```

The deploy script fails before applying the workload when neither source is
available. It also rejects a local manifest unless it is exactly a `v1` Secret
named `glasslab-v2-runner` in namespace `glasslab-v2` with a nonempty
`GLASSLAB_RUNNER_STORE_POSTGRES_DSN` entry in `data` or `stringData`. After a
local apply, the script confirms that exact live Secret/key before applying the
workload. Local Secret manifests must remain untracked.

## Configuration

### ConfigMap Parameters

- `GLASSLAB_RUNNER_MODEL_ID`: Model identifier (default: mlx-community/Qwen3-Coder-Next-4bit)
- `GLASSLAB_RUNNER_CONTEXT_LENGTH`: Context length (default: 262144)
- `GLASSLAB_RUNNER_MAX_TOKENS`: Max tokens (default: 2048)
- `GLASSLAB_RUNNER_TEMPERATURE`: Temperature (default: 0.7)
- `GLASSLAB_RUNNER_TOP_P`: Top-p sampling (default: 0.9)
- `GLASSLAB_RUNNER_SHARDING`: Sharding strategy (default: Pipeline)
- `GLASSLAB_RUNNER_RUNTIME`: Runtime backend (default: MlxJaccl)
- `GLASSLAB_RUNNER_DATASET`: Dataset name (default: cifar100)
- `GLASSLAB_RUNNER_SEEN_CLASSES`: Seen classes count (default: 80)
- `GLASSLAB_RUNNER_UNSEEN_CLASSES`: Unseen classes count (default: 20)
- `GLASSLAB_RUNNER_SEEDS`: Random seeds (default: 3,5)
- `GLASSLAB_RUNNER_METRICS`: Metrics to compute (default: grouped_recall_at_k,opis,ami,ari,nmi,silhouette)
- `GLASSLAB_RUNNER_AUGMENTATION`: Data augmentation (default: RandomResizedCrop+ColorJitter+RandomHorizontalFlip)

## Validation

Check pod status:

```bash
kubectl get pods -n glasslab-v2 -l app.kubernetes.io/name=glasslab-gpu-runner
```

Check logs:

```bash
kubectl logs -n glasslab-v2 -l app.kubernetes.io/name=glasslab-gpu-runner -f
```

Check service:

```bash
kubectl get svc -n glasslab-v2 glasslab-gpu-runner
```

Test endpoint:

```bash
kubectl port-forward -n glasslab-v2 svc/glasslab-gpu-runner 52415:52415
curl http://localhost:52415/healthz
```

## Notes

- Uses runtimeClassName: nvidia for GPU support
- Request: 2 CPU, 16Gi RAM, 1 GPU
- Limit: 4 CPU, 32Gi RAM, 1 GPU
- Model cache PVC: 50Gi
- Port: 52415
