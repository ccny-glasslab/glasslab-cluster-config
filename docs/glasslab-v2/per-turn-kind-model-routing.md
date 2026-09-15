# Per-Turn-Kind Model Routing (Issue #433)

Last updated: 2026-09-14

Glasslab's split serving runs two co-resident models:

| Role | Model | Host |
|---|---|---|
| `structured` | `mlx-community/Qwen3-Coder-Next-4bit` | `192.168.1.17:52417` |
| `reasoning` | `mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit` | `192.168.1.18:52417` |

Which role serves a turn is a **recorded, evidence-derived decision**, not a
hand-coded per-agent constant. Issue #433 replaced the static
"verification on the reasoning model, everything else on the structured model"
assignment with a routing table derived from per-turn-kind pass rates.

## Decision

Every `TurnKind` is routed to the role that scored best on the frozen
fixtures. The recorded mapping:

| Turn kind | Role |
|---|---|
| `task_spec` | `structured` |
| `protocol_draft` | `structured` |
| `contract_candidate` | `structured` |
| `implementation_plan` | `structured` |
| `implementation_proposal` | `structured` |
| `methodology_review` | `structured` |
| `revision` | `structured` |
| `experiment_analysis` | `structured` |
| `verification` | `reasoning` |
| `final_report` | `structured` |
| `research_answer` | `structured` |

The mapping lands on the same serving split the 2026-09-07 rehearsal used, but
it is now reproducible from evidence and can change without editing engine
logic.

## Evidence source

All three artifacts are versioned and reviewed like code, under
`services/research-orchestrator/fixtures/model-routing/v1/`:

- `fixtures.json` — one frozen, stable input per `TurnKind` (the fixed unit
  the benchmark scores).
- `evidence.json` — the recorded `(turn_kind, role, model, passed, samples,
  pass_rate)` rows. Each row names its `source` (for example, the #321
  envelope-validity baseline, or commit `e5a5b84` where the Thinking model
  exhausted its KV cache on long-context structured turns).
- `routing_table.json` — the derived `turn_kind -> role` table. It is generated
  from `evidence.json`, never edited by hand.

`app/model_routing.py` owns loading and the deterministic derivation:

- `score_envelope(fixture, envelope)` — a response counts as a pass only if it
  parses to an `AgentTurnResult` envelope with the fixture's required `kind`,
  a non-empty `summary`, and every required variant field.
- `derive_routes(measurements)` — per turn kind, the role with the highest
  `(pass_rate, samples)` wins; exact ties fall back to `structured` first
  (`ROLE_ORDER`). Roles outside the two configured roles are ignored, and a row
  with zero samples is not evidence.

`app/config.py` resolves a route to a concrete model: the `structured` role
uses `honeydew_structured_agent_model` for Honeydew and the per-agent Beaker
model for Beaker; the `reasoning` role uses
`honeydew_reasoning_agent_model`. A missing or malformed routing table falls
back to the legacy split, so a bad path cannot strand a run.

## Refreshing the evidence

Recording requires live model servers; the routing and its tests do not. From
the service root:

```bash
PYTHONPATH=. python3 scripts/record_model_routing.py \
  --base-url http://192.168.1.17:52417/v1 \
  --models structured=mlx-community/Qwen3-Coder-Next-4bit,reasoning=mlx-community/Qwen3-Next-80B-A3B-Thinking-4bit \
  --turns 3
```

This rewrites `evidence.json` and `routing_table.json`. `--replay <dir>` scores
a recorded-response corpus instead of a live endpoint. The offline
`tests/test_model_routing.py` asserts that the committed routing table equals
the derivation from the committed evidence, so a table that drifts from its
evidence fails CI.

## Related

- Issue #319/#321/#355: per-agent model config plumbing and the initial
  envelope/grounding benchmark.
- `docs/glasslab-v2/model-selection.md`: hardware/quantization decision
  framework.
- `docs/research-orchestrator.md`: runtime and split-serving deployment state.
