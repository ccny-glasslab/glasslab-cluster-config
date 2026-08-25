# Ingestion and Runtime Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent source-document SSRF/unbounded downloads and stop Hermes/OpenCode child processes from inheriting orchestrator credentials.

**Architecture:** A dedicated URL policy validates scheme, credentials, DNS results, redirect targets, and response size before bytes reach document parsing. A shared runtime-environment builder starts children from an explicit safe baseline and adds only runtime-specific non-secret values.

**Tech Stack:** Python 3, urllib, ipaddress/socket, subprocess, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-glasslab-sops-security-remediation-design.md`

## Global Constraints

- Source fetches accept HTTPS only and reject credentials in URLs.
- Every initial and redirected hostname is resolved and all returned addresses must be public/unicast.
- Downloads are streamed with fixed connect/read timeout, redirect ceiling, and byte ceiling.
- Runtime children do not inherit the parent environment wholesale.
- Secret names/values never enter child argv, environment, log, or exception output.

---

### Task 1: URL and resolved-address policy

**Files:**
- Create: `services/workflow-api/app/safe_fetch.py`
- Create: `services/workflow-api/tests/test_safe_fetch.py`
- Modify: `services/workflow-api/app/config.py`

**Interfaces:**
- `validate_source_url(url: str, resolver: Resolver = socket.getaddrinfo) -> ValidatedTarget`
- `ValidatedTarget(url: str, hostname: str, port: int, addresses: tuple[str, ...])`
- Settings: `source_fetch_max_bytes`, `source_fetch_timeout_seconds`, `source_fetch_max_redirects`, `source_fetch_allowed_hosts`.

- [ ] **Step 1: Write failing URL-policy tests**

Cover HTTP, FTP/file, embedded credentials, localhost names, IPv4/IPv6 literals,
loopback, RFC1918, link-local metadata (`169.254.169.254`), multicast,
unspecified, reserved, Kubernetes service ranges, mixed public/private DNS
answers, and a public HTTPS control. Resolver results are deterministic fakes.

- [ ] **Step 2: Verify RED**

Run: `cd services/workflow-api && pytest -q tests/test_safe_fetch.py`

Expected: FAIL because `safe_fetch` is absent.

- [ ] **Step 3: Implement strict validation**

Use `urllib.parse.urlsplit` and `ipaddress.ip_address`. Require `https`, no
username/password, a hostname, and port 443 unless explicitly allowed. Reject
an entire hostname if any resolved address is not globally routable. Optional
allowlist entries are exact hosts or a leading `*.` suffix rule; never substring
matching.

- [ ] **Step 4: Verify GREEN**

Run: `cd services/workflow-api && pytest -q tests/test_safe_fetch.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/workflow-api/app/safe_fetch.py services/workflow-api/app/config.py services/workflow-api/tests/test_safe_fetch.py
git commit -m "Validate source document network targets"
```

### Task 2: Bounded streaming and redirect revalidation

**Files:**
- Modify: `services/workflow-api/app/safe_fetch.py`
- Modify: `services/workflow-api/app/source_documents.py`
- Modify: `services/workflow-api/tests/test_safe_fetch.py`
- Modify: `services/workflow-api/tests/test_api.py`

**Interfaces:**
- `fetch_https_bytes(url: str, policy: FetchPolicy) -> tuple[bytes, str | None]`
- Raises stable `UnsafeSourceUrl`, `SourceTooLarge`, and `SourceFetchFailed` exceptions with redacted messages.

- [ ] **Step 1: Write failing streaming tests**

Use a fake opener/response to test oversized Content-Length, missing
Content-Length with streamed overflow, exact limit, timeout, too many redirects,
redirect to private IP, redirect with credentials, and a valid small response.
Assert no test uses an unbounded `.read()`.

- [ ] **Step 2: Verify RED**

Run: `cd services/workflow-api && pytest -q tests/test_safe_fetch.py tests/test_api.py -k 'source_document or safe_fetch'`

Expected: FAIL against current `response.read()` implementation.

- [ ] **Step 3: Implement bounded fetch**

Disable urllib automatic redirects or intercept each redirect. Re-run URL and
DNS validation for every location. Read chunks no larger than 64 KiB and stop
before retaining more than `source_fetch_max_bytes`. Convert policy failures to
the existing fetch-failed document state without exposing resolved internal
addresses to callers.

- [ ] **Step 4: Verify GREEN and full workflow suite**

Run: `cd services/workflow-api && pytest -q tests/test_safe_fetch.py tests/test_api.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/workflow-api/app/safe_fetch.py services/workflow-api/app/source_documents.py services/workflow-api/tests
git commit -m "Bound and revalidate source document fetches"
```

### Task 3: Shared minimal child environment

**Files:**
- Create: `services/research-orchestrator/app/runtime_environment.py`
- Create: `services/research-orchestrator/tests/test_runtime_environment.py`
- Modify: `services/research-orchestrator/app/hermes_runtime.py`
- Modify: `services/research-orchestrator/app/opencode_runtime.py`
- Modify: `services/research-orchestrator/tests/test_hermes_runtime.py`
- Modify: `services/research-orchestrator/tests/test_discord_and_opencode.py`

**Interfaces:**
- `build_runtime_environment(*, additions: Mapping[str, str], inherited_names: Collection[str] = SAFE_BASELINE) -> dict[str, str]`
- `SAFE_BASELINE` contains only `PATH`, locale variables, `TERM`, `TMPDIR`, and proxy/CA variables explicitly required and reviewed.

- [ ] **Step 1: Write failing sentinel-secret tests**

Set parent values for Postgres DSN, Discord token, operator token, workflow token,
AWS/MinIO keys, GitHub token, and an unrelated sentinel. Capture the `env`
passed to fake `subprocess.Popen` for both runtimes. Assert none are present;
assert required HOME/XDG/Hermes/server variables remain.

- [ ] **Step 2: Verify RED**

Run:

```bash
cd services/research-orchestrator
pytest -q tests/test_runtime_environment.py tests/test_hermes_runtime.py tests/test_discord_and_opencode.py
```

Expected: FAIL because both runtimes spread `os.environ` into the child.

- [ ] **Step 3: Implement explicit environment construction**

Copy only named safe baseline keys when present, then add runtime-specific
values. Reject additions whose name matches centrally defined parent-secret
patterns unless explicitly scoped by a future interface. Do not mutate
`os.environ`. Remove the OpenCode comment that acknowledges full inheritance.

- [ ] **Step 4: Verify GREEN**

Run the same three pytest files; expected PASS.

- [ ] **Step 5: Commit**

```bash
git add services/research-orchestrator/app/runtime_environment.py services/research-orchestrator/app/hermes_runtime.py services/research-orchestrator/app/opencode_runtime.py services/research-orchestrator/tests
git commit -m "Isolate agent runtime environments"
```

### Task 4: Manifest and live validation

**Files:**
- Modify: `kubeadm/glasslab-v2/config/10-workflow-api-configmap.yaml`
- Modify: workflow-api and orchestrator deployment documentation
- Create: `tests/security/test_fetch_runtime_manifests.py`

**Interfaces:**
- Workflow-api manifest sets exact fetch ceilings/timeouts/redirect count and optional allowed hosts.
- Orchestrator manifests continue to mount parent secrets only into the parent; tests prove child isolation in application code.

- [ ] **Step 1: Write failing manifest defaults test**

Assert positive finite byte/time/redirect settings exist and no wildcard
allowlist is used.

- [ ] **Step 2: Verify RED**

Run: `python3 -m unittest tests.security.test_fetch_runtime_manifests -v`

Expected: FAIL because settings are absent.

- [ ] **Step 3: Add bounded deployment configuration and docs**

Choose conservative documented defaults (for example 25 MiB, 30 seconds, and
three redirects) based on accepted paper/PDF sizes; exact values must match
Pydantic defaults and tests.

- [ ] **Step 4: Verify GREEN and service suites**

Run:

```bash
python3 -m unittest tests.security.test_fetch_runtime_manifests -v
python3 scripts/validate-configs.py
(cd services/workflow-api && pytest -q)
(cd services/research-orchestrator && pytest -q)
```

Expected: PASS.

- [ ] **Step 5: Roll and probe live controls**

Deploy merged images from `.44`. Confirm a public allowlisted document ingests,
private/link-local targets are rejected, oversized input is rejected, and a
runtime diagnostic reports only key names from the safe child environment—no
values.

- [ ] **Step 6: Commit validation docs**

```bash
git add kubeadm/glasslab-v2 docs tests/security
git commit -m "Configure bounded ingestion and runtime isolation"
```
