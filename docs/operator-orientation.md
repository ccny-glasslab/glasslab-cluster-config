# Operator Orientation

> **Historical orientation:** this document predates the deployed
> Honeydew/Beaker research orchestrator and still describes OpenClaw as the
> operator layer. For current agent and operator context, start with
> [`../AGENTS.md`](../AGENTS.md) and
> [`research-orchestrator-command-surface.md`](research-orchestrator-command-surface.md).

This document is for one purpose: to let you remember what Glasslab is without reloading the whole repo into your head.

It is not a deployment runbook. It is the map.

## First Principle

There are three different truths in this project:

1. `GitHub repo state`
   What is committed and visible from the laptop.
2. `Documented live state`
   What the docs say was running during the last in-lab validation.
3. `Actual live state`
   What is really running right now on the lab cluster and can only be verified from `.44`.

If you are at home, you only have direct access to the first one and partial memory of the second one.

## What Glasslab Is

Glasslab is a homegrown lab platform built on:

- one PXE/provisioner host
- one Kubernetes control plane
- several worker nodes
- a mix of CPU and GPU capacity
- a set of backend services for running approved experiment workflows
- an operator-facing gateway layer that is supposed to stay narrow and legible

There are really two generations of application stack in this repo.

## The Two Stacks

### v1: Titanic Agent Stack (removed)

The first narrow proof-of-concept lived in `services/agent-api`,
`services/runner`, and `kubeadm/agent-stack`. It accepted a plain-English
Titanic request, normalized it into a strict schema, validated it against an
approved small registry, submitted a Kubernetes Job, and summarized the result.

