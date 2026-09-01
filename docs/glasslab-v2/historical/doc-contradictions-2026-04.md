# Doc Contradictions 2026-04

**Date:** 2026-04-22  
**Scope:** Contradictions found in `/Users/glasslab/cluster-config/docs/glasslab-v2/`

---

## Summary

This report documents contradictions between older historical documents and the current Glasslab v2 product definition established in the 2026-04 cleanup pass. The canonical current state is defined by:

- `docs/glasslab-v2/README.md`
- `docs/glasslab-v2/current/README.md`
- `docs/glasslab-v2/canonical-stack-2026-04.md`
- `docs/glasslab-v2/deprecation-map-2026-04.md`
- `docs/glasslab-v2/product-cleanup-2026-04.md`

---

## Contradictions

### 1. OpenClaw as Primary Operator Shell

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/operator-access-recommendation.md` (lines 114-116) | "Glasslab should add one stable authenticated path to OpenClaw... It is not the workflow brain" | `canonical-stack-2026-04.md` (lines 118-120, 159-161) |
| `docs/glasslab-v2/bounded-agent-architecture.md` (lines 226-230) | OpenClaw receives "requests, triggers stage transitions, summarizes results" as primary role | `canonical-stack-2026-04.md` (lines 118-120, 159-161, 163-167) |
| `docs/glasslab-v2/external-literature-path.md` (lines 52, 169-175) | OpenClaw "summarizes and helps compare" literature findings as primary interface | `canonical-stack-2026-04.md` (lines 118-120, 159-161) |
| `docs/glasslab-v2/live-state-2026-03-28.md` (lines 100-110) | "OpenClaw remains unreliable as a multi-step workflow planner... move intent handling into deterministic backend-owned paths" | `canonical-stack-2026-04.md` (lines 118-120) |
| `docs/glasslab-v2/resume-next-session-2026-03-24.md` (lines 108-112) | "OpenClaw remains the narrow front door... Macs host stronger inference and ranking" | `canonical-stack-2026-04.md` (lines 118-120, 163-167) |
| `docs/glasslab-v2/research-pipeline-target.md` (lines 71-74) | "OpenClaw as the operator shell... with deterministic backend services" | `canonical-stack-2026-04.md` (lines 118-120) |

**Current State (2026-04):** OpenClaw is explicitly de-emphasized as the primary operator surface. The canonical path is `whatsapp-gateway -> research-ingress -> research-command-router -> workflow-api` (canonical-stack-2026-04.md lines 28-36). OpenClaw may remain as "optional and secondary" (overview.md line 143) for conversation and summaries, but it is NOT the command router or workflow brain.

---

### 2. JSON Store as Current Metadata Brain

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/state-and-storage-map-2026-03-27.md` (lines 57-87, 119-120, 151-155, 417-418) | "workflow-api now runs with GLASSLAB_WORKFLOW_API_STORE_BACKEND=json... `/mnt/artifacts/workflow-api/state/run-store.json`" | `canonical-stack-2026-04.md` (lines 67-98, 159-161) |
| `docs/glasslab-v2/state-and-storage-map-2026-03-27.md` (lines 210-225, 232-244) | OpenClaw persistent state at `/var/lib/openclaw/state` remains current | `canonical-stack-2026-04.md` (lines 159-161, 171-175) |
| `docs/glasslab-v2/product-cleanup-2026-04.md` (lines 152-180) | "JSON-on-artifacts-share as the long-term metadata store" remains viable | `canonical-stack-2026-04.md` (lines 67-98) |

**Current State (2026-04):** Postgres is the canonical system of record for session/workflow metadata (canonical-stack-2026-04.md line 73). JSON-backed state in workflow-api is explicitly described as "current technical debt" (overview.md line 188) and "a migration target, not the desired steady state" (overview.md line 189). The JSON store is "no longer the active record store" (state-and-storage-map-2026-03-27.md line 85) and "now a backup/import source" (state-and-storage-map-2026-03-27.md line 86).

