# Contrastive Learning Deployment Summary

## Overview

This document summarizes the contrastive learning deployment for Glasslab cluster.

## What Was Built

### 1. GPU Runner Image

**Image**: `glasslab/runner:gpu-v1`  
**Size**: 6.08GB  
**Location**: Cluster node (via Docker)

**Components**:
- NVIDIA CUDA 12.4.1
- PyTorch 2.11.0 + TorchVision 0.26.0
- Timm 1.0.11
- PyTorch Metric Learning 2.6.0
- FAISS-CPU 1.8.0
- UMAP 0.5.1
- MLXtend 0.22.0
- TorchCP 1.2.1
- Optuna 4.8.0
- All dependencies for contrastive learning

### 2. Kubernetes Deployment Manifests

**Location**: `kubeadm/glasslab-v2/gpu-runner/`

**Files**:
- `00-all.yaml` - Single-file deployment (all resources)
- `10-deployment.yaml` - Deployment with GPU support
- `20-pvc.yaml` - Model cache PVC (50Gi)
- `30-configmap.yaml` - Configuration parameters
- `40-secret.example.yaml` - Non-deployable database credential schema
- `40-secret.local.yaml` - Ignored operator-created live Secret (never commit)
- `50-service.yaml` - ClusterIP service
- `60-serviceaccount.yaml` - ServiceAccount

**Resources**:
- Request: 2 CPU, 16Gi RAM, 1 GPU
- Limit: 4 CPU, 32Gi RAM, 1 GPU
- Port: 52415
- Runtime class: nvidia

### 3. Helper Scripts

**`scripts/deploy-gpu-runner.sh`**:
```bash
# Deploy GPU runner
./scripts/deploy-gpu-runner.sh --apply

# Check status
./scripts/deploy-gpu-runner.sh --status

# Delete deployment
./scripts/deploy-gpu-runner.sh --delete
```

**`scripts/upload-cifar100.sh`**:
```bash
# After SOPS operator enrollment, provide MinIO values only to the child
# environment; never put either credential on the command line.
./scripts/glasslab-secret exec-env minio-cifar100 -- \
  ./scripts/upload-cifar100.sh
```

The planned `glasslab-secret exec-env` interface is not present on this branch;
until it is enrolled, run the uploader only from an already approved private
environment containing `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY`. The helper
rejects credential arguments and scopes the values to each `mc` child through
`MC_HOST_glasslab`.

**`scripts/run-contrastive-experiment.sh`**:
```bash
# Run contrastive learning experiment
./scripts/run-contrastive-experiment.sh \
  --runner-endpoint http://<runner-service>:52415 \
  --experiment-id my-experiment-001
```

### 4. Documentation

**`docs/glasslab-v2/contrastive-learning-validation-path.md`**:
- CI/CD policy compliance
- Local testing instructions
- GPU runner build process
- End-to-end validation workflow

**`docs/glasslab-v2/contrastive-learning-workflow-api.md`**:
- Workflow API endpoints
- Experiment configuration examples
- Error handling guide
- Example scripts

## CI/CD Compliance

Per `docs/glasslab-v2/github-actions-ci-policy-2026-04.md`:

### ✅ What GitHub Actions Does
- Python syntax validation
- Unit tests (11 tests in `test_contrastive_runner.py`)
- Compile-time checks
- Repository safety checks

### ❌ What GitHub Actions Does NOT Do
- GPU runtime validation (delegated to cluster)
- Cluster deployment validation (delegated to cluster)
- End-to-end integration testing (delegated to cluster)

## Contrastive Learning Features

### Loss Functions
- **Shadow Loss**: O(S) memory complexity for massive batches
- **L2A-NC**: L2A with novel class generation via conditional generator

### Metrics
- **Grouped Recall@K**: Class-count invariant recall computation
- **OPIS**: Operating Point Inconsistency Score
- **AMI**: Adjusted Mutual Information
- **ARI**: Adjusted Rand Index
- **NMI**: Normalized Mutual Information
- **Silhouette**: Cluster quality metric

### Data Augmentation
- Random Resized Crop (32x32, scale 0.2-1.0)
- Color Jitter (brightness/contrast/saturation/hue)
- Random Horizontal Flip

### Training Configuration
- ResNet-101 or ViT-large backbones
- Embedding dimension: 512
- Projection dimension: 128
- Temperature: 0.07
- Optimizer: Adam
- Scheduler: Cosine

## CIFAR-100 Split Strategy

- **Total classes**: 100
- **Seen classes**: 80
- **Unseen classes**: 20
- **Random seeds**: 3, 5 (for statistical validation)
- **Augmentation**: Random resized crop + color jitter + horizontal flip

## Deployment Steps

### 1. Build GPU Runner Image (on cluster node .21 or .19)

```bash
cd ~/cluster-config/services/runner
docker build -f Dockerfile.gpu -t glasslab/runner:gpu-v1 .
```

### 2. Deploy to Cluster

```bash
cd ~/cluster-config
export GLASSLAB_GPU_RUNNER_SECRET_FILE="$PWD/kubeadm/glasslab-v2/gpu-runner/40-secret.local.yaml"
./scripts/deploy-gpu-runner.sh --apply
```

Create the ignored local file from `40-secret.example.yaml` through the
approved secret workflow. Never apply `00-all.yaml` directly: the deployment
helper validates the local or existing live PostgreSQL DSN without printing it
and fails before applying workload resources when the Secret contract is not
satisfied.

### 3. Upload CIFAR-100 Dataset

```bash
./scripts/glasslab-secret exec-env minio-cifar100 -- \
  ./scripts/upload-cifar100.sh
```

### 4. Run Experiment

```bash
./scripts/run-contrastive-experiment.sh \
  --runner-endpoint http://glasslab-gpu-runner.glasslab-v2.svc.cluster.local:52415 \
  --experiment-id cifar100-contrastive-001
```

## Validation Checklist

- [x] GPU runner image built (`glasslab/runner:gpu-v1`)
- [x] Kubernetes manifests created
- [x] ConfigMap and non-deployable Secret schema defined
- [x] PVC for model cache created
- [x] ServiceAccount created
- [x] Deployment manifest with GPU support
- [x] Service endpoint created
- [x] Helper scripts created
- [x] Documentation created
- [ ] Dataset uploaded to MinIO
- [ ] Runner pod running
- [ ] Experiment submitted and completed
- [ ] Metrics computed and validated

## Next Steps

1. Push GPU runner image to registry or load on cluster node
2. Create the ignored local Secret through the approved workflow and deploy
   with `scripts/deploy-gpu-runner.sh`
3. Verify runner pod is running
4. Upload CIFAR-100 dataset to MinIO
5. Submit contrastive learning experiment
6. Monitor via workflow-api `/runs` endpoint
7. Validate metrics (Grouped Recall@K, OPIS, AMI, ARI, NMI, Silhouette)

## References

- `docs/glasslab-v2/github-actions-ci-policy-2026-04.md`
- `docs/glasslab-v2/contrastive-learning-validation-path.md`
- `docs/glasslab-v2/contrastive-learning-workflow-api.md`
- `configs/datasets/cifar100_unseen_classes.yaml`
- `configs/search_spaces/cifar100_contrastive_v0.yaml`
