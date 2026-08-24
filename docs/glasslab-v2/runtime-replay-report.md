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

The fixture bytes were captured from the live #98 corrected-feedback A/B replay
(2026-08-23) and are pinned by `MANIFEST.json` sha256s:

| artifact | source |
|---|---|
| `PROMPT.txt` | exact corrected feedback delivered in the A/B (includes the #196 appendix with the literal `"src/train"` YAML example corrected by #202) |
| `workspace/configs/candidate.yaml` | pre-repair config as both candidates received it |
| `gold_repair.diff` | canonical repair (regenerated zero-fuzz against the committed pre-repair base; same six-line `"src/train"` block Ox applied live) |
| `contract/classification-metric-v1/1.0.0/` | real sealed contract-candidate bytes used by the live gate |

## Recorded observations

Both rows below are **manual captures** from the live A/B (`capture_mode:
manual_live_ab_2026-08-23`); they predate the harness, so tool-call/token fields
that the harness now records are honestly `null`.

| candidate | wall clock | terminal outcome | correct? | notes |
|---|---|---|---|---|
| `exo/mlx-community/Qwen3-Coder-Next-4bit` | ~1560 s (wrapper timeout; work completed ~1590 s) | timeout → post-hoc verified | **yes** | exo transport streamed nothing before cutoff; process finished the correct repair locally |
| `opencode-go/ox-alpha-free` | ~285 s | accepted | **yes** | one revision cycle |

## What these observations do NOT establish

- They do not show Qwen is incapable. Before the feedback correction (#194,
  #196, #202), *both* candidates failed the same unsatisfiable example; after
  correction, *both* produced repairs that the real deterministic gate accepts.
- They do not support a general model-quality ranking. n=1 trial per candidate,
  single frozen task, single day, shared transport conditions.
- The Qwen latency figure includes a transport stall and is not a clean
  model-latency measurement.
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
  --timeout-seconds 1800
```

Requirements and boundaries: candidates are explicit CLI arguments only;
`--out-dir`/`--timeout-seconds` are required (no silent defaults); each trial
gets a fresh `HOME` and pristine workspace; raw JSONL rows land in
`observations.jsonl`; the summary never declares a winner. Ox is callable via
the interactive OpenCode provider on operator workstations; it is not wired into
the deployed orchestrator runtime path (that would require an explicit
`OPENCODE_API_KEY` secret and egress, which has deliberately not been done).