---

### 3. Literature-First Product Identity

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/external-literature-path.md` (lines 306-318) | "The user should eventually be able to say: `!research forged art detection...` and get: a real literature search over external sources... making Glasslab behave like a real research assistant, rather than a seed-corpus demo" | `canonical-stack-2026-04.md` (lines 22-25, 159-161, 163-167) |
| `docs/glasslab-v2/bounded-experiment-runner-priority.md` (lines 30-36) | "broad literature search as a headline product capability... broad external literature search as a product headline" remains priority | `canonical-stack-2026-04.md` (lines 19-25, 159-161) |
| `docs/glasslab-v2/research-assistant-implementation-checklist.md` (lines 1-70, 150-165) | "start literature search" as primary product command, broad literature search as product center | `canonical-stack-2026-04.md` (lines 159-161) |
| `docs/glasslab-v2/research-assistant-infra-proposal.md` (lines 1-20, 102-110) | "a human starts from a research idea... gather and interpret literature... propose bounded experiments" as primary loop | `canonical-stack-2026-04.md` (lines 9-25, 159-161) |
| `docs/glasslab-v2/research-assistant-ux-boundary.md` (lines 30-132) | "a research assistant that cannot reliably start a research session or gather the first papers does not have a usable UX" as product center | `canonical-stack-2026-04.md` (lines 9-25) |
| `docs/glasslab-v2/research-pipeline-target.md` (lines 9-30) | "the closest external shape to what Glasslab is trying to become is something like autoresearch" | `canonical-stack-2026-04.md` (lines 9-25) |

**Current State (2026-04):** The product is explicitly "runner-first" (canonical-stack-2026-04.md line 9) and "not primarily: a literature-search product, a general chat agent, an autonomous scientist, an OpenClaw-centered orchestration system" (canonical-stack-2026-04.md lines 22-25). Literature support remains "bounded source intake and review" (canonical-stack-2026-04.md line 159), not "broad literature search" (canonical-stack-2026-04.md line 159). The primary loop is `session -> source intake -> plan -> preflight -> run -> compare -> decide -> next bounded variant` (canonical-stack-2026-04.md lines 167-174).

---

### 4. Bespoke Workflow-Family Growth

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/workflow-registry.md` (lines 31-34) | "Do not add a new workflow family for every research topic or AI/ML subdomain... topics like fake-art detection... belong in research sessions and design drafts" | `docs/glasslab-v2/autoresearch-lane.md` (lines 113-121) |
| `docs/glasslab-v2/external-literature-path.md` (lines 199-200) | Suggests adding "literature-search" capability inside `workflow-api` or as separate `literature-agent` | `docs/glasslab-v2/workflow-registry.md` (lines 31-34) |
| `docs/glasslab-v2/autoresearch-lane.md` (lines 1-131) | Autoresearch as broad "general autonomous scientist behavior" | `canonical-stack-2026-04.md` (lines 19-25, 159-161) |
| `docs/glasslab-v2/research-pipeline-target.md` (lines 100-116) | Proposes 9-stage "literature-to-experiment-to-report" workflow family | `docs/glasslab-v2/workflow-registry.md` (lines 31-34) |

**Current State (2026-04):** Workflow families are explicitly coarse execution templates only (workflow-registry.md line 29), not topic-specific ontologies (workflow-registry.md line 34). The registry should contain only "approved workflow families" (external-literature-path.md line 42), with topics like "forged art detection" belonging in research sessions, not the registry taxonomy (workflow-registry.md line 34). Autoresearch is "bounded methodology exploration" (autoresearch-lane.md line 134), not "general autonomous science" (autoresearch-lane.md line 138).

---

