# Agent Coordination

Two coding agents work this repository concurrently. This file is the shared
coordination map. Read it before editing a shared file, and whoever resolves a
merge conflict in a shared file must apply the keep-both rule below.

## Lanes

- **Discord-surface agent**: the Discord citation / answer / packet surface —
  `/packet`, citation buttons, source buttons, chunked and streamed answers,
  packet matching (PRs #325–#337 and successors).
- **Research-chat orchestrator agent**: research-chat orchestration —
  conversations, `/research-promote` and the promote pipeline (#318, #328,
  #330, #335, #336), per-agent model configuration (#319), corpus/GPU
  infrastructure.

## Shared files (the seam)

- `services/research-orchestrator/app/discord_controls.py`
- `services/research-orchestrator/tests/test_discord_and_opencode.py`
- `services/research-orchestrator/tests/test_store_contract.py`

## Rules (apply in both directions)

1. Before editing a shared file, check for open PRs touching it
   (`gh pr view --json files`) and sequence around them (let the other merge
   first, or rebase).
2. Conflict resolution is **keep both sides** (additive). Never resolve a
   conflict by taking one side's version of the other lane's code.
3. `testing` is never force-pushed; sync it via fast-forward or a PR.

## Don't-clobber inventory

**Discord-surface agent owns:** `_on_packet_button`, source-button routing,
the `/packet` command, packet-id handling.

**Research-chat agent owns:** `_on_research_promote`, the `research-promote`
command, `_is_thread`, the thread-conversation behavior in
`_on_research_question`, the test fakes
(`_FakeFollowup` / `_FakeMessage` / `_FakeResponse` / `_FakeInteraction`), and
the tolerant active-slot postgres test
(`test_conversation_runs_do_not_hold_active_slot`, which a conflict
resolution already reverted once).

`_on_packet_button` and `_on_research_promote` sit in the same handler region
of `discord_controls.py`; edits to one must rebase around the other's merges.