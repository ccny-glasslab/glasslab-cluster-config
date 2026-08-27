# Deprecation Map 2026-04

This document explains what is current, what is secondary, and what should now be treated as historical or compatibility-only.

## Current

### Primary command/control path

* `OpenCode -> repo-owned scripts -> workflow-api`

### Secondary remote command adapter

* `whatsapp-gateway` and `research-ingress` are retired (issue #159); see
  `docs/glasslab-v2/historical/README.md`. The remaining
  `research-command-router -> workflow-api` link is held pending confirmation
  on the #173 workflow-api auth work.

### Primary product loop

* session
* source intake
* plan
* preflight
* run
* compare
* decide
* next bounded variant

### Primary state ownership

* Postgres for records
* filesystem and/or MinIO for files

### Primary deploy path

* GHCR pull-based deployment

## Secondary

### Historical OpenClaw material

Keep only as:

* historical reference

Not as:

* a deployed control surface
* command routing
* workflow control
* a supported conversational fallback

### Granular debug commands

Keep only as operator/debug tools.

Examples:

* `!research`
* `!more-papers`
* `!next-paper`
* `!interpret`
* `!design`
* `!preflight`
* `!start-autoresearch`
* `!launch-batch`
* `!decide-batch`

## Deprecated as current product truth

### Literature-first product messaging

Deprecate:

* broad literature search as product identity
* start-literature-search as the main product story
* open-ended harvester promises

Replace with:

* source intake and review
* bounded evidence gathering
* experiment-first operator loop

### JSON-backed metadata store as steady state

Deprecate:

* JSON-on-artifacts-share as the long-term metadata brain

Replace with:

* Postgres-backed workflow/session store

### Broad `latest` messaging

Deprecate:

* describing `latest` aliases as the normal operator-facing semantics

Replace with:

* sender-pinned session semantics
* explicit session-id where needed

### OpenClaw-first and old Ollama-first current-state narratives

Deprecate:

* OpenClaw as primary operator shell
* old `.12`/`.23` Ollama model paths as current product truth

Keep only as:

* historical notes
* fallback/reference material

## First-pass rewrite targets

### Rewrite current-state summaries

* `README.md`
* `docs/glasslab-v2/README.md`
* `docs/glasslab-v2/overview.md`

### Mark historical or deprecated

* `docs/glasslab-v2/operator-access-recommendation.md`
* `docs/glasslab-v2/openclaw-gateway.md`
* `docs/glasslab-v2/ollama-native-openclaw.md`
* `docs/glasslab-v2/resume-next-session-2026-03-24.md`
* `docs/glasslab-v2/live-state-2026-03-28.md`
* `docs/glasslab-v2/live-state-2026-03-30.md`

### Demote literature-first messaging

* `docs/glasslab-v2/research-assistant-implementation-checklist.md`
* `docs/glasslab-v2/research-assistant-infra-proposal.md`
* `docs/glasslab-v2/research-assistant-ux-boundary.md`
* `docs/glasslab-v2/external-literature-path.md`

## Near-term execution order

1. rewrite current summary docs around the canonical stack
2. add and document the new command surface
3. mark OpenClaw-first and Ollama-first notes as secondary or historical
4. implement Postgres store support for session/workflow records
5. demote `latest` from operator-facing docs
6. keep literature as intake/review, not headline product identity

## Bottom line

Glasslab should present:

* one canonical control surface
* one canonical record store
* one canonical experiment loop
* one honest statement about literature quality

It does not need more choices presented as equal.
