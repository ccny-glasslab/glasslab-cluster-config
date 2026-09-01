# Research Ingress

`research-ingress` is the repo-owned ingress layer in front of the deterministic
command router.

Its job is narrow:

- accept normalized inbound control messages
- call `research-command-router`
- return deterministic operator text

It does not provide conversational fallback.

## Contract

- `POST /inbound`
- request:
  - `message`
  - `sender`
  - `channel`
  - optional `session_id`
- response:
  - `handled`
  - `route`
  - `response_text`
  - `router_payload`

## Current behavior

- supported commands return `route=deterministic-router`
- unsupported and non-command turns return `route=unsupported-turn`
- both cases return a final user-facing response directly

## Deterministic command path

The supported command loop executes through:

- `whatsapp-gateway`
- `research-ingress`
- `research-command-router`
- `workflow-api`

That path is backend-owned and deterministic.

## Operator note

This service is a routing seam, not a backend control plane. If a turn needs to
change workflow state, the backend action should live in `workflow-api`, not in
`research-ingress`.
