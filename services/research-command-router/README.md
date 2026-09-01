# Research Command Router

> **Deprecated — legacy service, not the current research front door.**
> This service describes the `!new`/`!plan`/`!run` command surface that has been
> superseded by the Honeydew/Beaker Discord slash-command interface. It is
> retained for historical context only. See
> [`docs/research-orchestrator-command-surface.md`](../../docs/research-orchestrator-command-surface.md)
> for the current operator command reference.

This service is the deterministic front door for the supported Glasslab command
surface.

It owns explicit command traffic such as:

- `!new <goal>`
- `!state`
- `!add <thing>`
- `!plan`
- `!check`
- `!run`
- `!compare`
- `!decide <keep|discard|revise>`
- `!next`
- `!help`

The current contract is:

- `POST /dispatch` with the inbound user message
- command router matches a supported explicit command
- router calls `workflow-api` directly
- router returns `response_text` suitable for chat plus the structured backend payload
- unsupported or non-command text returns a deterministic rejection that points
  the user back to `!help`

This service intentionally does not provide free-form chat fallback.
