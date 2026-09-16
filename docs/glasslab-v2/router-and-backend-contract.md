# Router And Backend Contract

> **Status: historical.** `whatsapp-gateway`, `research-ingress`, and
> `research-command-router` were retired under issue #159 (the #173 workflow-api
> auth work landed and #289 removed the router's caller). This document is kept
> for historical context; see `historical/README.md`.

This document defines the command-routing contract between:

* `whatsapp-gateway`
* `research-ingress`
* `research-command-router`
* `workflow-api`

## Boundary rule

The router is not a workflow engine.

The router should:

* match commands
* resolve pinned session context
* dispatch one backend-owned action
* return a compact operator response

The router should not:

* improvise multi-step orchestration
* become a second backend control plane
* expose broad internal pipeline stages as normal command flow

## Canonical primary-command ownership

### `!new`

Owner:

* `workflow-api`

Effect:

* create a session
* optionally stage initial source-intake/search bootstrap if enabled

Suggested endpoint:

* `POST /research-sessions`

Compatibility alias handling:

* `!start` maps to `!new`

### `!state`

Owner:

* `workflow-api`

Effect:

* return compact session context summary

Suggested endpoint:

* `GET /research-sessions/{session_id}/context`

Compatibility alias handling:

* `!status` maps to `!state`

### `!add`

Owner:

* `workflow-api`

Effect:

* create source, note, dataset, or baseline attachment record

Suggested endpoint family:

* `POST /research-sessions/{session_id}/intake`

The request body should carry a normalized typed payload, for example:

* `source_url`
* `document_url`
* `note`
* `dataset_uri`
* `baseline_name`

The router may parse prefixes like:

* `note:`
* `dataset:`
* `baseline:`

### `!plan`

Owner:

* `workflow-api`

Effect:

* create or refresh current design draft

Suggested endpoint:

* `POST /research-sessions/{session_id}/transitions/prepare-current-plan`

The backend may reuse current design-skill internals, but the operator contract should be plan-oriented.

### `!check`

Owner:

* `workflow-api`

Effect:

* run design preflight/readiness without launching execution

Suggested endpoint:

* `GET /research-sessions/{session_id}/preflight/current-plan`

### `!run`

Owner:

* `workflow-api`

Effect:

* run the happy-path transition from pinned session to run record

Current acceptable endpoint:

* `POST /research-sessions/{session_id}/transitions/run-happy-path`

The router should make exactly one backend call for this command.

### `!compare`

Owner:

* `workflow-api`

Effect:

* compare the current relevant runs or campaign results

Current acceptable endpoint family:

* `GET /research-sessions/{session_id}/autoresearch-model-comparison`

Future cleanup may rename this to a more general comparison path.

### `!decide`

Owner:

* `workflow-api`

Effect:

* persist a human decision record for the current result set

Suggested endpoint:

* `POST /research-sessions/{session_id}/decisions/current`

### `!next`

Owner:

* `workflow-api`

Effect:

* advance the current bounded campaign
* draft variants if needed
* record decisions if eligible
* launch next bounded iterations

Current acceptable endpoint:

* `POST /research-sessions/{session_id}/transitions/advance-autoresearch`

## Gateway behavior

### `whatsapp-gateway`

Owns:

* sender transcript persistence
* sender/session pinning
* attachment normalization
* duplicate suppression
* direct forwarding of deterministic commands to `research-ingress`
* deterministic rejection of unsupported turns

Should not own:

* workflow planning
* multi-step experiment logic

### `research-ingress`

Owns:

* normalization of inbound control messages
* dispatch to deterministic router
* deterministic response shaping for supported and unsupported turns

Should not own:

* backend orchestration logic

### `research-command-router`

Owns:

* command recognition
* argument parsing
* session-aware routing
* exactly one backend-owned action per primary command
* stable operator-facing response text
* deterministic rejection for unsupported turns

Should not own:

* plan generation
* run preparation internals
* campaign logic
* literature search strategy

## Session semantics

Primary rule:

* router dispatch should prefer pinned session id

Compatibility rule:

* `latest` may remain internally available
* primary operator flows should not be documented around `latest`

## Error contract

Backend errors should come back as:

* compact operator-readable explanation
* explicit route marker indicating backend-owned error
* no fake "API unreachable" language when the backend returned a real validation error

Example good error:

* `Current plan is not ready_for_run: dataset_uri is unresolved.`

## Response shape guidance

Each primary command should return:

* one short explanation
* one current object reference where relevant
* one suggested next action where useful

Do not return:

* internal route graphs
* implementation-stage details unless debug mode is requested

## Router implementation priority

1. keep current deterministic happy path for the five primary commands
2. add `!plan`, `!check`, `!add`, and `!decide`
3. demote literature/debug commands from headline documentation
4. reject unsupported turns deterministically instead of inventing fallback paths
