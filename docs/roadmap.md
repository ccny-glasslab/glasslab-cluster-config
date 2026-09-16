# Glasslab Roadmap

This roadmap exists to answer one question:

What should happen next, in what order, and why?

It is intentionally narrower than the full docs set.

## Planning Rules

- Do not treat GitHub repo state as proof of live cluster state.
- Before starting a new operational change, verify the current live state from `.44`.
- Prefer finishing enabling primitives already implied by the design before adding new architectural surface area.
- Reduce special-case dependence on `.44` over time.

## Current Baseline

Based on committed docs, the current baseline is:

- the cluster exists and is usable
- GPUs are enabled on `node01`, `node02`, and `node04`
- the v1 Titanic stack has been removed (issues #157/#158)
- `glasslab-v2` core services have been designed and documented as live-validated
- OpenClaw has been validated as an internal operator path

The biggest gap is not “missing ideas.” It is “missing hardening and durable primitives.”

## Priority Order

### 1. Revalidate Live State

Why first:

- the repo has moved quickly
- the laptop cannot see `.44` from home
- operational decisions should be based on real cluster state, not stale assumptions

Done means:

- current pod, service, and deployment state is checked from `.44`
- actual OpenClaw replica state is known
- actual v2 core health is known
- actual vLLM and GPU allocatable state is known
- any repo-vs-live drift is written down

### 2. Make Glasslab v2 Durable

Why second:

- `glasslab-v2` is the current architecture direction
- one core service still depends on `emptyDir`
- durable state matters more than adding more workflows right now

Done means:

- `Postgres` has explicit durable storage
- `MinIO` has explicit durable storage
- OpenClaw writable state has explicit durable storage
- the chosen local-PV or static PV plan is documented and repeatable
- the repo clearly separates temporary from durable storage assumptions

### 3. Reduce `.44` Specialness

Why third:

- `.44` is still too much of a snowflake
- image builds, imported images, secrets, and runtime exports are too tightly coupled to one machine

Done means:

- custom images can be pulled from a registry or mirror
- node pinning caused only by manual image import is reduced
- ignored secret manifests have an encrypted off-host backup path
- the difference between “repo state” and “`.44` local state” is smaller

### 4. Stabilize OpenClaw As An Operator Layer

Why fourth:

- OpenClaw is useful, but the safe path is still narrower than the intended one
- argumented tool-calling is not yet reliable enough to trust broadly

Done means:

- the no-arg validation lifecycle is rechecked repeatably
- the operator tool surface remains intentionally small
- startup, export, restart, and validation steps are boring and repeatable
- chat-channel use remains explicitly constrained and documented

### 5. Clean Up Provisioner And PXE Debt

Why fifth:

- the cluster works, but provisioning debt remains
- tracked snapshots still contain historical password material

Done means:

- password-dependent helper assumptions are removed
- tracked autoinstall and PXE artifacts no longer carry unnecessary historical password state
- the provisioner is less awkward to reason about and safer to snapshot

### 6. Expand Supported Workflows Carefully

Why last:

- adding more workflows before the platform is durable will increase entropy
- the registry should grow after the backend path is stable

Done means:

- workflow families are added through explicit registry changes
- evaluator and reporter contracts remain deterministic
- expansion does not widen the operator trust boundary by accident

## Current Epics

### Epic: Live State Revalidation

Goal:

- compare actual cluster state on `.44` with documented repo state and record drift

Dependencies:

- lab-network access

### Epic: Durable Storage For v2

Goal:

- replace `emptyDir` dependencies for the important stateful services with explicit storage

Dependencies:

- live revalidation
- storage path decision

### Epic: Image Distribution Cleanup

Goal:

- remove the requirement that custom services be manually built on `.44` and imported into node-local containerd

Dependencies:

- live revalidation
- registry or mirror decision

### Epic: OpenClaw Hardening

Goal:

- keep the operator path useful while not pretending tool-calling is more reliable than it is

Dependencies:

- live revalidation
- stable vLLM path

### Epic: Provisioner Hardening

Goal:

- remove remaining password-era provisioning debt and make the tracked snapshots less risky

Dependencies:

- helper cleanup
- careful live validation on `.44`

## Current Next Five

This is the current recommended execution order given the newer backend-agent direction.

### 1. Add paper interpretation stage records

Issue:

- `#28`

Why first:

- it is the first missing bounded backend stage after intake
- it creates the right place for model-assisted paper understanding
- it advances the system without depending on richer OpenClaw tool behavior

### 2. Add replicability assessment before design approval

Issue:

- `#29`

Why second:

- it separates “what does the paper say” from “can we actually execute this safely”
- it gives a clean proceed / needs-review / reject boundary
- it reduces pressure on design-draft logic to do too much at once

### 3. Generalize intake templates beyond Titanic

Issue:

- `#17`

Why third:

- the current end-to-end path is real, but too benchmark-specific
- one more intake family will prove the backend-agent shape is not just a Titanic demo

### 4. Add design-draft review and update transitions

Issue:

- `#18`

Why fourth:

- once interpretation and replicability exist, the system needs a clean review loop
- this is the natural place to hold unresolved fields and operator corrections

### 5. Pin OpenClaw image to an explicit version or digest

Issue:

- `#19`

Why fifth:

- this is the smallest high-value hardening change around the operator edge
- it reduces rollout ambiguity without distracting from the backend-stage work

## Immediate Follow-On

After the next five, the strongest next candidates are:

- `#22` add a second end-to-end approved workflow family
- `#30` add approval-tier-gated unattended operations
- `#31` add notebook-style final artifact generation
- `#15` document backup and restore for local-PV-backed services
- `#21` reduce `.44` dependence for OpenClaw runtime export and deploy flow

## Work Buckets

### Needs Lab Access

- anything that checks actual live cluster state
- any secret or runtime bundle work
- any image import validation
- any `.44`-local deploy flow
- any port-forward or in-cluster smoke test

### Can Be Done From Home

- repo restructuring
- docs and issue planning
- service code review
- manifest review
- workflow-registry design
- roadmap and runbook improvements

## Deferred On Purpose

These are not necessarily bad ideas. They are just not first-order priorities.

- HA control plane
- public ingress
- broad workflow family expansion
- replacing the current operator path with a more magical one
- adding more chat channels before the first one is operationally boring

## Decision Heuristic

When choosing between tasks, prefer the option that does one of these:

1. reduces dependence on undocumented `.44` state
2. makes the live platform more durable
3. makes the operator path more explicit and less clever
4. lowers cognitive load for the next session
