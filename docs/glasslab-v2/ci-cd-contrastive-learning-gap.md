# CI/CD Insufficiency Analysis

**Date**: 2026-04-24  
**Issue**: CI/CD pipelines don't validate contrastive learning functionality  
**Status**: Critical Gap Identified

> **Status note (2026-09-16):** the legacy `services/runner` sources and the
> pre-2026-07 CI workflow set referenced below were removed from the repository
> during the v1 Titanic stack retirement (issues #157/#158). This document is
> retained as a historical record of the gap identified on 2026-04-24.

---

## Current CI/CD Coverage

### What Exists (31 Workflows)

| Category | Workflows |
|----------|-----------|
| **Build** | docker-builds.yml |
| **Test** | test-titanic-job.yml, test-gpu-experiments.yml, test-minio.yml, test-exo.yml, test-research-ingress.yml, test-whatsapp-gateway.yml, test-technique-catalog.yml, test-literature-search.yml, test-batch-processor.yml, test-metrics-reporting.yml |
| **Validate** | validate-all.yml, validate-all-pipelines.yml, validate-configs.yml, validate-docker.yml, validate-docs.yml, validate-github-actions.yml, validate-k8s.yml, validate-python.yml, validate-repo.yml, validate-services.yml |
| **Deploy** | gpu-runner.yml |
| **Release** | release.yml, smoke-tests.yml |

### What's Missing

**❌ No validation for contrastive learning pipeline**

---

## The Gap

### What Was Added (Commit c73379c)

1. **contrastive_runner.py** (382 lines)
   - SupervisedContrastiveLoss class
   - TripletLoss class
   - CIFAR-100 seen/unseen data loaders
   - Grouped Recall@K metrics
   - OPIS metrics
   - AMI, ARI, NMI, Silhouette metrics
   - Training loop for contrastive models

2. **cifar100_unseen_classes.yaml** (dataset config)

3. **runner.py updates**
   - Added `contrastive_learning` pipeline
   - Added `train_contrastive_model()` function
   - Added torch imports

4. **requirements.txt updates**
   - pytorch-metric-learning
   - faiss-cpu
   - umap-learn
   - mlxtend
   - torchcp
   - optuna

5. **Config updates**
   - workflow-api Settings passing
   - paper_pipeline.py fixes

### What CI/CD Validates

| Feature | CI/CD Coverage |
|---------|----------------|
| Docker builds | ✓ |
| GPU runner builds | ✓ |
| GPU node selectors | ✓ |
| MinIO configuration | ✓ |
| Exo configuration | ✓ |
| Titan job | ✓ |
| Workflow API tests | ✓ |
| **Contrastive Learning** | **✗ MISSING** |

---

## Why Current CI/CD Is Insufficient

### 1. No Contrastive Learning Tests

**Current test coverage:**
```bash
# Only tests generic GPU experiments
services/runner/tests/test_runner.py
- test_runner_baseline_generates_expected_artifacts
- test_literature_runner_generates_expected_artifacts
- test_gpu_experiment_runner_generates_expected_artifacts

# No contrastive learning tests!
```

**What's missing:**
```python
# Should have:
- test_contrastive_runner_generates_expected_artifacts
- test_supervised_contrastive_loss_computes_correctly
- test_triplet_loss_computes_correctly
- test_cifar100_seen_unseen_splits
- test_grouped_recall_at_k_metrics
- test_opis_metrics
```

### 2. No Contrastive Runner Validation

**Current GPU runner workflow** (`gpu-runner.yml`):
```yaml
# Only validates build
- docker buildx bake --file services/runner/Dockerfile.gpu
```

**What's missing:**
- Test `contrastive_runner.py` imports
- Validate `SupervisedContrastiveLoss` works
- Validate `TripletLoss` works
- Validate metrics functions

### 3. No Dataset Validation

**Current dataset workflow:**
```yaml
# validate-configs.yml only checks YAML syntax
```

**What's missing:**
- Validate CIFAR-100 config has 80/20 seen/unseen split
- Validate augmentation pipeline
- Validate evaluation metrics

### 4. No Integration Tests

**Missing integration test workflow:**
```yaml
# Should validate end-to-end
name: Test Contrastive Learning End-to-End

on:
  push:
    paths:
      - services/runner/app/contrastive_runner.py
      - configs/datasets/cifar100_unseen_classes.yaml
      - .github/workflows/test-contrastive-learning.yml

jobs:
  test-contrastive-learning:
    # Test training loop
    # Test metrics
    # Test CIFAR-100 data loading
```

---

## The Root Cause

### CI/CD Design Assumption

The existing CI/CD assumes:
1. **Docker builds are sufficient** → Validates syntax, not functionality
2. **Generic GPU tests cover all GPU work** → Doesn't test specific pipelines like contrastive learning
3. **No pipeline-specific tests** → Each new pipeline (contrastive, autoresearch, etc.) needs its own workflow

### Why This Is a Problem

**For contrastive learning:**
- Requires specific dependencies (`pytorch-metric-learning`, `faiss-cpu`, etc.)
- Requires specific data loaders (CIFAR-100 seen/unseen splits)
- Requires specific metrics (Grouped Recall@K, OPIS, etc.)
- Requires specific training loop validation

**Current CI/CD:**
```yaml
# gpu-runner.yml
- docker buildx bake --file services/runner/Dockerfile.gpu
```

This only validates:
- ✅ Dockerfile syntax
- ✅ Package installation succeeds
- ❌ No runtime validation
- ❌ No pipeline-specific tests
- ❌ No contrastive learning validation

---

## Required CI/CD Fixes

### Fix 1: Add Contrastive Learning Workflow

```yaml
# .github/workflows/test-contrastive-learning.yml
name: Test Contrastive Learning

on:
  push:
    branches: [main]
    paths:
      - services/runner/app/contrastive_runner.py
      - services/runner/app/runner.py
      - configs/datasets/cifar100_unseen_classes.yaml
      - .github/workflows/test-contrastive-learning.yml
  pull_request:
    branches: [main]
    paths:
      - services/runner/app/contrastive_runner.py
      - services/runner/app/runner.py
      - configs/datasets/cifar100_unseen_classes.yaml
      - .github/workflows/test-contrastive-learning.yml
  workflow_dispatch:

jobs:
  test-contrastive-learning:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install pytest torch torchvision numpy scikit-learn
          pip install -r services/runner/requirements.txt

      - name: Test contrastive_runner imports
        run: |
          python << 'PYEOF'
          # Test imports
          from app.contrastive_runner import (
              SupervisedContrastiveLoss,
              TripletLoss,
              train_contrastive_model,
              build_backbone,
              build_augmentation_pipeline,
              load_cifar100_splits,
              compute_grouped_recall_at_k,
              compute_opis,
              compute_metrics,
          )
          print("✓ All imports successful")
          
          # Test loss functions
          import torch
          features = torch.randn(8, 128)
          labels = torch.randint(0, 10, (8,))
          
          loss_fn = SupervisedContrastiveLoss(temperature=0.1)
          loss = loss_fn(features, labels)
          print(f"✓ SupervisedContrastiveLoss works: {loss.item():.4f}")
          
          loss_fn = TripletLoss(margin=0.3)
          loss = loss_fn(features, labels)
          print(f"✓ TripletLoss works: {loss.item():.4f}")
          PYEOF

      - name: Test metrics
        run: |
          python << 'PYEOF'
          import numpy as np
          from app.contrastive_runner import compute_grouped_recall_at_k, compute_opis
          
          embeddings = np.random.randn(100, 64)
          labels = np.random.randint(0, 10, 100)
          
          rk = compute_grouped_recall_at_k(embeddings, labels, k=10, n_groups=4)
          print(f"✓ Grouped Recall@K works: {rk:.4f}")
          
          opis = compute_opis(embeddings, labels)
          print(f"✓ OPIS works: {opis:.4f}")
          PYEOF

      - name: Test CIFAR-100 data loading
        run: |
          python << 'PYEOF'
          # Test data split logic
          seen_classes = list(range(0, 80))
          unseen_classes = list(range(80, 100))
          
          assert len(seen_classes) == 80, "80 seen classes required"
          assert len(unseen_classes) == 20, "20 unseen classes required"
          assert set(seen_classes).isdisjoint(set(unseen_classes)), "No overlap"
          print("✓ CIFAR-100 split configuration valid")
          
          # Validate config file
          import yaml
          with open('configs/datasets/cifar100_unseen_classes.yaml') as f:
              config = yaml.safe_load(f)
          
          assert 'seen_classes' in config
          assert 'unseen_classes' in config
          print("✓ CIFAR-100 config file valid")
          PYEOF

      - name: Test training loop
        run: |
          python << 'PYEOF'
          import torch
          from app.contrastive_runner import train_contrastive_model, SupervisedContrastiveLoss
          from torch.utils.data import TensorDataset, DataLoader
          
          # Mock data
          features = torch.randn(32, 3, 32, 32)
          labels = torch.randint(0, 10, (32,))
          dataset = TensorDataset(features, labels)
          loader = DataLoader(dataset, batch_size=8)
          
          # Test training loop (short)
          device = torch.device('cpu')
          metrics = train_contrastive_model(
              train_loader=loader,
              val_loader=loader,
              device=device,
              loss_name="contrastive",
              max_epochs=1,
              batch_size=8
          )
          
          assert 'train_loss' in metrics
          assert 'val_loss' in metrics
          print("✓ Training loop works")
          print(f"  Train loss: {metrics['train_loss']:.4f}")
          print(f"  Val loss: {metrics['val_loss']:.4f}")
          PYEOF

      - name: Upload test results
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: contrastive-learning-test-results
          path: |
            contrastive-learning-test-results.xml
            services/runner/tests/results/
          retention-days: 7
```

### Fix 2: Update GPU Runner Workflow

```yaml
# .github/workflows/gpu-runner.yml
name: Build GPU Runner

on:
  push:
    branches: [main]
    paths:
      - services/runner/**/*
      - .github/workflows/gpu-runner.yml
  workflow_dispatch:

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.repository_owner }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository }}/glasslab-gpu-experiment-runner
          tags: |
            type=raw,value={{shortCommit}}
            type=raw,value=latest
            type=raw,value=0.1.7-local

      - name: Build and push Docker image
        uses: docker/build-push-action@v5
        with:
          context: services/runner
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          build-args: |
            GLASSLAB_GIT_SHA=${{ github.sha }}
            GLASSLAB_BUILD_SOURCE=github:${{ github.run_id }}
          file: services/runner/Dockerfile.gpu

      # NEW: Add contrastive learning validation
      - name: Validate contrastive learning runner
        run: |
          docker run --rm ghcr.io/${{ github.repository }}/glasslab-gpu-experiment-runner:${{ steps.meta.outputs.tags }} python -c "
import sys
try:
    from app.contrastive_runner import (
        SupervisedContrastiveLoss,
        TripletLoss,
        train_contrastive_model,
        build_backbone,
        build_augmentation_pipeline,
    )
    print('✓ contrastive_runner imports successful')
    
    import torch
    features = torch.randn(4, 128)
    labels = torch.randint(0, 10, (4,))
    
    loss_fn = SupervisedContrastiveLoss(temperature=0.1)
    loss = loss_fn(features, labels)
    print(f'✓ SupervisedContrastiveLoss works: {loss.item():.4f}')
    
    loss_fn = TripletLoss(margin=0.3)
    loss = loss_fn(features, labels)
    print(f'✓ TripletLoss works: {loss.item():.4f}')
    
    print('✓ Contrastive learning validation passed')
except Exception as e:
    print(f'✗ Validation failed: {e}')
    sys.exit(1)
"

      - name: Check contrastive_runner.py syntax
        run: |
          python -m py_compile services/runner/app/contrastive_runner.py
          echo "✓ contrastive_runner.py syntax valid"
```

### Fix 3: Update validate-all.yml

```yaml
# .github/workflows/validate-all.yml
# Add to existing workflow:
      - name: Check contrastive learning coverage
        run: |
          python << 'PYEOF'
          import os
          
          print("Contrastive learning validation coverage:")
          
          # Check runner file exists
          runner_path = 'services/runner/app/contrastive_runner.py'
          if os.path.exists(runner_path):
              print("  ✓ contrastive_runner.py exists")
              with open(runner_path) as f:
                  content = f.read()
              
              checks = [
                  ('SupervisedContrastiveLoss', 'SupervisedContrastiveLoss'),
                  ('TripletLoss', 'TripletLoss'),
                  ('train_contrastive_model', 'train_contrastive_model'),
                  ('compute_grouped_recall_at_k', 'compute_grouped_recall_at_k'),
                  ('compute_opis', 'compute_opis'),
                  ('compute_metrics', 'compute_metrics'),
              ]
              
              for name, search in checks:
                  if search in content:
                      print(f"  ✓ {name}")
                  else:
                      print(f"  ✗ {name} missing")
              
              # Check dependencies
              deps_path = 'services/runner/requirements.txt'
              with open(deps_path) as f:
                  deps = f.read()
              
              required_deps = [
                  'pytorch-metric-learning',
                  'faiss-cpu',
                  'umap-learn',
                  'mlxtend',
                  'torchcp',
                  'optuna',
              ]
              
              print("\nRequired dependencies:")
              for dep in required_deps:
                  if dep in deps:
                      print(f"  ✓ {dep}")
                  else:
                      print(f"  ✗ {dep} missing")
          else:
              print("  ✗ contrastive_runner.py not found")
          PYEOF
```

---

## Summary

### The Problem

Current CI/CD:
- Validates Docker builds ✓
- Validates generic GPU workflows ✓
- Validates MinIO/Exo configuration ✓
- **Does NOT validate contrastive learning pipeline** ✗

### The Fix

Need 3 new validations:

1. **test-contrastive-learning.yml** - End-to-end contrastive learning tests
2. **Update gpu-runner.yml** - Validate contrastive runner in Docker image
3. **Update validate-all.yml** - Add contrastive learning coverage check

### Impact

Without these fixes:
- Contrastive learning code can break silently
- Docker builds succeed but runtime fails
- CIFAR-100 data loading may fail
- Metrics may produce wrong results
- No protection against regression

With these fixes:
- Every PR validates contrastive learning
- Docker builds include runtime validation
- CI catches regressions before merge

---

## Recommendation

**Priority: HIGH**

This is a **critical gap** in CI/CD coverage. The contrastive learning pipeline is fully functional but has zero automated validation.

**Action Items:**

1. Create `test-contrastive-learning.yml` workflow
2. Update `gpu-runner.yml` to validate contrastive runner
3. Update `validate-all.yml` to check coverage

**Estimated Effort:** 2-3 hours
**Risk:** High (runtime failures without CI validation)
