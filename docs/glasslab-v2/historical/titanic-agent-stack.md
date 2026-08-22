# Titanic Agent Stack

Status:

- this is now legacy/reference `v1` material
- keep it as a useful example of the earlier end-to-end agent pattern
- do not treat it as the main architectural direction for Glasslab going forward

## Scope

This is the first narrow local agent scaffold for the Glasslab cluster.

Supported v1 workflow only:

- Kaggle Titanic baseline
- request normalization through local Qwen on vLLM
- registry validation
- Kubernetes Job submission
- Job monitoring and artifact collection
- short run summary back to the caller

Out of scope for this repo change:

- frontend work
- arbitrary code execution
- notebook generation
- vector databases
- broad AutoML
- Ray or a multi-agent platform

## Box-And-Arrow Flow

```text
plain-English request
        |
        v
FastAPI agent API
        |
        +--> Qwen via vLLM /v1/chat/completions
        |         |
        |         v
        |    strict JSON planner output
        |
        v
non-LLM validator / registry gate
        |
        v
Kubernetes Job submission
        |
        v
Titanic runner container
        |
        +--> metrics.json
        +--> feature_summary.json
        +--> model_comparison.json
        +--> submission.csv (when test.csv exists)
        +--> result_payload.json
        |
        v
agent status refresh + summary
```

## Repo Fit

The implementation stays inside the existing repo structure:

- `services/agent-api/`: FastAPI control plane, planner, validator, SQLite state store, Kubernetes Job orchestration
- `services/runner/`: fixed Titanic baseline runner image
- `kubeadm/agent-stack/`: deployable manifests for namespace, RBAC, vLLM, agent API, PVCs, and optional MLflow
- `scripts/`: deploy, test, and sample API helpers
- `docs/`: deployment, workflow, and troubleshooting notes

## Why It Still Exists

This stack is still worth keeping around because it shows:

- an earlier complete request-to-artifacts loop
- how the project originally used local model serving plus bounded execution
- what `v2` is evolving out of rather than replacing blindly

The important boundary is:

- preserve it as reference, compatibility, and comparison material
- do not let it drive new platform decisions unless the goal is explicitly about legacy support or regression checking

## Required Input Files

The runner expects the standard Kaggle Titanic dataset layout under the mounted dataset path:

- `train.csv`
- `test.csv`
- optional `gender_submission.csv`
- optional `sample_submission.csv`

`train.csv` must include:

- `Survived`

`test.csv` must include:

- `PassengerId`

## Output Files

The runner writes artifacts under `/mnt/artifacts/<experiment-id>/` inside the cluster.

Expected files:

- `metrics.json`
- `feature_summary.json`
- `model_comparison.json`
- `result_payload.json`
- `submission.csv` when `test.csv` exists and `produce_submission=true`

`submission.csv` is written with exactly these columns:

- `PassengerId`
- `Survived`

## Deployment From The Provisioner

1. Build and publish the images you want the cluster to pull.

```bash
cd /home/glasslab/cluster-config
sudo docker build -t ghcr.io/ccny-glasslab/glasslab-agent-api:0.1.0 services/agent-api
sudo docker build -t ghcr.io/ccny-glasslab/glasslab-titanic-runner:0.1.0 services/runner
sudo docker push ghcr.io/ccny-glasslab/glasslab-agent-api:0.1.0
sudo docker push ghcr.io/ccny-glasslab/glasslab-titanic-runner:0.1.0
```

2. Create the ignored live Secret manifest from the documentation-only
   contract.

```bash
cd /home/glasslab/cluster-config
install -m 600 /dev/null kubeadm/agent-stack/12-agent-secrets.yaml
vi kubeadm/agent-stack/12-agent-secrets.yaml
```

The tracked `12-agent-secrets.example.yaml` file records the required resource
identity and key names, but is deliberately not a Kubernetes manifest and must
not be copied or applied. The live file must be a single `v1` `Secret` named
`glasslab-agent-secrets` in the `glasslab-agents` namespace and must provide a
non-empty, non-placeholder value for every key listed by the contract.

The default ignored path above is used automatically. To use another live
manifest, set its path explicitly:

```bash
export GLASSLAB_VLLM_SECRET_FILE=/secure/path/agent-secrets.local.yaml
```

Both deployment helpers also accept the exact pre-existing cluster Secret when
the keys they consume contain non-empty, non-placeholder values. Standalone
vLLM requires only `VLLM_API_KEY`; full-stack deployment requires all three
keys in the tracked contract. Both helpers validate the local or cluster source
before applying any workload resources.

