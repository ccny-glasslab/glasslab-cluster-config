# FAISS Integration Test on Kubernetes Cluster

## Summary

This document describes the FAISS integration test for the Glasslab Kubernetes cluster, which verifies that FAISS works correctly without OpenMP deadlock when running on Linux/Kubernetes.

## Problem

FAISS has an OpenMP deadlock issue on macOS when used for metric computation. The platform-specific handling implemented in `src/metrics/metrics.py`:
- **macOS (Darwin)**: FAISS is NOT imported, uses manual distance computation
- **Linux/Kubernetes**: FAISS IS imported and used for efficient metric computation

## Solution

Created a dedicated Kubernetes Job that:
1. Runs the FAISS integration test on a Linux/Kubernetes cluster with GPU support
2. Tests all 4 FAISS usage scenarios that mirror production code in `AdvancedMetrics`
3. Verifies no OpenMP deadlock occurs during index building and search operations

## Files Created

### 1. Job Definition
**Location**: `/Users/glasslab/cluster-config/kubeadm/glasslab-v2/jobs/11-faiss-integration-test.yaml`

Contains:
- Kubernetes Job manifest with GPU resource requirements
- ConfigMap embedding the FAISS test script
- Node selector for NVIDIA GPUs
- TTL for automatic cleanup

### 2. Test Script (Embedded in ConfigMap)
**Path**: `/app/faiss_integration_test.py`

Test coverage:
1. **Test 1**: Basic FAISS index creation and search
2. **Test 2**: Grouped Recall@K simulation (mimics `AdvancedMetrics.grouped_recall_at_k`)
3. **Test 3**: Large-scale test (10000 vectors, stress test for OpenMP)
4. **Test 4**: PyTorch integration verification

### 3. Helper Scripts
**Location**: `/Users/glasslab/cluster-config/kubeadm/glasslab-v2/scripts/`

- `submit-faiss-test.py`: Submit job via kubectl
- `monitor-faiss-test.py`: Monitor job progress and retrieve logs
- `run-faiss-test.sh`: Simple shell wrapper for job submission

## How to Run

### Method 1: Direct kubectl (Recommended)

```bash
kubectl apply -f /Users/glasslab/cluster-config/kubeadm/glasslab-v2/jobs/11-faiss-integration-test.yaml
```

### Method 2: Python Script

```bash
python /Users/glasslab/cluster-config/kubeadm/glasslab-v2/scripts/submit-faiss-test.py
```

## Monitoring the Job

### Check Job Status
```bash
kubectl -n glasslab-v2 get jobs faiss-integration-test
```

### Check Pod Status
```bash
kubectl -n glasslab-v2 get pods -l job-name=faiss-integration-test
```

### View Logs in Real-time
```bash
kubectl -n glasslab-v2 logs -f -l job-name=faiss-integration-test
```

### View Complete Logs
```bash
kubectl -n glasslab-v2 logs -l job-name=faiss-integration-test
```

### Describe Job (Detailed Info)
```bash
kubectl -n glasslab-v2 describe job faiss-integration-test
```

## Expected Results

When the job completes successfully:

```
============================================================
Time 1234567890.123: ALL TESTS PASSED
Time 1234567890.123: FAISS works correctly on Kubernetes without OpenMP deadlock
Time 1234567890.123: Platform: Linux (not Darwin/macOS)
Time 1234567890.123: FAISS version: 1.8.0
============================================================
```

### Test Results Verification
- ✓ Platform detection: Linux (not Darwin)
- ✓ FAISS import: Successful
- ✓ Test 1: Basic index operations completed
- ✓ Test 2: Grouped Recall@K completed
- ✓ Test 3: Large-scale stress test completed
- ✓ Test 4: PyTorch integration works
- ✓ No OpenMP deadlock detected

## Cleanup

After verification, delete the job:

```bash
kubectl -n glasslab-v2 delete job faiss-integration-test
kubectl -n glasslab-v2 delete configmap faiss-integration-test-script
```

## Technical Details

### Platform-Specific FAISS Handling

The test script mimics the production code pattern in `src/metrics/metrics.py`:

```python
import os
# Only import FAISS on non-macOS platforms
if os.uname().sysname != "Darwin":
    import faiss
```

### Test Scenarios

1. **Basic FAISS Operations**
   - Creates IndexFlatL2 index
   - Adds 1000 vectors
   - Searches 100 queries with k=10
   - Verifies results shape

2. **Grouped Recall@K Simulation**
   - Partitions 500 samples into 4 groups
   - Builds index per group
   - Computes recall for each group
   - Mimics `AdvancedMetrics.grouped_recall_at_k`

3. **Large-Scale Stress Test**
   - 10000 vectors × 256 dimensions
   - 1000 queries × k=20
   - Multiple search iterations
   - Stress tests OpenMP parallelization

4. **PyTorch Integration**
   - Converts torch tensors to numpy
   - Uses FAISS with PyTorch tensors
   - Verifies device (CPU) and dtype conversions

### Resource Requirements

- **GPU**: 1 NVIDIA GPU (nvidia.com/gpu: 1)
- **CPU**: 1-2 cores
- **Memory**: 2-4 Gi
- **Node Selector**: `glasslab.io/gpu-vendor: nvidia`

### Image Used

`ghcr.io/ccny-glasslab/glasslab-metric-search:faa46eb`

This image includes:
- PyTorch
- FAISS
- NumPy
- Scikit-learn
- And dependencies

## Integration with Existing Code

This test validates the FAISS usage pattern in:

1. `src/metrics/metrics.py` - FAISS conditional import
2. `src/runners/trainer.py` - FAISS for metric computation
3. `src/runners/experiment.py` - Contrastive learning with FAISS
4. `scripts/run_experiment.py` - FAISS environment setup (macOS only)

## References

- Job Template: `/Users/glasslab/cluster-config/kubeadm/glasslab-v2/jobs/11-faiss-integration-test.yaml`
- Workflow API: `/Users/glasslab/cluster-config/services/workflow-api/app/job_submission.py`
- Run Script: `/Users/glasslab/dml-project/scripts/run_experiment.py`
- Metrics Module: `/Users/glasslab/dml-project/src/metrics/metrics.py`
