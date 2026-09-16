# CI Policy 2026-07

Status: current policy

Date: 2026-07-23

The default CI signal should answer one question:

> Did this commit break the current Glasslab run-fabric path?

It should not fail a push because an old debug manifest, archived adapter, or
historical product path has drifted.

## Current Push CI

Required push coverage:

- Python syntax across `services/`
- full `workflow-api` tests
- current YAML/JSON syntax validation
- local Markdown link validation for docs changes
- shell syntax validation for script changes

The current operator path is:

```text
OpenCode -> repo-owned scripts -> workflow-api -> Kubernetes Jobs
```

So default CI should prioritize:

- `services/workflow-api`
- `services/workflow-registry`
- `kubeadm/glasslab-v2` current manifests
- repo-owned scripts used by the current path

## Manual / Compatibility CI

Adapters and older product surfaces still have tests, but they are not the
default push signal:

- WhatsApp gateway
- research ingress
- research command router
- evaluator service
- reporter service
- contrastive-learning runner

Run them with the manual `CI Python` workflow when changing those areas.

## Config Validation

Config validation lives in:

```bash
scripts/validate-configs.py
```

It parses current YAML and JSON files and deliberately ignores backup files such
as `*.bak`, `*.bak2`, and `*.bak3`.

Do not reintroduce large inline CI scripts that scan every artifact-shaped file
without an explicit policy.

## Local Contributor Check

Contributors should run:

```bash
./scripts/check-before-push.sh
```

The script mirrors the default CI signal and can be narrowed with:

```bash
./scripts/check-before-push.sh --docs
./scripts/check-before-push.sh --configs
./scripts/check-before-push.sh --python-core
```

Keep this script small enough that contributors actually run it before pushing.

## Cleanup Direction

As compatibility surfaces are removed, delete their manual matrix entries and
their old tests in the same commit.

Prefer one clear test lane over several overlapping partial lanes.
