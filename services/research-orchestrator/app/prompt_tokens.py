"""Token accounting for the model-prompt bound.

``estimate_prompt_tokens`` delegates to the shared, dependency-free
:func:`app.text_tokens.estimate_tokens` leaf (#517). That estimate is
content-aware -- ``max(1, whitespace words, ceil(chars / 3))`` -- so it counts
compact JSON, code, and tool output by their size instead of treating a whole
document as one whitespace "word", and it is never below the old word count.

Context rotation is driven by the real per-message token usage OpenCode reports
(see ``opencode_runtime.message_context_tokens`` and
``AgentRuntime.session_context_tokens``); this module provides the conservative
estimate used only as a floor when the runtime cannot report usage.

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

from .text_tokens import CHARS_PER_TOKEN, estimate_tokens

__all__ = ['CHARS_PER_TOKEN', 'estimate_prompt_tokens']


def estimate_prompt_tokens(text: str) -> int:
    """Estimate model tokens for ``text`` (the shared content-aware floor).

    A thin alias for :func:`app.text_tokens.estimate_tokens`, kept as a named
    public entry point for the prompt-bound call sites in ``app.engine``.
    """
    return estimate_tokens(text)
