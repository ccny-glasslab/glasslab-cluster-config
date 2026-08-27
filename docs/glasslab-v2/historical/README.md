# Historical Docs

These documents describe systems and interfaces that have been superseded by
the current Honeydew/Beaker research orchestrator and Discord command surface.
They are preserved for context and are not authoritative for current operation.

For current architecture read `AGENTS.md` and `docs/glasslab-v2/current/README.md`.

## OpenClaw (deprecated gateway)

- [openclaw-deprecation-and-custom-whatsapp-plan.md](openclaw-deprecation-and-custom-whatsapp-plan.md) — migration plan when OpenClaw was retired
- [next-no-arg-operator-actions.md](next-no-arg-operator-actions.md) — no-arg operator surface design for OpenClaw
- [no-arg-vs-argumented-tools.md](no-arg-vs-argumented-tools.md) — interface design decision for OpenClaw commands
- [custom-chat-shell-plan.md](custom-chat-shell-plan.md) — plan to replace OpenClaw with a custom chat shell

## WhatsApp (deprecated transport)

- [whatsapp-dedicated-account-migration.md](whatsapp-dedicated-account-migration.md) — WhatsApp dedicated account migration plan
- [research-ingress.md](research-ingress.md) — research-ingress service contract, deterministic ingress layer in front of research-command-router

## Old Command Surface (`!new`, `!plan`, `!run` era)

- [research-command-router.md](research-command-router.md) — deterministic command matcher for the `!new`/`!plan`/`!run` surface
- [research-session-cli.md](research-session-cli.md) — CLI session interface predating the orchestrator
- [autoresearch-lane.md](autoresearch-lane.md) — bounded autoresearch loop concept
- [autonomous-research-lane.md](autonomous-research-lane.md) — autonomous research lane planning
- [autoresearch-orchestration-plan.md](autoresearch-orchestration-plan.md) — orchestration plan predating the current research orchestrator

## Titanic (legacy v1 stack)

- [titanic-agent-stack.md](titanic-agent-stack.md) — legacy v1 FastAPI agent stack, preserved as reference
