# V2 Services And Contracts

Glasslab v2 treats workflow execution as a contract-driven pipeline.

## Service placement

- `workflow-api` owns request intake, registry lookup, validation, run manifest creation, persistence, and the job submission boundary.
- `evaluator` reads multiple completed run bundles and produces deterministic `comparison.json` and `summary.md` outputs.
- `reporter` converts manifests, metrics, and optional evaluator output into a stable Markdown memo for operators.
- Kubernetes Jobs remain the bounded execution layer.
- `research-command-router` remains the deterministic command surface in front of these components. Its upstream `whatsapp-gateway` and `research-ingress` adapters have been retired; see `docs/glasslab-v2/historical/README.md`.

## Canonical artifact contract

Every workflow must emit these artifacts:

- `run_manifest.json`
- `config.json`
- `metrics.json`
- `artifacts_index.json`
- `report.md`
- `status.json`
- `logs/`

Optional artifacts are declared per workflow definition under `expected_artifacts.optional` in the workflow registry.

The first richer optional presentation artifact is:

- `analysis_notebook.ipynb`: deterministic notebook scaffold for interactive review and simple visualizations where appropriate

## Evaluator inputs

The evaluator expects each run bundle to contain at least:

- `run_manifest.json`
- `metrics.json`
- `status.json`

It compares workflow family, model choice, primary metrics, and runtime metadata, then emits deterministic comparison output.

## Reporter inputs

The reporter expects:

- one `run_manifest.json`
- one `metrics.json`
- optional evaluator output such as `comparison.json`

The first reporter version stays deterministic and does not require direct LLM access.