### 5. Old Ollama/Mac Inference Paths

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/mac-studio-inference.md` (lines 9-40, 118-137) | `.23` with `deepseek-r1:32b` as primary inference host; native-Ollama tool experiments with `.12` | `canonical-stack-2026-04.md` (lines 51, 120-121) |
| `docs/glasslab-v2/resume-next-session-2026-03-24.md` (lines 12-29, 57-71) | `deepseek-r1:32b` on `.23`, `qwen3:30b` pull in progress; `.12` `qwen3:14b` for native tool experiments | `canonical-stack-2026-04.md` (lines 51, 120-121, 159-161) |
| `docs/glasslab-v2/live-state-2026-03-28.md` (lines 44-47, 84-86) | OpenClaw configured for `.23` `qwen3:30b` as current live setup | `canonical-stack-2026-04.md` (lines 51, 120-121) |

**Current State (2026-04):** The "old Ollama-backed operator / stage-agent assumptions" are explicitly deprecated as current product truth (product-cleanup-2026-04.md lines 119-151). The canonical backend path is now exo OpenAI-compatible serving (product-cleanup-2026-04.md line 51), not Mac Ollama paths (product-cleanup-2026-04.md lines 119-151). The exo endpoint is at `.21` (192.168.1.21:52415) with model `mlx-community/Qwen3-Coder-Next-4bit` (interpretation-agent-service.md line 144).

---

### 6. OpenClaw as Workflow Brain

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/research-assistant-infra-proposal.md` (lines 38-40, 90-99) | "OpenClaw remains the front door... but OpenClaw is still being asked to route intent... decide whether to touch the backend... sequence or recover backend actions... classify failures" | `canonical-stack-2026-04.md` (lines 118-120) |
| `docs/glasslab-v2/live-state-2026-03-28.md` (lines 37-45, 93-111) | "OpenClaw command mediation was the main reliability failure... OpenClaw is still too responsible for intent routing... decide when to touch the backend... classify failures" | `canonical-stack-2026-04.md` (lines 118-120, 163-167) |
| `docs/glasslab-v2/bounded-agent-architecture.md` (lines 226-230) | OpenClaw "receives requests, triggers stage transitions, summarizes results" | `canonical-stack-2026-04.md` (lines 118-120) |

**Current State (2026-04):** OpenClaw is explicitly "not the workflow brain" and "not the command router" (overview.md lines 151-152). The command routing and workflow control is owned by `workflow-api`, `research-ingress`, and `research-command-router` (canonical-stack-2026-04.md lines 28-36). OpenClaw may remain for "optional chat, summaries, read-only or bounded help" (overview.md lines 145-150), but it does not own the workflow structure.

---

