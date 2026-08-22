# Lab Security Agent Dispatch Design

Date: 2026-08-22
Status: approved in chat; awaiting written-spec review

## Objective

Use Glasslab's exo-hosted model for most parallel repository security-audit
work while retaining a hosted Codex session as the final validator and
integrator. The design reduces hosted inference consumption without treating
local-model output as trusted evidence.

## Scope

The first implementation supports bounded security discovery and repair tasks
for this repository. It reuses the existing OpenCode-to-exo provider setup and
Git worktree conventions. It does not replace Codex's native subagent API,
modify the research orchestrator, deploy a new cluster service, or allow a
model to operate the live cluster.

## Trust Model

Lab agents are untrusted proposal generators. Their prose, classifications,
patches, and claimed test results are candidates until independently checked.
They receive no SSH agent, Kubernetes configuration, SOPS identity, secret
files, GitHub credentials, or permission to push or merge.

The dispatcher may expose only the selected Git worktree, a per-run runtime
directory, and an explicitly written assignment. It must start each worker
with a sanitized environment and fail closed if the source repository is
dirty, the requested base revision is ambiguous, the exo endpoint is
unhealthy, or isolation cannot be established.

The primary Codex session remains responsible for validating each finding,
reviewing every diff, running relevant checks, and authorizing commits or
integration. Hosted subagents are an explicit escalation option for findings
whose validity or remediation remains uncertain; they are not the default.

## User Interface

Add one repository-owned command, `scripts/lab-security-agent`, with two
explicit modes:

- `discover`: create an isolated detached worktree and require a read-only
  audit report.
- `repair`: create an isolated branch worktree for one already validated
  finding and permit changes only inside that worktree.

The command accepts an assignment file rather than an unrestricted shell
fragment. The caller supplies a run name, mode, base revision, and assignment.
Concurrency is controlled by the caller; the initial implementation launches
one worker per invocation so parallelism remains visible and bounded.

The launcher prints the worktree path, runtime path, process identifier, log
path, and result path. It never automatically commits, pushes, merges, deletes
a non-clean worktree, or copies a patch into the canonical checkout.

## Assignment Contract

Every assignment combines:

1. the repository's `AGENTS.md` instructions;
2. a stable, repository-owned security methodology;
3. the caller's narrow audit scope or validated finding;
4. output and evidence requirements; and
5. mode-specific tool restrictions.

Discovery reports use a machine-readable JSON document plus a Markdown
summary. Each candidate finding records a stable identifier, title, severity,
confidence, affected paths and lines, attacker preconditions, source-to-sink
reasoning, impact, reproduction or inspection steps, recommended validation,
and a remediation outline. Empty reports are valid and must explicitly state
what was inspected.

Repair results include the validated finding identifier, base commit, final
commit or working-tree diff digest, files changed, commands actually run,
their exit status, residual risk, and any unresolved question. A repair agent
must not broaden its task into unrelated findings.

## Security Methodology

The repository-owned methodology mirrors the useful structure of the Codex
Security workflow without assuming that OpenCode can load Codex plugins. It
requires repository-grounded threat boundaries, secret and credential
handling review, injection and command-execution paths, authorization checks,
unsafe deserialization or parsing, filesystem boundary violations,
supply-chain and CI exposure, logging leakage, insecure operational defaults,
and validation of exploitability before severity assignment.

The prompt explicitly separates candidate discovery from validation. The lab
model must prefer precise, evidenced findings over counts and must report
uncertainty rather than inventing proof.

## Isolation And Credentials

Each run uses a uniquely named worktree rooted beneath an ignored dispatcher
directory. Discovery worktrees are detached at the selected base commit and
configured read-only through OpenCode permissions. Repair worktrees use a
dedicated `lab-agent/<run-name>` branch and permit writes only under that
worktree.

The launcher constructs a minimal environment instead of inheriting the
interactive shell wholesale. It supplies only the executable search path,
locale, isolated HOME/XDG directories, exo base URL, selected model, and
run-specific paths. Credential-shaped variables in the parent environment are
not propagated. The launcher does not mount or copy files from `.ssh`,
`.kube`, `.config/sops`, ignored secret manifests, or external directories.

OpenCode sharing, web access, task delegation, external-directory access, and
automatic updates are disabled. Discovery mode denies write-capable tools.
Repair mode allows repository-local editing and bounded test commands but
denies network access and access outside the worktree.

## Runtime And Data Flow

The launcher first validates its inputs, resolves the base commit, checks the
exo `/v1/models` endpoint, and creates the isolated worktree and runtime
directories. It then generates a per-run OpenCode configuration pointing at
the existing OpenAI-compatible exo endpoint and starts one non-interactive
turn with the assembled assignment.

OpenCode stdout and stderr are captured without being interpreted as success.
Completion requires a zero process exit status, a valid result document, and
mode-specific postconditions. Discovery additionally requires an unchanged
worktree. Repair records the complete diff and rejects modifications outside
the allowed worktree or result directory.

The primary session reads the report, reproduces the evidence, and either
rejects, validates, or escalates each candidate. For validated repairs, it
reviews the diff and runs independent verification before choosing whether to
commit or integrate it.

## Failure Handling And Cleanup

A failed health check, timeout, malformed result, dirty discovery worktree,
unexpected path, or OpenCode error marks the run failed and preserves its
artifacts for diagnosis. The launcher records no secrets in arguments or logs.

Cleanup is a separate explicit operation. It refuses to remove worktrees with
uncommitted changes and uses `git worktree remove` only for the exact recorded
path. Repair branches are never deleted automatically.

## Verification

Automated tests cover argument validation, safe run-name handling, base-commit
resolution, ignored-directory enforcement, sanitized environment construction,
mode-specific OpenCode permissions, assignment composition, malformed output,
timeouts, unchanged discovery enforcement, repair diff capture, and safe
cleanup refusal.

An integration smoke test uses a fake OpenAI-compatible endpoint and a fake
OpenCode executable so it is deterministic and consumes no model resources.
A separately invoked live smoke test asks exo to inspect a tiny fixture
repository, produce the required result schema, and leave discovery state
unchanged. The live test is never part of the normal pre-push suite.

## Rollout

Begin with one read-only discovery worker on one narrow portion of the current
security audit. Compare its report against the hosted review and record misses,
false positives, runtime, and hosted-credit savings. Enable repair mode only
after discovery containment and output validation pass. Increase concurrency
only after observing that the exo pair remains stable under the chosen load.

Success means most first-pass audit scopes can run on lab inference, every
accepted finding still has independent evidence, no worker can access secrets
or live infrastructure credentials, and parallel work no longer silently
multiplies hosted-model usage.
