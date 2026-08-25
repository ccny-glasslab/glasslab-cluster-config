# Workflow Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Authenticate and authorize every workflow-api mutation, eliminate request-selected execution code, and reduce Kubernetes/network privileges.

**Architecture:** FastAPI middleware protects every non-safe HTTP method using named caller tokens and an operation allowlist. Three direct callers receive separate SOPS-managed tokens. Registry policy exclusively chooses digest-pinned runner images, entrypoints, commands, resources, and service accounts.

**Tech Stack:** Python 3, FastAPI, Pydantic, Kubernetes manifests/RBAC/NetworkPolicy, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-glasslab-sops-security-remediation-design.md`

## Global Constraints

- All mutation methods fail closed when auth configuration is absent or invalid.
- Tokens are compared with `secrets.compare_digest` and never logged.
- Each caller has a distinct token and least-privilege operation set.
- User input cannot select image, entrypoint, command, or service account.
- Runner images are immutable `@sha256:` references.
- WhatsApp callers are excluded.

---

### Task 1: Central mutation authentication and authorization

**Files:**
- Create: `services/workflow-api/app/auth.py`
- Modify: `services/workflow-api/app/config.py`
- Modify: `services/workflow-api/app/main.py`
- Create: `services/workflow-api/tests/test_auth.py`

**Interfaces:**
- `CallerPolicy(name: str, token: SecretStr, allowed_operations: frozenset[str])`
- `authenticate_request(request: Request, settings: Settings) -> CallerPolicy`
- Operation identity is normalized as `METHOD path-template`, not raw URL.
- Header names: `X-Glasslab-Caller` and `X-Glasslab-Workflow-Token`.

- [ ] **Step 1: Write failing exhaustive route tests**

Parameterize over `app.routes`; for every route containing POST, PUT, PATCH, or
DELETE assert missing/invalid token returns 401. Assert a valid known caller
without permission returns 403 and an authorized caller reaches the existing
route response. Assert GET `/healthz` remains available.

- [ ] **Step 2: Verify RED**

Run: `cd services/workflow-api && pytest -q tests/test_auth.py`

Expected: FAIL because current mutations are unauthenticated.

- [ ] **Step 3: Implement fail-closed middleware**

Parse caller policies from a JSON environment variable whose token fields are
populated by Secret references. Resolve the Starlette route template after
routing and authorize against the normalized operation. Follow the existing
constant-time pattern in `services/research-orchestrator/app/main.py`.

- [ ] **Step 4: Verify GREEN and regression suite**

Run: `cd services/workflow-api && pytest -q tests/test_auth.py`

Run: `cd services/workflow-api && pytest -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/workflow-api/app/auth.py services/workflow-api/app/config.py services/workflow-api/app/main.py services/workflow-api/tests/test_auth.py
git commit -m "Authenticate workflow API mutations"
```

### Task 2: Propagate distinct caller identity

**Files:**
- Modify: `services/research-command-router/app/main.py`
- Modify: `services/research-command-router/tests/test_api.py`
- Modify: `services/schedule-worker/app/main.py`
- Modify: `services/schedule-worker/tests/test_main.py`
- Modify: `services/research-orchestrator/app/cluster.py`
- Modify: `services/research-orchestrator/app/config.py`
- Modify: `services/research-orchestrator/tests/test_cluster.py`

**Interfaces:**
- Each caller reads `GLASSLAB_WORKFLOW_API_CALLER_NAME` and `GLASSLAB_WORKFLOW_API_TOKEN`.
- Every workflow-api mutation sends both headers; no token appears in exception text.

- [ ] **Step 1: Write failing caller tests**

Capture outbound requests and assert the exact caller name and token header are
present. Assert missing token prevents the outbound mutation and produces a
redacted configuration error.

- [ ] **Step 2: Verify RED**

Run the three focused suites:

```bash
(cd services/research-command-router && pytest -q tests/test_api.py)
(cd services/schedule-worker && pytest -q tests/test_main.py)
(cd services/research-orchestrator && pytest -q tests/test_cluster.py)
```

Expected: FAIL on missing headers.

- [ ] **Step 3: Implement one shared request-header pattern per service**

Construct headers immediately before the request. Do not store tokens in
dataclasses that are serialized/logged. Treat blank caller/token as fatal for
mutation calls.

- [ ] **Step 4: Verify GREEN**

Run the same three commands; expected PASS.

- [ ] **Step 5: Commit**

```bash
git add services/research-command-router services/schedule-worker services/research-orchestrator
git commit -m "Identify workflow API callers"
```

### Task 3: Remove request-controlled runner execution

**Files:**
- Modify: `services/workflow-api/app/schemas.py`
- Modify: `services/workflow-api/app/execution_routes.py`
- Modify: `services/workflow-api/app/registry.py`
- Modify: `services/workflow-api/app/job_submission.py`
- Modify: affected records in `services/workflow-registry/`
- Modify: `services/workflow-api/tests/test_api.py`
- Modify: `services/workflow-api/tests/test_validation.py`

**Interfaces:**
- `GenericExperimentRunRequest` no longer accepts `image_ref` or `entrypoint`.
- Registry records require `runner_image` containing `@sha256:`, fixed entrypoint/command, resource bounds, and `runner_service_account_name`.

- [ ] **Step 1: Write failing boundary tests**

Assert image/entrypoint fields receive 422, every registry record with a tag or
`allow_custom_*: true` is rejected, and submitted Jobs exactly match registry
image, entrypoint, resources, and service account.

- [ ] **Step 2: Verify RED**

Run: `cd services/workflow-api && pytest -q tests/test_api.py tests/test_validation.py`

Expected: FAIL because `metric-search-v0` permits both overrides.

- [ ] **Step 3: Enforce registry-owned execution**

Remove custom fields and flags, reject unknown request fields, validate digest
syntax, and move service-account selection into the registry record. Preserve
existing no-privilege-escalation, dropped capabilities, RuntimeDefault seccomp,
and disabled runner token automount.

- [ ] **Step 4: Verify GREEN**

Run: `cd services/workflow-api && pytest -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/workflow-api services/workflow-registry
git commit -m "Bind runner execution to approved registry policy"
```

### Task 4: Caller secrets, RBAC, and ingress policy

**Files:**
- Modify: `kubeadm/glasslab-v2/workflow-api/10-rbac.yaml`
- Modify: `kubeadm/glasslab-v2/workflow-api/20-deployment.yaml`
- Create: `kubeadm/glasslab-v2/workflow-api/50-ingress-network-policy.yaml`
- Modify: caller deployment manifests under `kubeadm/glasslab-v2/{research-command-router,schedule-worker,research-orchestrator}/`
- Modify: corresponding nondeployable secret examples
- Create: `tests/security/test_workflow_security_manifests.py`

**Interfaces:**
- SOPS vault produces three caller tokens plus workflow-api policy configuration.
- Workflow-api Role retains Job/pod/PVC verbs required for execution and has no Secret verbs/resources.
- Ingress TCP/8080 is allowed only from the three labeled callers.

- [ ] **Step 1: Write failing manifest assertions**

Assert Role rules never include `secrets`; each caller has a dedicated
secretKeyRef for its token and fixed caller name; workflow-api receives all
token refs; NetworkPolicy selects workflow-api and only named caller labels.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_workflow_security_manifests -v`

