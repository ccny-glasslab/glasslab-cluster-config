"""Token accounting for the model-prompt bound.

``knowledge_manager.estimate_tokens`` is a whitespace word count. It is the
right measure for the retrieval chunk budget it was written for, but it
under-counts compact JSON, code, and tool output by several times, so it cannot
bound a model request. Context rotation is driven by the real per-message token
usage OpenCode reports (see ``opencode_runtime.message_context_tokens`` and
``AgentRuntime.session_context_tokens``); this module provides the conservative
character-based estimate used only as a floor when the runtime cannot report
usage.

Measured on run ``295bc0ce`` (2026-09-16): the ``.17`` MLX host deadlocked on a
60,333-token prompt. Over the same run the whitespace estimate summed to 2,014
tokens (1.6% of the then-threshold of 128,000), while ``len(chars) // 4`` over
the orchestrator's stored turn data reached only 6,541 -- proof that no estimate
over stored turn data can observe OpenCode's own session contents (tool
schemas, file reads, command output).

ADVISORY ONLY. This estimate is a FLOOR, never a BOUND. It cannot see the
system prompt, tool schemas, file reads, or tool output, so it understates the
real prompt by up to an order of magnitude (6,541 vs 60,333 above). It does not
bound the fatal range. The protection is the captured real value
(``AgentRuntime.session_context_tokens``); this estimate only makes a
usage-less backend rotate earlier than nothing.
"""

from __future__ import annotations

# Characters per model token used by the fallback. The live fatal prompt
# measured ~3.8 characters per token; dividing by a smaller number over-counts,
# so the fallback rotates earlier rather than later. Integer math keeps the
# result deterministic across platforms.
CHARS_PER_TOKEN = 3


def estimate_prompt_tokens(text: str) -> int:
    """Estimate model tokens for ``text`` from its character count.

    Unlike ``knowledge_manager.estimate_tokens`` (a whitespace word count that
    treats a compact JSON document as a single word), this tracks request size
    closely enough to act as a conservative floor when the runtime reports no
    usage. Empty text counts as one token so a free prompt is never zero-cost.
    """
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)
