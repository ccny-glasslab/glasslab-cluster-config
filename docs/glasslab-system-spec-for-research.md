# Glasslab System Spec For Deep Research

This file is a top-down system spec for external analysis.

It is written for a deep research prompt that needs enough context to reason about architecture, bottlenecks, tradeoffs, and next steps without loading the whole repo.

## 1. System Identity

Glasslab is a self-hosted Kubernetes lab for controlled, repeatable experiment workflows.

It is not a generic autonomous agent shell.

Its intended end state is:

`paper or research idea -> bounded validation experiment -> artifacts -> evaluation -> report`

The project is designed to keep workflow rules explicit and reviewable in Git, while using an LLM for interpretation, summarization, intake, and narrow operator interaction.

## 2. Core Problem The System Is Solving

The system is trying to reduce friction between:

- a research paper, research note set, or plain-language experimental idea
- an approved, bounded validation run on local infrastructure
- a deterministic output bundle that can be compared and reviewed

The design goal is not maximum raw autonomy.

The design goal is controlled autonomy:

- the operator should be able to start a workflow from a high-level request
- the backend should turn that into approved, explicit execution
- the system should generate artifacts and summaries automatically
- the system should fail safely when the model is weak or ambiguous

## 3. Reality Boundaries

There are three distinct truths in this project:

1. `repo state`
   What is committed to GitHub.

2. `documented live state`
   What the repo docs say was verified during the last in-lab validation.

3. `actual live state`
   What is currently running in the lab and can only be verified from the provisioner host.

Important operational fact:

- the canonical live environment is the provisioner at `192.168.1.44`
- the laptop checkout is not the source of truth for live runtime state

Important consequence:

- some operational truth exists only on `.44`, including ignored secrets, exported runtime bundles, imported images, and any unpushed changes

## 4. Current Physical / Cluster Topology

### Public Gateway

- `glasslab.org`
- public SSH entry point
- separate from the internal provisioner

### Provisioner

- `192.168.1.44`
- PXE host
- Ansible control host
- `kubectl` admin workstation
- canonical repo checkout location in the live lab

### Kubernetes Control Plane

- `cp01` at `192.168.1.49`

### Kubernetes Workers

- `node01` at `192.168.1.48`
- `node02` at `192.168.1.11`
- `node03` at `192.168.1.50`
- `node04` at `192.168.1.51`
- `node05` at `192.168.1.47`

### GPU Summary

Documented active GPU workers:

- `node01`: Quadro P4000
- `node02`: RTX A4000
- `node04`: GTX 1060 6GB

Documented practical inference target:

- `node02` is the strongest current single GPU inference node

## 5. Base Infrastructure Stack

### Provisioning

- PXE / autoinstall
- `dnsmasq` in ProxyDHCP mode
- `tftpd-hpa`
- `nginx` for PXE and cloud-init delivery

### Cluster

- Kubernetes `v1.35.2`
- Calico CNI
- single control plane
- multiple CPU/GPU workers

### Host Automation

- Ansible for bootstrap, maintenance, SSH hardening, and GPU enablement

### Packaging / Updates

- unattended security updates on nodes
- manual package maintenance via Ansible for broader changes

## 6. The Two Application Stacks

Glasslab's active stack is the v2 workflow platform.

### v1: Legacy / Reference Stack (removed)

