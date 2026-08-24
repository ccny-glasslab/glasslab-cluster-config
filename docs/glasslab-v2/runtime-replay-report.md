# Runtime Replay Report — wine-classification-v1 (run #98 frozen case)

Status: baseline report for the bounded agent-runtime replay facility.
Case fixture: `services/research-orchestrator/fixtures/runtime-replay/wine-classification-v1/`
Harness: `services/research-orchestrator/scripts/replay-runtime-benchmark.py`
Raw observations: `docs/glasslab-v2/runtime-replay/wine-classification-v1-run98-observations.jsonl`

## Purpose and scope

This facility exists so agent-runtime comparisons stop being anecdotes. It replays
one frozen workspace-repair case against explicitly named candidate runtimes:

- identical frozen task input (pre-repair `configs/candidate.yaml`), identical
  prompt (the corrected-feedback text from the #98 methodology loop), identical
  acceptance condition;
- per-trial isolated `HOME` and a pristine workspace copy, so no candidate can
  observe another candidate's outputs;
- correctness scored **only** by the real deterministic preflight gate
  (`app.preflight.preflight_matrix`) plus frozen expected comparisons — never by
  file-hash equality, never by latency;
- replay is bounded: it stops at workspace acceptance. It never touches
  workflow-api, cluster jobs, or any live scientific run.

## Provenance

The fixture is a **captured replay fixture derived from the live #98
corrected-feedback case**. Its bytes are pinned going forward by `MANIFEST.json`
sha256s (`asserted_at_capture`); this proves immutability after capture.

What provenance establishes and what it does not:

| claim | status |
|---|---|
| `PROMPT.txt` bytes were passed verbatim as the CLI prompt argument to **both** manual A/B candidate invocations | established — preserved session transcripts record quoted `"$(cat …/ab_prompt.txt)"` in every recorded invocation, and PROMPT.txt carries no trailing newline so quoting is lossless |
| prompt content derives from the live #98 corrected-feedback lineage (#196 appendix as corrected by #202; the same resolution mechanics verified delivered live in run `4be29763…` event 163) | established |
| fixture bytes are byte-identical to any single orchestrator-delivered **live turn** prompt | **NOT established** — orchestrator-delivered turns carry different wrapper text (structured-output suffix); the fixture is the standalone-replay form of that feedback |
| pre-repair config equals the state both candidates received in the A/B | established via the preserved round-1 capture |

## Timing corrections (2026-08)

Earlier informal write-ups misstated these figures. Corrected values:

| candidate | wall clock | terminal outcome | correct? |
|---|---|---|---|
| `exo/mlx-community/Qwen3-Coder-Next-4bit` | wrapper timeout at **1560 s (~26 min)**; the correct workspace repair appeared ~1590 s (~26.5 min) after start per recorded observation | timeout → post-hoc verified | **yes** |
| `opencode-go/ox-alpha-free` | **285 s (= 4 min 45 s)** | accepted | **yes** |

Notes on these numbers:

- 1560 s was the **manual A/B wrapper budget** (`timeout` around the CLI call),
  not a production limit; the deployed orchestrator turn budget is separately
  configured (1800 s). Do not read the Qwen row as "production would time out".
- Both repairs pass the same deterministic preflight scorer.
- Earlier informal write-ups understated Ox's wall clock; the recorded
  observation is 285 s and must be read as four minutes forty-five seconds,
  not as an approximate sub-five-minute rounding.

## Causal scope

The methodology failures under the defective prompt do not demonstrate Qwen
incapability; after correction both candidates solved the frozen task.

This statement is deliberately narrow. It covers the six failed
methodology-revision cycles recorded under the defective feedback. It does
**not** attribute other historical observations — such as sessions aborted for
repeated invalid tool names — to the feedback defect, and it does not claim
that a corrected prompt demonstrates capability. n=1 per candidate supports no
general conclusion in either direction.

## Completion-path stall attribution

The Qwen observation records a **completion-path/streaming stall observed
while using exo**: no observable completion output arrived before the wrapper
timeout while the workspace eventually contained the correct repair. The
failing layer is **not yet identified** — candidates include the exo server,
the OpenAI-compatible streaming layer, the provider adapter, OpenCode CLI
buffering, or another transport boundary. Attribution requires exo-side
evidence; tracking issue #218 stays open until then.

## Usage-counter semantics

Observation schema v2 separates three things earlier prose blurred:

| field | meaning |
|---|---|
| `tool_call_count` | terminal tool parts (completed or errored) in the trial's session database |
| `tool_error_count` | terminal tool parts whose execution errored — **valid tools can appear here** (a preserved live session shows a valid `grep` erroring on an infrastructure failure) |
| `invalid_tool_call_count` | populated **only** when an errored part's text explicitly indicates the runtime rejected an unknown/invalid tool name (pattern list in `app/runtime_replay.py`); otherwise `None`, which means *unknown*, never zero |
| `doom_loop_event_count` / `doom_loop_threshold` | repeated-identical-terminal-tool-call events and the threshold in effect; count is `null` unless a threshold was specified for that trial |
| `revision_cycles` | discrete repair/revision passes where observable; null when not |
| `session_db_layout` | which opencode storage layout produced the usage data (`xdg` / `legacy`) |

The committed run98 rows are manual captures that predate the harness: their
usage fields are honestly `null` (no session database was captured), and the
original v1 rows remain viewable in git history.

## Doom-loop threshold provenance

The deployed orchestrator path has used a repeated-tool limit of **6**
(`GLASSLAB_ORCHESTRATOR_OPENCODE_REPEATED_TOOL_LIMIT` in the research-
orchestrator configmap). The merged #217 harness hardcoded 4; that silent
default is removed. The threshold is now explicit per campaign (CLI flag or
case manifest) and every observation records the threshold next to its count.
The committed manual-A/B rows carry `doom_loop_event_count = null` because no
parts were captured at all, so no threshold-scoped count exists for them.

## Candidate environment isolation

Trials never inherit the operator environment wholesale. Each trial runs with
a fresh `HOME` and a minimal environment; additional variables pass through
only when named explicitly (`--env-pass NAME`, repeatable), and trial-scoped
keys (`PATH`, `HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`) can never be
overridden via the allowlist. Provider credentials remain explicit operator
inputs: `--seed-auth-file PATH` copies an operator-supplied auth file into each
trial HOME (both known layouts, `0600`) where ordinary trial cleanup removes
it. Tests prove unlisted environment variables do not reach the subprocess.

Because provider credentials were not available in environment-variable form,
no live smoke trial is asserted from CI or this report; the harness's
end-to-end path is exercised by fake-runner tests, and any live smoke result
must be produced by an operator running the documented command.

## Non-claims

- These observations do not show Qwen is incapable. Before the feedback
  correction (#194, #196, #202), *both* candidates failed the same
  unsatisfiable example; after correction, *both* produced repairs that the
  real deterministic gate accepts.
- They do not support a general model-quality ranking. n=1 trial per candidate,
  single frozen task, single day, shared conditions.
- The Qwen wall-clock includes a completion-path/streaming stall whose failing
  layer is unidentified; it is not a clean latency measurement, and slower was
  not wrong.
- No cluster execution occurred in either arm; nothing here evaluates job-time
  scientific behavior.
- Faster was not better and slower was not wrong: correctness and latency are
  separate columns and must stay that way.

## Reproducing / extending

```bash
cd services/research-orchestrator
PYTHONPATH=. python ../scripts/replay-runtime-benchmark.py \
  --candidate exo/mlx-community/Qwen3-Coder-Next-4bit \
  --candidate opencode-go/ox-alpha-free \
  --repeats 3 \
  --out-dir /tmp/runtime-replay-out \
  --timeout-seconds 1800 \
  --doom-loop-threshold 6
```

Requirements and boundaries: candidates are explicit CLI arguments only;
`--out-dir`/`--timeout-seconds` are required (no silent defaults); each trial
gets a fresh `HOME` and pristine workspace; raw JSONL rows land in
`observations.jsonl`; the summary never declares a winner. Ox is callable via
the interactive OpenCode provider on operator workstations; it is not wired into
the deployed orchestrator runtime path (that would require an explicit
`OPENCODE_API_KEY` secret or seeded auth file plus egress, which has
deliberately not been done).
