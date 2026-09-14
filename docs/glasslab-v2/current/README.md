# Current Docs Index

Use this index when you want the current product and architecture story, not the
historical path that got the repo here.

## Read First

- `../canonical-stack-2026-04.md`
- `../system-map-2026-07.md`
- `../run-fabric-design-2026-04.md`
- `../learning-task-flow.md`
- `../investigation-api-v1.md`
- `../runtime-replay-report.md`
- `../deprecated-api-surface-2026-07.md`
- `../ci-policy-2026-07.md`
- `../command-surface-spec.md`
- `../router-and-backend-contract.md`
- `../deprecation-map-2026-04.md`

## Current Product / Architecture

- `../overview.md`
- `../system-map-2026-07.md`
- `../learning-task-flow.md`
- `../investigation-api-v1.md`
- `../run-fabric-design-2026-04.md`
- `../bounded-experiment-runner-priority.md`
- `../runner-first-technique-knowledge-plan.md`
- `../technique-catalog.md`

## Model Serving

Exo is retired (verified 2026-09-09). Each Mac serves one full model behind a
serializing guard at `:52417` (`.17` = `mlx-community/Qwen3-Coder-Next-4bit`,
`.18` = `mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit`). See `HANDOFF.md`
("Model Serving") and `scripts/model-serve/README.md`.

`../local-model-command-surface.md` predates the retirement: it still documents
the exo OpenAI-compatible endpoint and the `glasslab-exo17` SSH target. Treat its
endpoint details as exo-era; the OpenCode launcher usage remains useful.

## Current Operational / Data Notes

- `../state-and-storage-map-2026-03-27.md`
- `../cluster-primitives-gap-audit.md`
- `../image-distribution.md`
- `../internal-service-exposure.md`
- `../provisioner-dependence-inventory.md`

## Cleanup / Simplification

- `../product-cleanup-2026-04.md`

If a document conflicts with the files above, prefer the files above.