That stack has been removed from this repository (issues #157/#158); it is no
longer an available or compatibility path.

### v2: Workflow Platform

This is the current architectural direction.

Purpose:

- move from a one-off Titanic flow to a registry-backed workflow platform
- make approved workflows explicit and reviewable in Git
- separate orchestration, evaluation, reporting, and operator interaction
- keep the operator layer narrow instead of burying logic inside prompts

Main pieces:

- `services/workflow-api`
- `services/workflow-registry`
- `services/evaluator`
- `services/reporter`
- `services/openclaw-config`
- `kubeadm/glasslab-v2`
- `docs/glasslab-v2/`

Mental model:

- `v1` proved the loop and has since been removed
- `v2` is the attempt to generalize it cleanly

## The Current Architecture In Plain English

The system is easiest to understand as four layers.

### 1. Infrastructure Layer

This is the base lab machinery.

Includes:

- PXE boot and autoinstall
- node inventory
- Ansible playbooks
- Kubernetes bootstrap and cluster add-ons
- GPU enablement
- package maintenance

Repo areas:

- `ansible/`
- `kubeadm/` for cluster-level manifests
- `live-config/`
- `docs/kubernetes-bootstrap.md`
- `docs/gpu-workers.md`
- `docs/package-maintenance.md`

Question this layer answers:

- how do machines exist, boot, join, stay patched, and expose GPUs?

### 2. Execution Layer

This is where actual workloads run.

Includes:

- Kubernetes Jobs
- runner containers
- configured local model serving
- persistent or semi-persistent state paths
- artifact storage

Repo areas:

- `services/research-workspace-runner`
- `kubeadm/glasslab-v2`
- `docs/model-serving.md`
- `docs/glasslab-v2/storage-and-state.md`

Question this layer answers:

- once a run is approved, where does it execute and where do outputs go?

### 3. Backend Logic Layer

This is the deterministic control plane for workflows.

Includes:

- request validation
- workflow family lookup
- run manifest creation
- run persistence
- evaluation logic
- reporting logic

Repo areas:

- `services/workflow-api`
- `services/workflow-registry`
- `services/evaluator`
- `services/reporter`
- `services/common`

Question this layer answers:

- what work is allowed, and how is it represented?

### 4. Operator / Gateway Layer

This is the human-facing shell.

Includes:

- OpenClaw runtime config
- operator prompts and bindings
- internal API tool surface
- optional chat-channel front door

Repo areas:

- `services/openclaw-config`
- `docs/glasslab-v2/openclaw-gateway.md`
- `docs/glasslab-v2/tool-calling-reliability.md`
- `docs/glasslab-v2/runbooks/deploy-openclaw.md`

Question this layer answers:

- how does a human interact with the system without getting raw cluster access?

## What Each Top-Level Directory Is For

### `ansible/`

Host lifecycle and maintenance.

Think:

- bootstrap nodes
- harden SSH
- enable GPUs
- patch packages

### `docs/`

Human memory.

This is where architecture notes, validation notes, runbooks, and current assumptions live.

If you are confused, start here before reading code.

### `kubeadm/`

Cluster manifests.

Think:

- namespaces
- services
- deployments
- runtime classes
- storage placeholders

This is the Kubernetes expression of the design.

### `live-config/`

Tracked snapshots of provisioner-side machine config.

Think:

- PXE
- dnsmasq
- autoinstall snapshots

This is historical and operationally useful, but easy to over-focus on when you really need the higher-level platform picture.

### `scripts/`

Human-friendly operational wrappers.

These are the usual entrypoint for repeatable tasks.

If something feels like “there should be a one-liner for this,” check here first.

### `services/`

Application logic.

This is where the actual backend services, runners, schemas, and operator config live.

If `kubeadm/` says what runs, `services/` says what the thing is.

## What Is Probably Live vs What Is Merely Designed

Based on committed docs, the safest summary is:

Likely live or at least validated recently:

- the base cluster
- GPU enablement on `node01`, `node02`, and `node04`
- `glasslab-v2` core services:
  - `workflow-api`
  - `Postgres`
  - `MinIO`
  - `NATS`
- OpenClaw internal validation

Not yet “done” in the durable, production-like sense:

- persistent storage
- image distribution independent of `.44`
- secret backup and DR
- stable internal ingress story
- reliable argumented tool-calling
- durable OpenClaw session state

## The Real Constraints

These constraints explain most of the weirdness in the repo.

### Constraint 1: `.44` Is Special

The provisioner is simultaneously:

- Ansible control host
- `kubectl` workstation
- image build point
- source of ignored secrets
- place where some runtime state is materialized

That is why so much operational truth sits there.

The public SSH gateway at `glasslab.org` is a separate machine. It provides
the hop into the lab but does not own these provisioner responsibilities.

### Constraint 2: No Clean Shared Storage Yet

This is why several services still lean on:

- `emptyDir`
- static local paths
- node pinning

The platform is ahead of the infrastructure primitive set.

### Constraint 3: No Clean Image Distribution Yet

Some custom services are still built on `.44` and imported into node-local containerd, especially around `node03`.

That means a manifest may look portable while the actual deployment path is not.

### Constraint 4: Local LLM Tool-Calling Is Not Fully Reliable

The no-arg tools worked.
The tiny argumented tool did not.

That means the safe operator path is intentionally narrower than the aspirational one.

## The Most Important Files To Rebuild Context Quickly

If you want the shortest useful reread path, use this order:

1. `README.md`
2. `docs/operator-orientation.md`
3. `docs/glasslab-v2/overview.md`
4. `docs/glasslab-v2/cluster-primitives-gap-audit.md`
5. `docs/glasslab-v2/openclaw-gateway.md`
6. `docs/glasslab-v2/tool-calling-reliability.md`
7. `docs/gpu-workers.md`

## Suggested GitHub Structure

The repo is now large enough that you should stop treating all work as one undifferentiated stream.

Use GitHub issues as the control surface, not as an afterthought.

Suggested labels:

- `area:infra`
- `area:pxe`
- `area:k8s`
- `area:gpu`
- `area:v2-core`
- `area:workflow-api`
- `area:workflow-registry`
- `area:openclaw`
- `area:vllm`
- `area:storage`
- `area:security`
- `area:docs`
- `state:blocked`
- `state:needs-lab-access`
- `state:needs-live-validation`
- `state:design`
- `state:ready`

Suggested milestones:

- `Stabilize v2 core`
- `Durable storage`
- `OpenClaw operator hardening`
- `Chat channel validation`
- `Registry and workflow growth`
- `Provisioner hardening`

Suggested issue types:

- `Epic`
  A cross-cutting outcome such as “make v2 durable.”
- `Runbook Gap`
  Something works but is not documented well enough to repeat.
- `Infra Gap`
  Missing primitive like storage, registry, ingress, backup.
- `Validation`
  Confirm what is actually live and working from `.44`.
- `Refactor`
  Simplify repo structure, naming, or scripts without changing intent.

## Suggested First Epics

If you want order, these are the right buckets.

### Epic: Make v2 Real, Not Just Validated

Includes:

- durable storage for `Postgres` and `MinIO`
- off-host secret backup
- image distribution not tied to `.44`

### Epic: Make OpenClaw Safe And Repeatable

Includes:

- tighten operator tool surface
- revalidate no-arg lifecycle regularly
- treat argumented tools as experimental until proven
- document exact runtime export and restart flow

### Epic: Clean Up Provisioner Debt

Includes:

- remove remaining password material from tracked PXE/autoinstall config
- reduce password-based helper assumptions
- make the provisioner less special over time

### Epic: Reduce Cognitive Load

Includes:

- top-level repo map
- directory-level READMEs where missing
- issue taxonomy
- decision log for major architecture choices

## What To Ignore For Now

Do not try to hold every detail in memory.

For most conversations, you can safely ignore:

- backup files in `docs/*.bak-*`
- detailed YAML internals until you are changing manifests
- exact autoinstall mechanics unless you are provisioning or hardening nodes
- implementation details of every service at once

The important split is:

- infra
- execution
- backend logic
- operator gateway

If you remember those four layers and the difference between `v1` and `v2`, you already have the useful mental model.
