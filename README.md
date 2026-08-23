# Glasslab Cluster Config

Glasslab is a Beaker/Honeydew research system and the lab platform that runs
it. This repository contains the deterministic research control plane together
with the Kubernetes, GPU, storage, registry, and model-serving configuration it
depends on.

The run fabric is deliberately narrow, but the product-level object is now an
investigation. The goal is not general agent chat. The goal is:

- keep a bounded investigation with explicit hypotheses
- turn it into a reviewable plan
- freeze an approved plan before execution
- launch approved runs
- compare outcomes
- record a decision
- link claims to exact run artifacts
- propose the next bounded mutation

## Repo Layout

- `ansible/`
  - host bootstrap, maintenance, GPU prep
- `kubeadm/`
  - cluster manifests, especially `glasslab-v2`
- `services/`
  - backend services and bounded operators
- `scripts/`
  - deploy, export, sync, smoke-test helpers
- `docs/`
  - architecture notes, runbooks, current-state docs, and historical notes

Useful service buckets:

- current research control plane:
  - `services/research-orchestrator`
  - `services/workflow-api`
  - `services/workflow-registry`
  - `services/evaluator`
  - `services/reporter`
- legacy or compatibility services:
  - `services/whatsapp-gateway`
  - `services/research-ingress`
  - `services/research-command-router`
  - `services/intake-agent`
  - `services/interpretation-agent`
  - `services/assessment-agent`
  - `services/design-agent`

The legacy services are retained for migration and historical reference. They
are not the current research front door.

## Canonical Product Direction

The current bounded Honeydew/Beaker research workflow is documented in
[`docs/research-orchestrator.md`](docs/research-orchestrator.md). It adds a
durable research workflow around isolated OpenCode-backed runtimes and the
existing bounded cluster-execution service. The Titanic stack remains legacy
v1 reference material; see [`docs/glasslab-v2/historical/titanic-agent-stack.md`](docs/glasslab-v2/historical/titanic-agent-stack.md).

The current Discord and operator commands, arbitrary-task intake limits, and
live progress are summarized in
[`docs/research-orchestrator-command-surface.md`](docs/research-orchestrator-command-surface.md).

The active product is the Beaker/Honeydew research workflow on the
`glasslab-v2` platform.

The canonical human research path is:

- Discord
- `research-orchestrator`
- isolated OpenCode-backed Honeydew and Beaker runtimes
- configured local OpenAI-compatible model serving
- `workflow-api`
- bounded Kubernetes Jobs

OpenCode owns agent-level runtime behavior. Glasslab owns the durable workflow,
state transitions, approvals, evaluation contracts, job policy, artifacts, and
provenance. `workflow-api` remains the bounded cluster-execution control plane.
The remaining orchestration boundary is tracked in
[issue #154](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/154);
authoritative invariants must remain deterministic and must not move into
prompts or skills.

## Primary Operator Loop

The intended primary loop is:

```text
question
  -> hypotheses
  -> immutable execution-graph plan
  -> explicit approval
  -> dependency-checked bounded runs
  -> verified evidence bundles
  -> claim and next experiment
```

Discord is the primary human surface. The research orchestrator's database and
append-only event log are authoritative; Discord is their operator-facing
projection. OpenCode remains internal to Honeydew and Beaker.

## Start Here

If you want the current source of truth:

- [AGENTS.md](AGENTS.md) for the concise coding-agent and contributor handoff
- [HANDOFF.md](HANDOFF.md) for the summarized current implementation checkpoint
- [TODO.md](TODO.md) for the prioritized index into the GitHub Issues work queue
- [Beaker/Honeydew delivery board](https://github.com/orgs/ccny-glasslab/projects/3)
  for current Todo, In progress, and Done work
- [Beaker/Honeydew roadmap and ownership](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/155)
  for the active architecture boundary and contributor assignments
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [docs/glasslab-v2/current/README.md](docs/glasslab-v2/current/README.md)
- [docs/glasslab-v2/canonical-stack-2026-04.md](docs/glasslab-v2/canonical-stack-2026-04.md)
- [docs/glasslab-v2/system-map-2026-07.md](docs/glasslab-v2/system-map-2026-07.md)
- [docs/glasslab-v2/learning-task-flow.md](docs/glasslab-v2/learning-task-flow.md)
- [docs/glasslab-v2/investigation-api-v1.md](docs/glasslab-v2/investigation-api-v1.md)
- [docs/glasslab-v2/local-model-command-surface.md](docs/glasslab-v2/local-model-command-surface.md)
- [docs/glasslab-v2/deprecated-api-surface-2026-07.md](docs/glasslab-v2/deprecated-api-surface-2026-07.md)
- [docs/glasslab-v2/ci-policy-2026-07.md](docs/glasslab-v2/ci-policy-2026-07.md)
- [docs/glasslab-v2/command-surface-spec.md](docs/glasslab-v2/command-surface-spec.md)
- [docs/research-orchestrator-command-surface.md](docs/research-orchestrator-command-surface.md)
- [docs/research-orchestrator.md](docs/research-orchestrator.md)
- [docs/glasslab-v2/router-and-backend-contract.md](docs/glasslab-v2/router-and-backend-contract.md)
- [docs/glasslab-v2/deprecation-map-2026-04.md](docs/glasslab-v2/deprecation-map-2026-04.md)

If you are operating the lab:

- `scripts/`
- `docs/glasslab-v2/runbooks/`
- `ansible/playbooks/`

## Contributor Workflow

1. Choose a `Todo` issue from the
   [Beaker/Honeydew board](https://github.com/orgs/ccny-glasslab/projects/3).
2. Read its acceptance criteria, assign yourself, and clarify scope in the
   issue before starting.
3. Create a short-lived branch from `testing` using a prefix such as
   `feat/`, `fix/`, `docs/`, or `chore/`, and open one PR that references the
   issue.
4. Keep state labels current: `state:in-progress`, `state:review`,
   `state:blocked`, or `state:todo`.
5. Merge completed work into `testing` first. Promote `testing` to `main` only
   after the integration state is ready for production.
6. Treat CI as repository validation. Record live cluster checks and rollout
   separately in the PR; CI does not prove deployment.

Branch policy:

- `main` is the production branch and should contain only reviewed,
  releasable commits.
- `testing` is the shared integration branch for combining approved feature,
  fix, documentation, and infrastructure changes.
- Feature branches are disposable. Do not build long-lived personal or agent
  branches; open a PR, merge it, and delete the branch.

Issues define desired outcomes, PRs define canonical implementations, and the
durable run records define actual research state.

## Canonical Environment

Important distinction:

- `glasslab.org` is the public SSH gateway
- the canonical live environment is the provisioner at `192.168.1.44`
- the gateway and provisioner are separate machines
- this laptop checkout is a working client and Git copy
- ignored secrets, runtime bundles, imported images, and some operational truth
  still live only on `.44`

See [docs/access-topology.md](docs/access-topology.md) for canonical host and
SSH names.

So:

- GitHub tells you committed repo state
- docs tell you the last documented live state
- only `.44` can confirm actual live state

## Current Design Rule

Glasslab does not need more competing paths.

The project needs:

- one current Discord/operator surface
- one canonical investigation record
- one deterministic control plane
- one bounded experiment loop
- one honest distinction between current services and legacy material
