# Workflow API Test Triage 2026-04-28

This note records the state after the Postgres/pgvector storage pass and the
transition-route import fix.

## Fixed In This Pass

- `transition_routes.py` now imports cleanly when `app.main` is imported.
- `job_submission._build_runner_spec` no longer references an out-of-scope
  `settings` name during preflight validation.
- Focused GPU run creation tests that were failing with
  `workflow submission contract is not implemented: name 'settings' is not
  defined` now pass.

## Current Test State

Passing:

```bash
python3 -m compileall services/workflow-api/app services/workflow-api/scripts
python3 -m pytest services/workflow-api/tests/test_persistence.py -q
python3 -m pytest \
  services/workflow-api/tests/test_api.py::test_gpu_technique_card_can_fill_executable_contract \
  services/workflow-api/tests/test_api.py::test_gpu_technique_card_design_can_launch_run \
  services/workflow-api/tests/test_persistence.py -q
```

Broader API subset:

```bash
python3 -m pytest services/workflow-api/tests/test_persistence.py services/workflow-api/tests/test_api.py -q
```

Result after the fixes:

```text
116 passed, 13 failed
```

## Remaining Failure Themes

- Literature/design routes infer source URLs as dataset inputs in some paths
  where tests expect `s3://datasets/...`.
- A few tests assert older exact error text.
- One preflight test expects an older GPU runner image tag
  `0.1.4-local` while the registry now reports `0.1.7-local`.
- Session execution preflight warning expectations no longer match the current
  warning payload.

These are behavior-contract and test-expectation issues separate from the
state/storage cutover. They should be handled in a focused workflow-api
behavior cleanup pass before using the full API test file as a release gate.
