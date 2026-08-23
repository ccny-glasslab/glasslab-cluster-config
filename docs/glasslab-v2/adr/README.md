# ADRs

Architecture decision records for Glasslab v2 live here.

Current decisions:

- `0001-session-skill-execution-model.md`: sessions are the primary research object, skills are bounded backend capabilities, and workflow families are execution templates
- `0002-hermes-agent-runtime-pilot.md`: proposed, disabled-by-default pilot of Hermes as the inner Honeydew/Beaker runtime
- `0004-task-fabric-authorities.md`: PostgreSQL stays authoritative while RabbitMQ/Celery is delivery-only infrastructure with per-service transactional outboxes, leased/fenced claims, at-least-once Discord delivery, and a single-node (persistent, not highly available) broker

Use these records to understand why the repo is shaped the way it is, not just what the current manifests or APIs look like.