3. Confirm the dataset PVC plan and populate the Titanic files after the claim binds.

Use the provisioner helper to pull the official Kaggle competition files and sync them onto `node03`:

```bash
cd /home/glasslab/cluster-config
mkdir -p ~/.kaggle && chmod 700 ~/.kaggle
vi ~/.kaggle/kaggle.json
chmod 600 ~/.kaggle/kaggle.json
./scripts/sync-titanic-dataset.sh
```

The helper bootstraps a user-local Kaggle CLI under `/home/glasslab/.local/share/glasslab/kaggle-cli` when needed, uses either passwordless sudo or the reviewed `glasslab-install-titanic-dataset` wrapper on the node, and writes a timestamped backup under `/var/lib/glasslab-agent/datasets/_sync_backup_*` before replacing the live dataset.

If neither path exists on the node, the helper fails with a remediation message instead of falling back to password-fed `sudo`.

You can avoid storing the Kaggle credential file on disk by exporting `KAGGLE_USERNAME` and `KAGGLE_KEY` for the command instead.

4. Deploy model serving.

```bash
/home/glasslab/cluster-config/scripts/deploy-vllm.sh
/home/glasslab/cluster-config/scripts/port-forward-vllm.sh
/home/glasslab/cluster-config/scripts/test-vllm.sh
```

5. Deploy the full agent stack.

```bash
/home/glasslab/cluster-config/scripts/deploy-agent-stack.sh
```

6. Optional: deploy MLflow.

```bash
/home/glasslab/cluster-config/scripts/deploy-agent-stack.sh --with-mlflow
```

7. Port-forward the agent API when you want to use curl from the provisioner.

```bash
kubectl -n glasslab-agents port-forward svc/glasslab-agent-api 8080:8080
```

## API Usage

List supported pipelines:

```bash
curl -sS http://127.0.0.1:8080/pipelines
```

Submit the first supported workflow:

```bash
/home/glasslab/cluster-config/scripts/submit-sample-experiment.sh
```

Fetch a run:

```bash
/home/glasslab/cluster-config/scripts/get-experiment-status.sh <experiment-id>
```

Example request body:

```json
{
  "request_text": "Run a Titanic baseline with logistic regression and random forest, compare them, and prepare a submission file."
}
```

## Where State, Logs, And Artifacts Live

Inside the cluster:

- agent SQLite state: `/var/lib/glasslab-agent/state/agent.db`
- runner artifacts: `/mnt/artifacts/<experiment-id>/`
- vLLM model cache: `/root/.cache/huggingface`

Through the API:

- experiment record: `GET /experiments/{id}`
- control-loop logs: `GET /experiments/{id}/logs`
- artifact references: `GET /experiments/{id}/artifacts`

## Operations And Troubleshooting

- If `POST /experiments` rejects a request, inspect the returned validation errors first. Unknown fields and unsupported models are blocked before Job submission.
- If the planner output is invalid, the API falls back to deterministic parsing for common Titanic requests instead of executing unsafe output.
- If a Job stays pending, check PVC binding, image pull success, and whether the requested GPU node selector matches current worker labels.
- If a Job fails, inspect `GET /experiments/{id}` for the stored error message and `kubectl -n glasslab-agents logs job/<job-name>` for the pod log tail.
- If dataset sync fails before the copy step, confirm the provisioner has valid Kaggle credentials in `~/.kaggle/kaggle.json` or in the `KAGGLE_USERNAME` and `KAGGLE_KEY` environment variables.
- If `submission.csv` is missing, verify that `test.csv` exists in the mounted Titanic dataset path and that `produce_submission` was true in the normalized spec.
- If vLLM is up but planning fails, test `/v1/models` and `/v1/chat/completions` directly before debugging the FastAPI layer.

## Tomorrow Edits You Must Review

These are the lab-specific values to confirm before a real deployment:

- namespace name assumptions in `kubeadm/agent-stack/*.yaml`
- storage class behavior or PVC backing for `glasslab-agent-state`, `glasslab-agent-artifacts`, `vllm-model-cache`, and `titanic-datasets`
- GPU node selector and labels: `glasslab.io/gpu-candidate=true`
- NVIDIA resource key assumption: `nvidia.com/gpu`
- image names and tags for `vllm`, `glasslab-agent-api`, `glasslab-titanic-runner`, and optional `mlflow`
- Hugging Face token handling in `glasslab-agent-secrets`
- vLLM API key handling in `glasslab-agent-secrets`
- Titanic dataset mount path and PVC population plan
- service exposure assumptions: current scripts expect `ClusterIP` plus `kubectl port-forward`