The v1 stack proved that plain-language request -> validation -> Kubernetes job
-> artifacts could work end-to-end (FastAPI agent API, local vLLM model
service, fixed Titanic runner, SQLite state). It has been removed from this
repository (issues #157/#158) and is no longer an available path.

### v2: Current Direction

Purpose:

- turn the narrow v1 vertical slice into an explicit workflow platform
- keep allowed workflows reviewable in Git
- separate operator UX from execution control logic

Role today:

- primary platform direction
- current focus of architecture and infrastructure work

## 7. Architectural Layers

The system is best understood as four layers.

### Layer 1: Infrastructure

Responsibilities:

- machines exist and boot
- nodes join cluster
- SSH is hardened
- GPUs are enabled
- packages are maintained

Representative repo areas:

- `ansible/`
- `live-config/`
- cluster-level `kubeadm/`

### Layer 2: Execution

Responsibilities:

- actual workloads run
- runner images exist
- model serving is provided outside the cluster
- artifacts are stored
- state is mounted

Representative repo areas:

- `kubeadm/glasslab-v2`
- `services/research-workspace-runner`
- `docs/model-serving.md`
- `docs/glasslab-v2/storage-and-state.md`

### Layer 3: Backend Logic

Responsibilities:

- validate requests
- map requests to approved workflow families
- build canonical run manifests
- persist run state
- evaluate outputs
- report deterministically

Representative repo areas:

- `services/workflow-api`
- `services/workflow-registry`
- `services/evaluator`
- `services/reporter`

### Layer 4: Operator / Gateway

Responsibilities:

- provide a human-facing front door
- preserve operator session context
- call approved backend tools
- expose narrow chat or CLI entrypoints

Representative repo areas:

- `services/openclaw-config`
- `docs/glasslab-v2/openclaw-gateway.md`
- `docs/glasslab-v2/tool-calling-reliability.md`

## 8. Current v2 Component Model

### `workflow-api`

Role:

- orchestration backend
- request intake
- workflow family lookup
- validation
- canonical manifest creation
- job-submission boundary

### `workflow-registry`

Role:

- explicit catalog of approved workflow families

It defines:

- allowed inputs
- allowed models / methods
- resource profiles
- runner image assumptions
- expected artifacts
- approval tiers

### `evaluator`

Role:

- deterministic comparison of multiple completed runs

Expected outputs include:

- `comparison.json`
- `summary.md`

### `reporter`

Role:

- deterministic operator-facing reporting from manifests and metrics

### OpenClaw

Role:

- operator shell and session/gateway layer
- not the workflow brain

Important rule:

- OpenClaw should not contain the only copy of workflow logic

### Kubernetes Jobs

Role:

- bounded execution substrate for approved runs

## 9. Canonical v2 Flow

The intended v2 control flow is:

`request -> workflow family lookup -> registry-backed validation -> canonical run_manifest -> Kubernetes Job submission -> artifacts -> evaluation -> report`

This implies:

- approval logic belongs in backend definitions and validators
- not in ad hoc model output

## 10. Canonical Artifact Contract

Every workflow should emit at least:

- `run_manifest.json`
- `config.json`
- `metrics.json`
- `artifacts_index.json`
- `report.md`
- `status.json`
- `logs/`

This contract exists so evaluator and reporter remain deterministic and reusable.

## 11. Current Supporting Platform Services

### Postgres

Role:

- durable run state
- workflow metadata
- backend persistence

Current state:

- on retained local PV/PVC on `node01`

### MinIO

Role:

- object storage for artifacts, reports, and optional dataset snapshots

Current state:

- on retained local PV/PVC on `node01`

### NATS

Role:

- internal event bus
- status updates
- decoupled background work

Current state:

- still on `emptyDir`
- current remaining core durability gap

### Model Serving

Role:

- local LLM inference layer for the operator and research agents
- OpenAI-compatible `/v1` endpoints on dedicated inference hosts

Current state:

- model serving runs outside the cluster; see `docs/model-serving.md`
- the legacy in-cluster vLLM deployment was part of the v1 stack and has been
  removed from this repository (issues #157/#158)

Important architectural note:

- model serving is infrastructure, not orchestration logic

## 12. Current OpenClaw Usage

Glasslab is intentionally using a narrow subset of OpenClaw.

Currently used:

- operator agent
- repo-managed prompts and workspaces
- custom plugin tools into `workflow-api`
- WhatsApp ingress path
- local vLLM provider

Intentionally not leaned on yet:

- broad exec / shell tooling
- broad browser automation
- general node-host mutation
- broad cron automation
- wide multi-tool surfaces

Reason:

- the current local model/runtime path is not yet reliable enough for broad structured tool usage

## 13. AI / Tooling Situation

### What Works Reliably

The current reliable operator path is mostly no-arg tools.

Known good:

- `workflow_api_create_validation_run`
- `workflow_api_get_last_validation_run`

Why they work:

- the backend supplies the important payload structure
- the model is only selecting and summarizing
- the model is not being asked to reliably synthesize structured arguments

### What Does Not Work Reliably Yet

Tiny argumented tools are still unreliable on the current local model/runtime path.

Concrete tested example:

- `workflow_api_get_family_by_id`

Failure pattern:

- model selects tool but leaves required `workflow_id` empty
- or later avoids making a fresh tool call entirely

### Key Live Finding About RPC

There is a reachable lower-level OpenClaw gateway RPC method called `agent`.

However:

- the reachable gateway `agent` RPC rejects `tool_choice`
- `chat.send` also rejects `tool_choice`

Important consequence:

- internal OpenClaw code contains `tool_choice` support
- but the reachable request schemas used by Glasslab do not expose it

This means:

- the current limitation is not just missing CLI flags
- testing forced-tool or pinned-tool behavior likely requires an OpenClaw patch, custom build, or upstream change

## 14. Multi-Tool Situation

The project would eventually like reliable multi-tool operator behavior.

Current blocker:

- the stack does not yet have a reachable control surface that allows clean pinned or required tool choice for the operator path

Implication:

- broad multi-tool expansion today would likely add ambiguity faster than capability

Current recommended posture:

- keep production operator flows narrow and mostly no-arg
- expand only after single-tool control and measurement improve

## 15. Why No-Arg Tools Are Central Right Now

No-arg tools are not a hack.

They are the current safe autonomy rail.

They allow:

- plain-language intake
- bounded operator action selection
- backend-owned payload templates
- deterministic validation
- safe execution

This means the system can still be meaningfully autonomous even if the model is weak at structured argument generation.

## 16. Target Product Shape

The target system experience is:

1. operator presents paper, notes, or research goal
2. system starts bounded intake
3. system maps intake to approved workflow family
4. system creates a bounded design draft
5. system creates and submits a validation run
6. system evaluates results
7. system reports in plain language with explicit artifacts

Current recommended next no-arg ladder:

1. `workflow_api_start_paper_intake`
2. `workflow_api_get_last_intake`
3. `workflow_api_create_design_draft`
4. `workflow_api_create_validation_run_from_last_design`

This would move the operator flow much closer to the real desired product without depending on argumented tools.

## 17. Current Storage Posture

The cluster currently has:

- no default `StorageClass`
- explicit retained local PV/PVC for:
  - Postgres
  - MinIO
  - OpenClaw writable state

Current implication:

- important state is more durable than before
- but still node-local rather than shared/failover-grade

Expected future shared storage:

- network storage / NFS is expected later, likely from `192.168.1.207`

Current view:

- local PVs are an appropriate interim step
- NFS/shared storage should be added deliberately, not as a reflex

## 18. Current Security / Safety Posture

Broad principle:

- OpenClaw should remain narrow and policy-constrained

Default denied categories should include:

- arbitrary shell execution
- mutating `kubectl`
- broad filesystem mutation
- broad outbound HTTP
- uncontrolled cluster mutation

This project is trying to avoid “bag of prompts with hidden powers” failure modes.

## 19. Current Live-State Highlights

As of the 2026-03-19 documented live validation:

- all cluster nodes were `Ready`
- OpenClaw was live at `1` replica
- WhatsApp ingress path was active
- `workflow-api`, Postgres, MinIO, and NATS were live
- OpenClaw writable state was restored onto durable local PVC storage
- the safe no-arg OpenClaw tool path worked
- the tiny argumented tool path still failed

## 20. Key Constraints

### Model Constraint

Current local model path is not yet trustworthy for general structured tool arguments.

### Control-Surface Constraint

The reachable OpenClaw gateway path does not currently expose `tool_choice` for the operator flow.

### Hardware Constraint

Current GPUs may support moderate improvement over the current 4B path, but probably not a dramatic jump to a much stronger local model tier without meaningful tradeoffs.

### Operational Constraint

The provisioner at `.44` is still too special:

- secrets
- runtime bundles
- imported images
- some live-only operational truth

## 21. Non-Goals

The system should not become:

- a generic unrestricted agent shell
- an opaque prompt-driven workflow engine
- a platform where workflow rules exist only inside model behavior
- a broad cluster-mutation assistant by default

## 22. Open Research / Design Questions

Useful questions for deep analysis:

1. What is the best backend-owned intake and design-draft model that preserves explicit workflow control?
2. Which no-arg operator actions most reduce friction from paper to validation experiment?
3. Is it worth carrying a custom OpenClaw image to expose `tool_choice` on the gateway `agent` RPC?
4. What is the best current-hardware local model/runtime candidate for modestly improving structured tool use?
5. How should NATS durability be handled before or alongside shared network storage?
6. How should `.44`-local image import and runtime-export dependence be reduced?

## 23. Best Prompting Guidance For External Analysis

If you are using this spec in a deep research prompt, treat these as the design priorities:

- preserve explicit workflow contracts
- preserve narrow operator tooling
- reduce friction from paper to bounded experiment
- keep backend validation deterministic
- do not assume broader tool use is a free win
- do not recommend architectures that depend on trusting current model output too much

## 24. Most Important Summary

Glasslab is not trying to build a generic “AI does everything” shell.

It is trying to build a controlled research workflow platform where:

- the model helps with intake, interpretation, and operator UX
- the backend owns approval, manifests, and execution boundaries
- OpenClaw stays narrow
- Kubernetes provides bounded execution
- artifacts remain explicit
- the path from paper to validation experiment becomes low-friction without becoming illegible or unsafe
