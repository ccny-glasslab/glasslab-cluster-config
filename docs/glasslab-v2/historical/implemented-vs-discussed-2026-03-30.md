# Implemented vs Discussed 2026-03-30

This note is the short answer to a specific problem from the 2026-03-30 session:

- some of the conversation was exploratory
- some of it was actual implementation
- this document separates the two

## What Was Actually Implemented

### Live on `.44`

These were validated from the provisioner and are not just repo ideas.

- `workflow-api` is live at:
  - `ghcr.io/ccny-glasslab/glasslab-workflow-api:0.1.54-local`
- `/healthz` reported:
  - `build_source_revision: 41cf6b6`

That live build includes:

- autoresearch model-comparison summary
  - `recommended_model`
  - `model_comparison`
- execution preflight split/overfitting checks
- validation-split variants in the experiment path

### Live experiment-path pieces

The following backend spine is materially real:

- literature / intake state
- interpretation
- methodology variants
- bounded validation iteration launch
- run comparison
- keep/discard/review decision records

What was specifically validated:

- create autoresearch campaign
- draft methodology variants
- launch bounded iteration
- attach synthetic run metrics/status
- decide latest iteration as `keep`

### Live coding-model notebook refinement

This was implemented and validated live.

- backend:
  - `.12` native Ollama
- model:
  - `qwen2.5-coder:14b`
- output:
  - refined notebook artifacts written under the shared artifacts volume

Sample exported to the repo:

- [analysis_notebook_refined_sample.ipynb](/home/gr66ss/cluster-config/docs/glasslab-v2/examples/analysis_notebook_refined_sample.ipynb)

Important reality note:

- the notebook path works
- the content quality is still close to the deterministic scaffold

### Live interpretation-agent path

This was also implemented and validated as a bounded backend lane.

- `workflow-api` calls `interpretation-agent`
- primary backend:
  - `.23` `qwen3:30b`
- fallback backend:
  - `.12` `qwen3:14b`
- final fallback:
  - deterministic interpretation scaffold

Important reality note:

- the lane is live
- it is still somewhat latency-sensitive
- deterministic fallback still matters

## What Was Implemented In Repo But Not Fully Revalidated Live

These are real code changes, but they were not closed out as live-validated during the session.

### Session-scoped intake creation

This was the main unfinished code slice at the end of the session.

Implemented in repo:

- `POST /research-sessions/{session_id}/intakes`
- `POST /research-sessions/latest/intakes`

Why it matters:

- the generic `/intakes` route does not bind session intake state cleanly enough for the fully session-first experiment path
- the session-scoped path is the correct fix

Status at end of session:

- repo change written
- focused test passed locally
- live rollout had started as `0.1.55-local`
- not revalidated to completion before the session ended

### Autoresearch smoke-test helper

Added in repo:

- [smoke-test-autoresearch.sh](/home/gr66ss/cluster-config/scripts/smoke-test-autoresearch.sh)

Purpose:

- exercise the bounded autoresearch lane end to end
- avoid reconstructing the same API sequence manually

Status at end of session:

- script existed
- helper assumptions were being corrected
- not yet committed / closed out as a finished validated tool

## What Was Discussed But Not Actually Implemented

These were legitimate design directions, but they were not made real in the repo/live state during that session.

- using the huge `.21` model in the Glasslab experiment path
- making the system generally solve arbitrary research problems without more tightening
- turning OpenClaw into the primary workflow brain
- any unconstrained or free-form “autonomous scientist” behavior
- robust same-artist vs different-artist painting methodology comparison beyond notes and framing

## Practical Summary

The important truth is:

- this was not just a brainstorming session
- a real bounded experiment spine was implemented

But the equally important truth is:

- the session-first intake seam was still unfinished at the end
- some larger ambitions remained design intent, not shipped capability

If you want the shortest accurate summary:

- implemented:
  - bounded experiment backbone
  - interpretation lane
  - notebook refinement lane
  - model-comparison summary
  - split-aware execution preflight
- not fully finished:
  - clean session-scoped intake binding
  - polished end-to-end autoresearch smoke path
- not implemented:
  - broad autonomous research behavior
  - `.21` large-model integration into Glasslab itself
