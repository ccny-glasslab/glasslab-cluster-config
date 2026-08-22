# Research Orchestrator

This service coordinates Honeydew and Beaker as separate agent runtimes.
It owns durable research state, approvals, policy, job reconciliation, event
history, and report acceptance. It delegates bounded execution to
`workflow-api`; it does not give either agent Kubernetes credentials.

OpenCode remains the live default. An experimental Hermes adapter can be
selected explicitly with:

```text
GLASSLAB_ORCHESTRATOR_AGENT_RUNTIME_BACKEND=hermes
```

That switch is not sufficient for deployment: the image must first contain a
reviewed, pinned Hermes executable and satisfy the isolation gates in
[`../../docs/glasslab-v2/adr/0002-hermes-agent-runtime-pilot.md`](../../docs/glasslab-v2/adr/0002-hermes-agent-runtime-pilot.md).

Local checks:

```bash
python -m pip install -r requirements-dev.txt
PYTHONPATH=. pytest -p no:cacheprovider -q
PYTHONPATH=. python -m app.smoke
```

The smoke path uses scripted OpenCode output, a fake cluster executor, the
repository example evaluation contract, and disabled Discord.

For the one-time SQLite-to-Postgres migration, pass the DSN through the
existing service environment or an already-open private descriptor, never an
argument:

```bash
exec {postgres_dsn_fd}< /secure/path/research-orchestrator-postgres-dsn
python3 scripts/import-sqlite-store-to-postgres.py \
  --sqlite-path /secure/path/orchestrator.db \
  --dsn-fd "$postgres_dsn_fd" \
  --apply
exec {postgres_dsn_fd}<&-
```

`GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN` is the supported environment mode.
The historical `--postgres-dsn` option is rejected without echoing its value.

The agent model is selected with
`GLASSLAB_ORCHESTRATOR_AGENT_MODEL_PROVIDER_ID` and
`GLASSLAB_ORCHESTRATOR_AGENT_MODEL_NAME`. The live manifest temporarily uses
`opencode/big-pickle`; it requires `OPENCODE_API_KEY` in the orchestrator
Secret. The local exo/Qwen settings remain the explicit rollback target and are
not used as a silent per-turn fallback. Big Pickle uses prompt-delimited JSON
because its thinking mode rejects OpenCode's forced JSON-schema tool choice;
the orchestrator still validates every result against `AgentTurnResult`. The
service image pins OpenCode `1.18.14`; older `1.4.x` tool definitions are not
compatible with the Zen Big Pickle endpoint.

Generic task archives are compiled by Honeydew into a validated TaskSpec and
then mapped by deterministic policy to fixed CPU or GPU workspace profiles.
Use `/task-start` in Discord or `POST /task-bundles/import`; inspect
`GET /task-bundles/{task_id}/preflight` before creating a run.

See [`../../docs/research-orchestrator.md`](../../docs/research-orchestrator.md)
for the architecture, trust boundaries, deployment state, and limitations.
