# Glasslab v2 Overview

Glasslab v2 is the backend-owned research workflow layer for the lab.

It exists to turn investigation state into bounded, reviewable, repeatable
experiments.

## Mental Model

Read the product as:

- investigation-first
- plan-oriented
- deterministic at the control boundary
- bounded at execution time

That means:

- investigations hold the question, hypotheses, immutable execution-graph plans,
  approvals, runs, and claims
- source and dataset integrations produce digest-pinned plan inputs
- preflight decides whether that plan is runnable
- explicit approval freezes the runnable plan before launch
- runs are launched through approved registry workloads
- claims cite exact verified artifact bytes by SHA-256
- comparison and decision drive the next mutation

Research sessions, intake pipelines, and design drafts are compatibility
features. Investigation execution does not depend on them.

## Primary Product Loop

The intended loop is:

`investigation -> hypothesis -> execution graph -> preflight -> approve -> runs -> verified evidence -> claim -> next`

Compatibility aliases and older debug flows still exist, but they are not the
product center.

The first current implementation of this aggregate is documented in
[investigation-api-v1.md](investigation-api-v1.md).

## Command Surface

The canonical interactive path is:

- `OpenCode`
- exo's OpenAI-compatible endpoint
- repo-owned tools
- `workflow-api`

WhatsApp, research ingress, and the command router are optional adapters.

Primary commands:

- `!new`
- `!state`
- `!add`
- `!plan`
- `!check`
- `!run`
- `!compare`
- `!decide`
- `!next`

Legacy aliases may remain:

- `!start`
- `!status`

OpenClaw is not part of the primary command loop.

## Service Roles

### `workflow-api`

The control plane.

Owns:

- investigations and plan-approval snapshots
- immutable execution-graph plans
- run creation
- evidence-backed claims
- execution preflight
- evaluator/report handoff

It also still hosts compatibility sessions, source intake records, design
drafts, and autoresearch transitions while those callers migrate.

### `workflow-registry`

The approved workflow catalog.

Owns:

- execution templates
- allowed inputs
- resource profiles
- runner image references
- expected artifacts
- approval tiers

### `research-ingress`

Retired (issue #159). See `docs/glasslab-v2/historical/README.md`.

### `research-command-router`

Deterministic command dispatch.

Owns:

- command matching
- argument parsing
- pinned-session routing
- one backend-owned action per primary command

### `whatsapp-gateway`

Repo-owned operator shell.

Owns:

- sender transcript persistence
- sender/session pinning
- attachment normalization
- duplicate suppression

### `evaluator` and `reporter`

Deterministic post-run services.

They should stay artifact-grounded and backend-owned.

### Stage agents

Current bounded stage-agent roles:

- `intake-agent`
- `interpretation-agent`
- `assessment-agent`
- `design-agent`

These exist to improve bounded steps inside the product loop.
They are not the primary product surface.

### OpenClaw

Optional and secondary.

If retained, it is for:

- optional chat
- summaries
- read-only or bounded help

It is not the workflow brain.
It is not the command router.

## Data Ownership

### Records

Target system of record:

- `Postgres`

This should own:

- sessions
- stage records
- source metadata
- designs
- runs
- decisions
- campaign state

### Files

Target file/object plane:

- shared filesystem and/or MinIO

This should own:

- source documents
- artifacts
- logs
- reports
- notebooks

Current technical debt:

- session/workflow state still has JSON-backed paths in `workflow-api`
- that is a migration target, not the desired steady state

## Infrastructure Posture

Keep backend services private by default:

- `workflow-api`
- `Postgres`
- `NATS`
- `MinIO API`

Use `ClusterIP` by default.

Use one primary human-facing command surface only.

Current primary human-facing surface:

- repo-owned WhatsApp/control path

## Design Rules

1. Keep command turns deterministic.
2. Keep backend ownership explicit.
3. Keep stage-agent behavior bounded.
4. Keep files and records separated conceptually and operationally.
5. Do not let historical experiments dictate current product language.

## Non-Goals

Glasslab v2 is not primarily:

- a literature-search product
- a general chat shell
- an autonomous multi-agent scientist
- an OpenClaw-centered orchestration stack

It is a bounded research workflow system with a narrow operator loop.
