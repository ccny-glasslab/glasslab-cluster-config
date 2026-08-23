# Glasslab Work Queue

Last reviewed: 2026-08-13

GitHub Issues are the authoritative backlog. This file is a compact priority
index for humans and coding agents arriving in the repository; it must not
duplicate complete task specifications or maintain an independent status. The
authoritative roadmap, ownership, and current order of work are in issue #155.

Current issues:

- [all open work](https://github.com/ccny-glasslab/glasslab-cluster-config/issues)
- [ready work](https://github.com/ccny-glasslab/glasslab-cluster-config/issues?q=is%3Aissue%20state%3Aopen%20label%3Astate%3Aready)
- [newcomer work](https://github.com/ccny-glasslab/glasslab-cluster-config/issues?q=is%3Aissue%20state%3Aopen%20label%3A%22good%20first%20issue%22)

## P0: Architecture And End-To-End Validation

- [#154 Reassess ResearchOrchestrator responsibilities after Hermes migration](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/154)
- [#92 Add terminal research-run checkpoint retry](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/92), implemented in #145 (draft)
- [#98 Validate an arbitrary-dataset research workflow end to end](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/98)

## P1: Research Validation And Operability

- [#100 Complete corrected Wine clustering run](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/100), blocked by #92/#145
- [#101 Complete Fashion-MNIST compatibility run](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/101)
- [#93 Compact research-agent evidence prompts](https://github.com/ccny-glasslab/glasslab-cluster-config/issues/93)

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