Expected: FAIL on current RBAC and absent policy/token mounts.

- [ ] **Step 3: Implement least-privilege manifests**

Remove Secret reads, add dedicated token references, and add ingress policy.
Keep the workflow-api service account token because Kubernetes Job operations
require it; keep runner token automount disabled.

- [ ] **Step 4: Verify GREEN and config parsing**

Run: `python3 -m unittest tests.security.test_workflow_security_manifests -v`

Run: `python3 scripts/validate-configs.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kubeadm/glasslab-v2 tests/security
git commit -m "Restrict workflow API credentials and ingress"
```

### Task 5: Live token rollout and denial validation

**Files:**
- Create outside Git: SOPS caller token documents in the external vault.
- Modify: `docs/glasslab-v2/runbooks/` relevant rollout documentation.

**Interfaces:**
- Produces rotated distinct tokens applied as Kubernetes Secrets.
- Validates authorized traffic, missing-token denial, wrong-caller denial, and RBAC denial.

- [ ] **Step 1: Create distinct random tokens through the SOPS workflow**

Do not print tokens. Apply them with `glasslab-secret apply` and confirm Secret
names only.

- [ ] **Step 2: Roll workflow-api and callers from `.44`**

Build/push changed images with the merged SHA, apply RBAC/network manifests,
set exact images, and wait for rollouts.

- [ ] **Step 3: Prove authorized and unauthorized behavior**

Run the normal v2 smoke suite. From an uncredentialed test pod, verify mutations
return 401. With a valid token but disallowed caller/operation, verify 403.
Verify the three legitimate caller flows still work.

- [ ] **Step 4: Prove Kubernetes least privilege**

Run:

```bash
kubectl auth can-i get secrets --as=system:serviceaccount:glasslab-v2:glasslab-workflow-api -n glasslab-v2
kubectl auth can-i create jobs --as=system:serviceaccount:glasslab-v2:glasslab-workflow-api -n glasslab-v2
```

Expected: `no`, then `yes`.

- [ ] **Step 5: Record validation and commit docs**

```bash
git add docs/glasslab-v2/runbooks
git commit -m "Document authenticated workflow rollout"
```
