# Model-routing fixtures and evidence (v1)

Versioned, reviewed inputs for evidence-driven per-turn-kind routing
(issue #433). Do not edit these by hand except to add a new version directory.

## Files

- `fixtures.json` — one frozen, stable input per `TurnKind`. Each fixture
  declares the required envelope kind and the variant fields a valid response
  must carry.
- `evidence.json` — the recorded `(turn_kind, role, model, passed, samples,
  pass_rate, source)` rows. Every row names the evidence it was recorded from.
- `routing_table.json` — the derived `turn_kind -> role` table consumed by
  `app/config.py`. Generated from `evidence.json`; never hand-edited.

## Schema versions

- `glasslab-model-routing-fixtures-v1`
- `glasslab-model-routing-evidence-v1`
- `glasslab-model-routing-table-v1`

## How the routing table is derived

`app/model_routing.py::derive_routes` picks, per turn kind, the measured role
with the highest `(pass_rate, samples)` rank. Exact ties fall back to
`ROLE_ORDER` (`structured` first). Rows with zero samples and roles outside the
two configured roles are ignored. `tests/test_model_routing.py` asserts that
`routing_table.json` equals this derivation from `evidence.json`.

## Regenerating

Recording requires live model servers; routing and its tests do not. From the
service root:

```bash
PYTHONPATH=. python3 scripts/record_model_routing.py \
  --base-url http://192.168.1.17:52417/v1 \
  --models structured=mlx-community/Qwen3-Coder-Next-4bit,reasoning=mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit \
  --turns 3
```

Use `--replay <dir>` to score a recorded-response corpus
(`<fixture_id>__<role>__<index>.txt`) with no network. See
`docs/glasslab-v2/per-turn-kind-model-routing.md` for the decision record.