### 7. `latest` Aliases as Primary UX

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/state-and-storage-map-2026-03-27.md` (lines 157-158) | "workflow-api uses GLASSLAB_WORKFLOW_API_ARTIFACTS_MOUNT_PATH=/mnt/artifacts... run artifact root: `/mnt/artifacts/<run_id>/`" | `canonical-stack-2026-04.md` (lines 159-161, 167-174) |
| `docs/glasslab-v2/router-and-backend-contract.md` (lines 235-245) | "`latest` may remain internally available; primary operator flows should not be documented around `latest`" | `canonical-stack-2026-04.md` (lines 159-161, 167-174) |
| `docs/glasslab-v2/research-session-cli.md` (lines 16-20) | "Resume or reuse active session" with `start` command implies `latest` semantics | `canonical-stack-2026-04.md` (lines 159-161) |

**Current State (2026-04):** `latest` aliases are deprecated in favor of sender-pinned session semantics (canonical-stack-2026-04.md line 163). The primary UX uses explicit session IDs, not `latest` (canonical-stack-2026-04.md lines 159-161). The command surface uses sender-pinned sessions with `session_id` routing (research-command-router.md lines 235-245).

---

### 8. vLLM as Current Product Lane

| File | Contradictory Claim | Conflicts With |
|------|---------------------|----------------|
| `docs/glasslab-v2/node02-role-decision.md` (lines 9-119) | "Keep Legacy vLLM As Active Fallback... preserve the old cluster-local tool-capable path as a standby lane" | `canonical-stack-2026-04.md` (lines 159-161, 163-167) |
| `docs/glasslab-v2/live-state-2026-04-03.md` (lines 11-12) | Workflow-api rolled live as `ghcr.io/ccny-glasslab/glasslab-workflow-api:0.1.84-local` on `.44` | `canonical-stack-2026-04.md` (lines 127-128, 136-142) |

**Current State (2026-04):** The "vLLM as a current product lane" is explicitly legacy (product-cleanup-2026-04.md line 253). The "old in-cluster `vllm` story is legacy" (product-cleanup-2026-04.md line 254). The canonical backend is exo OpenAI-compatible serving, not vLLM (product-cleanup-2026-04.md line 51). Node02 vLLM should be "retired or repurposed" (node02-role-decision.md line 37).

---

## High-Signal Conflicts Summary

The most critical contradictions (those that would cause operational errors if followed) are:

1. **OpenClaw as primary operator shell** - The old OpenClaw path was the main reliability failure; current product uses WhatsApp gateway + deterministic router (product-cleanup-2026-04.md lines 52, 94-118).

2. **JSON store as current metadata brain** - JSON-backed workflow-api state is explicitly technical debt; Postgres is the canonical system of record (product-cleanup-2026-04.md lines 152-180).

3. **Literature-first product identity** - Product is explicitly runner-first, not literature-first; literature support is bounded source intake, not broad search (product-cleanup-2026-04.md lines 182-220).

4. **OpenClaw as workflow brain** - OpenClaw should never own workflow structure; backend owns command routing and orchestration (product-cleanup-2026-04.md lines 94-118).

5. **Old Ollama/Mac inference paths** - Mac Ollama paths are deprecated; exo OpenAI-compatible serving is canonical (product-cleanup-2026-04.md lines 119-151).

---

## Recommendations for Documentation Maintenance

1. **Mark historical documents clearly:** All March live-state notes, resume-next-session notes, and OpenClaw-first narratives should be explicitly marked as historical snapshots (README.md lines 82-96).

2. **Remove outdated current-state claims:** Rewrite current summary docs to stop contradicting the canonical stack (product-cleanup-2026-04.md lines 377-405).

3. **Update OpenClaw references:** Keep OpenClaw as optional, secondary, and explicitly not the workflow brain (product-cleanup-2026-04.md lines 57-70).

4. **Replace JSON-store assumptions:** Implement Postgres-backed workflow/session store and cut workflow-api from JSON (product-cleanup-2026-04.md lines 175-180).

5. **Demote literature-first messaging:** Rename "literature pipeline" to "source intake and review" and make external search feature-flagged (product-cleanup-2026-04.md lines 182-220).

---

## Appendix: Current State Reference

### Primary Command Path
`whatsapp-gateway -> research-ingress -> research-command-router -> workflow-api` (canonical-stack-2026-04.md lines 28-36)

### Primary Commands
`!new`, `!state`, `!add`, `!plan`, `!check`, `!run`, `!compare`, `!decide`, `!next` (canonical-stack-2026-04.md lines 40-49)

### Primary State Ownership
- **Records:** Postgres
- **Files/Objects:** Shared filesystem and/or MinIO (canonical-stack-2026-04.md lines 67-98)

### Primary Backend
Exo OpenAI-compatible serving (`.21`: 192.168.1.21:52415) with model `mlx-community/Qwen3-Coder-Next-4bit` (product-cleanup-2026-04.md line 51)

### Primary Control Surface
Repo-owned WhatsApp/control path through `whatsapp-gateway` (canonical-stack-2026-04.md line 118)

### Current Infrastructure
- `.44`: Canonical apply host, validation host, local secret source of truth
- `.21`: Primary exo node, exo OpenAI-compatible serving
- `.19`: Secondary exo node for distributed inference

---

*Report generated 2026-04-22 based on comprehensive sweep of `/Users/glasslab/cluster-config/docs/glasslab-v2/`*
