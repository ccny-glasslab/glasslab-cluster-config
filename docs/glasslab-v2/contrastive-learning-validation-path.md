# Contrastive Learning Validation Path

> **Status note (2026-09-16):** the contrastive-learning runner and the legacy
> `services/runner` sources referenced below were removed from the repository
> during the v1 Titanic stack retirement (issues #157/#158). Retained as a
> historical record of the validation design.

## Overview

Contrastive learning features cannot be validated through GitHub Actions hosted runners
due to runtime dependencies (GPU, CUDA, cluster infrastructure). This document defines
the correct validation path.

## Design Principle

Per `docs/glasslab-v2/github-actions-ci-policy-2026-04.md:7`:

> GitHub-hosted Actions should validate repository state that is safe and meaningful
> to run outside the lab.
> 
> They should not pretend to be the live Glasslab deployment system.

## CI Policy Compliance

### What GitHub Actions Should Do

- ✅ Parse YAML/JSON syntax
- ✅ Compile Python sources
- ✅ Run unit tests that don't require cluster access
- ✅ Validate deterministic backend behavior

### What GitHub Actions Should NOT Do

- ❌ Validate GPU runtime behavior
- ❌ Validate cluster deployment
- ❌ Validate exo/RDMA connectivity
- ❌ Validate workflow-api integration

## Contrastive Learning Validation Path

### 1. Local Development Testing

Run tests locally on your machine (CPU mode, no GPU required):

```bash
cd services/runner
pytest tests/test_contrastive_runner.py -v
```

**Note**: Tests may fail if dependencies are incomplete. This is expected during
active development. Focus on unit tests for individual loss functions and metrics
that don't require full pipeline execution.

### 2. GPU Runner Build (on cluster node .21 or .19)

Docker daemon must be running (e.g., via Colima):

```bash
# On cluster node .21 or .19
cd ~/cluster-config/services/runner
docker build -f Dockerfile.gpu -t glasslab/runner:gpu-v1 .
```

### 3. Deploy Runner to Cluster

Push image to cluster node and deploy via Kubernetes:

```bash
# Push image to registry or load directly
docker save glasslab/runner:gpu-v1 | gzip > runner-gpu-v1.tar.gz

# Load on cluster node and deploy
# (deployment manifests need to be created)
```

### 4. End-to-End Validation

Once runner is deployed, validate via workflow-api:

```bash
# Check runner health
curl -s http://<runner-service>:52415/health

# Submit contrastive learning experiment
curl -X POST http://<workflow-api>:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "experiment_id": "contrastive-cifar100-001",
    "runner_image": "glasslab/runner:gpu-v1",
    "config": "configs/search_spaces/cifar100_contrastive_v0.yaml"
  }'

# Monitor status
curl -s http://<workflow-api>:8000/runs/contrastive-cifar100-001
```

## Metrics to Validate

The following metrics should be computed and validated:

- ✅ `grouped_recall_at_k` - Class-count invariant recall
- ✅ `opis` - Operating Point Inconsistency Score
- ✅ `adjusted_mutual_info` - AMI
- ✅ `adjusted_rand_index` - ARI
- ✅ `normalized_mutual_info` - NMI
- ✅ `silhouette_score` - Cluster quality

## Known Limitations

1. **Missing `search` module**: `contrastive_runner.py` imports from `search.run_spec`
   which doesn't exist. This needs to be fixed by either:
   - Creating the `search` module with `RunSpec` class
   - Removing the import and using mock data

2. **No dataset in S3**: CIFAR-100 datasets referenced in config don't exist yet

3. **No workflow-api endpoint**: `/runs` endpoint may need to be created

## Next Steps

1. Fix `contrastive_runner.py` dependency on `search.run_spec`
2. Create dataset upload script for CIFAR-100
3. Create runner deployment manifests
4. Create workflow-api `/runs` endpoint
5. Run end-to-end validation on cluster

## References

- `docs/glasslab-v2/github-actions-ci-policy-2026-04.md`
- `.github/workflows/ci-python.yml`
- `.github/workflows/manual-docker.yml`
