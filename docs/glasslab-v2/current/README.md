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

Exo is retired (verified 2026-11-12). The active agent provider is
`opencode-go` with `deepseek-v4.1-flash`. `.17` runs the Ollama embedding
service for ROBOJAN; `.18` still runs a per-host `mlx_lm.server` as a rollback
path. See `HANDOFF.md` ("Model Serving") and `scripts/model-serve/README.md`.

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
