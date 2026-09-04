# Model Selection on the exo Pair

Last verified: 2026-09-02

How to choose models for the 2x64 GB exo pair (192.168.1.17:52415) and what the
tradeoffs are. Written for agents and operators deciding Honeydew/Beaker model
configuration (issues #319, #321).

## Hardware envelope

- 2x Macs, 64 GB unified memory each (exo17 master / exo18 worker).
- macOS + exo overhead leaves roughly **48-52 GB usable per Mac** for weights.
- Generation is **memory-bandwidth-bound**: a model reads its weights per
  token, so smaller footprint = faster tokens, all else equal.
- exo shards one model across both Macs (2-node Pipeline + MlxJaccl over
  RDMA) or runs single-node fallback. A model that fits one Mac can run
  single-node with zero cross-node traffic.
- The reconcile daemon (`glasslab-exo-reconcile`) currently manages the
  placement of ONE model (`GLASSLAB_EXO_MODEL`). Hosting two models with
  different placements requires extending it or staging + on-demand placement.

## Quantization (4 / 5 / 8-bit / bf16)

Bits-per-weight define size: bf16 = 2 bytes/weight, 8-bit = 1, 4-bit = 0.5.

| Quant | Size of a 35B | Quality | Speed |
|---|---|---|---|
| bf16 | ~65 GB | reference | slowest |
| 8-bit | ~35 GB | near-lossless | slow |
| 5-bit | ~23 GB | near-8-bit at near-4-bit size | fast |
| 4-bit | ~19 GB | pragmatic floor | fastest |

- **Size** scales linearly with bits.
- **Speed** scales with size (bandwidth-bound): a 19 GB model generates ~3x
  faster than the same model at 65 GB.
- **Quality** degrades subtly at low precision: weaker multi-step reasoning,
  more long-tail knowledge errors (hallucination), and worse strict-output-
  format adherence — the last matters for the `AgentTurnResult` envelope.
  4-bit is the common production floor; 5-bit is the middle ground.

## Mixture-of-Experts and "A3B"

MoE models carry N total parameters but route each token through only a few
active experts. "35B-A3B" = 35B total, ~3.4B **active** per token.

- **Total params** = knowledge capacity + RAM footprint (all 35B must be
  resident).
- **Active params** = per-token compute and generation speed.
- A3B MoE runs like a ~3.4B model while holding ~35B of knowledge.

This is why `Qwen3-Coder-Next-4bit` (80B-A3B, 42.5 GB) is fast despite its
size, and why a 35B-A3B-4bit (19 GB) can beat a dense 27B-4bit (15 GB) on
speed-to-knowledge ratio.

## Verified candidate table (live registry, 2026-09-02)

| Model | Size | One Mac? | Notes |
|---|---|---|---|
| `Qwen3-Coder-Next-4bit` | 42.5 GB | yes (tight) | current shared model, 80B-A3B coder |
| `Qwen3-Next-80B-A3B-Thinking-4bit` | 43.8 GB | yes (tight) | same footprint + thinking depth |
| `Qwen3.5-122B-A10B-4bit` | 64.8 GB | **no** | exo planner: "No cycles found with sufficient memory" |
| `Llama-3.3-70B-Instruct-4bit` | 37.9 GB | yes | deepest dense option we can host |
| `Qwen3.6-35B-A3B-4bit` | 19.0 GB | yes | fast MoE, 35B knowledge |
| `Qwen3.6-35B-A3B-5bit` | 23.0 GB | yes | +quality at small size cost |
| `Qwen3.6-27B-4bit` | 15.0 GB | yes | dense, fast |
| `MiniMax-M2.7-4bit` / `Kimi-K2.6` | 120+ GB | no | out of envelope |

## Decision framework

1. **Fit**: the model must fit with headroom. > ~50 GB needs the pair; 64.8 GB
   (122B) is ruled out by exo's placement planner on this hardware.
2. **Quality per size**: knowledge density follows total params; reasoning
   depth follows active params (dense) or expert integration (MoE). 8-bit/bf16
   buys format-reliability headroom at responsiveness cost.
3. **Responsiveness**: prefer A3B/A4B MoE at 4-bit for fast tokens with large
   knowledge; accept slower dense models only for deep sequential reasoning.
4. **Agent allocation**: Honeydew's errors (protocols, methodology, reports,
   research answers) are the least mechanically catchable — give Honeydew the
   deepest-reasoning model the hardware hosts. Beaker's output is gated by
   preflight + evaluator + review; its model should still be strong enough to
   avoid rejection loops, but "one Mac" does not force weakness (Coder-Next
   itself fits one Mac).
5. **Co-residency**: with both models resident (e.g., Honeydew deep model
   sharded ~22 GB/Mac + Coder-Next ~21 GB/Mac = ~43 GB/Mac), the serial
   pipeline never contends (only one agent turns at a time), so both run at
   full 2-node bandwidth.

## Current state (2026-09-02)

- Shared model: `Qwen3-Coder-Next-4bit` (both agents).
- #319 plumbing (`GLASSLAB_ORCHESTRATOR_AGENT_MODEL_HONEYDEW` /
  `GLASSLAB_ORCHESTRATOR_AGENT_MODEL_BEAKER`) is merged and behavior-neutral.
- #321 decision record: 122B ruled out; keep Coder-Next until a candidate
  wins a benchmark (Thinking-4bit and Llama-70B are the open questions).
- Staging models requires `HF_TOKEN` (present at
  `/Users/glasslab/.cache/huggingface/token`) and a reconcile placement; see
  #321 for the operational details.