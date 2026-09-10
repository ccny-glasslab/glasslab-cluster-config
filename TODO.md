# Glasslab Work Queue

Last reviewed: 2026-09-10

GitHub Issues are the authoritative backlog. This file is a compact priority
index for humans and coding agents arriving in the repository; it must not
duplicate complete task specifications or maintain an independent status. The
authoritative roadmap, ownership, and current order of work are in issue #206
("Define the post-MVP Glasslab roadmap after first end-to-end research
completion"), which is open. Issue #155, the previous roadmap, is closed.

Current issues:

- [all open work](https://github.com/ccny-glasslab/glasslab-cluster-config/issues)
- [ready work](https://github.com/ccny-glasslab/glasslab-cluster-config/issues?q=is%3Aissue%20state%3Aopen%20label%3Astate%3Aready)
- [newcomer work](https://github.com/ccny-glasslab/glasslab-cluster-config/issues?q=is%3Aissue%20state%3Aopen%20label%3A%22good%20first%20issue%22)

## P0: Architecture And End-To-End Validation

- [#206 Define the post-MVP Glasslab roadmap after first end-to-end research completion](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/206)
- [#101 Complete Fashion-MNIST compatibility run](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/101)

## P1: Research Validation And Operability

- [#429 Re-run corrected Wine clustering (#100 successor)](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/429)
- [#430 Finish evidence-prompt compaction (#93 successor)](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/430)
- [#420 Corpus RAG: ship the real reranker + confirm hybrid retrieval end-to-end + ingest new local textbooks](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/420)
- [#379 Honeydew agent-directed retrieval tool: read-only `retrieve_evidence(query)` for iterative corpus search](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/379)
- [#208 Define promotion from exploratory runs to confirmatory research campaigns](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/208)

## P2: Runtime Hardening And Cleanup

- [#427 Reconcile tracked orchestrator configmap with live split-model serving](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/427)
- [#426 Make rehearsal state durable on shared PVC (rollout-safe run-through)](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/426)
- [#431 Threshold-triggered turn-history rotation](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/431)
- [#432 Migrate `.17` mlx serving to launchd (reboot-survivable)](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/432)
- [#433 Evidence-driven per-turn-kind model routing](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/433)
- [#369 Unauthenticated read API on research-orchestrator exposes unredacted run state](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/369)
- [#271 Dormant-but-deployed service defects: router idempotency/timeouts, schedule-worker partial failures, evaluator dead paths](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/271)
- [#270 workflow-api low-severity hardening batch](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/270)
- [#269 Orchestrator low-severity cleanups found during defect trawl](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/269)
- [#229 Stabilize the OpenCode agent runtime (protocol-draft hangs and transient compile failures)](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/229)

## Maintenance Rule

When work is discovered, create or update a GitHub issue before changing this
index. The issue must contain scope, acceptance criteria, relevant area and
priority labels, dependencies, and enough context for a new contributor to
start without reconstructing chat history.

When work starts, comment with the intended approach and link the branch or
pull request. Pull requests should use `Closes #<issue>` when they fully satisfy
the issue. Close abandoned work with a reason rather than deleting it. Update
this file only when the short prioritized index changes.

Completed work belongs in release notes, design docs, or the issue history,
not in a growing completed-items section here.
