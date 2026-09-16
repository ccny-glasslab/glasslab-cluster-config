# Research Orchestrator Deployment

These manifests define one orchestrator replica with no Kubernetes API token.
The service calls `workflow-api` for bounded execution and stores SQLite WAL,
worktrees, and run artifacts on `glasslab-shared-artifacts`.

The init container maintains a detached checkout of the one approved public
repository. Production rollout still requires:

- a published orchestrator image matching the manifest tag
- live exo reachability from `node05`
- a published evaluation-contract image configured in the workflow-api trusted
  catalog
- a local secret containing a generated operator API token
- a local secret containing the Discord bot token and channel webhook URL when
  Discord is enabled

Discord application, guild, and channel IDs are non-secret values in the
ConfigMap. The bot must be installed in the guild and have View Channel, Send
Messages, Read Message History, Create Public Threads, and Send Messages in
Threads on the configured channel. The installation must include the
`applications.commands` OAuth2 scope so the guild-scoped `/task-start`
command can be registered. `/research-start` and `/benchmark-start` are
retired and are not registered. Administrator is not
required. Approval
buttons are received over an outbound Gateway connection and are authorized
against the immutable role or user IDs configured in the ConfigMap. The live
configuration uses the `Mystic Arts Masters` role; changing that role's
membership changes who may approve or reject Glasslab actions.

Do not treat the example Discord secret as deployable.

New task archives are compiled into fixed CPU/GPU profiles and automatically
preflighted. The `scripts/stage-ml-benchmark-datasets.sh` helper is needed only
for the three legacy benchmark records.
