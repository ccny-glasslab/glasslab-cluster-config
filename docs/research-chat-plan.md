# Research Chat — NotebookLM-style grounded Q&A over the RAG corpus

Status: **PROPOSED** (plan for future implementation; not started)
Owner: future-us
Related: corpus-RAG (#224 / feat/honeydew-method-advisor), knowledge retrieval, research-orchestrator

## Purpose

Let an operator talk to Honeydew in a conversational, NotebookLM-like mode:
ask real research questions and get **grounded answers that cite the RAG
corpus**. This is distinct from the current pipeline (task → protocol →
implementation → evidence → report), which is a bounded, human-gated
experiment loop. Research Chat is an **investigation surface**: quick,
interactive, cite-everything.

## Current state (what already exists — the foundation is unusually good)

The three NotebookLM pillars are already built in `research-orchestrator`:

1. **Grounded retrieval.** Every agent turn runs per-turn retrieval
   (`KnowledgeManager.retrieve`), producing a persisted `ContextPacket`
   that is citable as **`knowledge://context:<packet_id>`**. The
   `engine.py` turn path explicitly records `agent.context_attached` with the
   packet id so the source of any answer is auditable.
2. **Citation discipline.** Honeydew's system prompt (`prompts/honeydew.md`)
   requires evidence URIs (`artifact://`, `job://`, `git://`, `event://`,
   `knowledge://`, `contract://`) for claims; the `Claim` model already
   carries `evidence: list[str]` of URIs. The turn contract can require
   citations.
3. **A bounded agent runtime with structured output.** Every turn must
   return a schema-validated `AgentTurnResult` (JSON-schema enforced, with
   bounded repair). An answer schema can therefore require a `citations[]`
   field.

What is **missing**:

- A free-form Q&A turn kind. All current `TurnKind`s are phase-scoped
  (`schemas.py:64-74`): `protocol_draft`, `methodology_review`, etc.
- Conversation state (multi-turn memory is per-run, not per-conversation).
- A chat surface (none exists; Discord is a control surface, not a product
  surface).
- **Corpus curation, not corpus creation.** The live production store already
  holds **75 knowledge-source rows / 66 unique titles / 3151 chunks** (checked
  live 2026-08-30): ML-methods literature (metric learning, uncertainty
  quantification, conformal prediction, autoML, model cards, agent
  benchmarks) plus Glasslab-internal sources. Nine titles are duplicated
  (18 rows) and need dedup — see Phase 0.

## Design principles

- **Every answer cites its sources.** The answer schema requires
  `citations[]` with `knowledge://context:<id>` (or `artifact://`) URIs, so
  an ungrounded answer is a schema violation.
- **Bounded + deterministic.** Same discipline as the pipeline: the agent
  proposes, deterministic code validates; no free-form tool use beyond the
  existing permission surface.
- **Chat is a projection, not state.** The orchestrator stays the durable
  control plane; conversation records are durable but never authoritative
  over pipeline state.
- **No Discord for the product surface.** Discord remains the operator
  control surface. A NotebookLM experience needs a web surface (rendered
  citations, follow-up threads, source panel).

## Architecture

**Phase 1 and 2 live on the orchestrator** (reuse the runtime + retrieval
in-process; fastest to a working demo). **Phase 4 migrates the chat to a
thin `research-chat` service** (web UI + conversation store calling a
bounded orchestrator API) once the UX warrants it — matching the repo's
separation (orchestrator = durable control plane; interfaces are
projections).

## Phases

### Phase 0 — Corpus readiness (prerequisite, can run in parallel)

The corpus already exists and is substantial (75 source rows / 66 unique
titles / 3151 chunks live). Phase 0 is therefore **curation, not creation**:
1. **Dedup** — nine titles are ingested twice (050, 083, 102, 152, 192,
   193, 242, 244, 254). Remove the duplicate rows so retrieval cannot
   return the same content twice.
2. **Quality/coverage** — audit chunk health (the `_rechunk_source` path)
   and fill gaps for the lab's actual question domains (the current corpus
   is ML-methods-heavy; check Glasslab-ops and research-frontier coverage).
3. **Retrieval spot-check** — representative questions must return relevant
   packets with clean citations.

**Acceptance:** zero duplicate titles; retrieval over the corpus returns
relevant packets for representative questions; packets are citable as
`knowledge://context:<id>`.

### Phase 1 — Grounded answer turn + `/chat` endpoint (the feasibility proof)

- Add `TurnKind.RESEARCH_ANSWER = 'research_answer'` (`schemas.py:64-74`).
- Add the answer schema: `ResearchAnswer` with `answer: str`,
  `citations: list[Citation]` where `Citation = {knowledge_uri, source,
  excerpt}` (required, non-empty when the question is answerable from the
  corpus), plus optional `unanswerable: bool` and `suggested_followups`.
- Add a bounded `POST /chat` endpoint on the orchestrator:
  `{question, conversation_id?}` →
  1. derive the retrieval query from the question (reuse
     `_retrieval_query`-style intent derivation),
  2. `KnowledgeManager.retrieve(...)` scoped to the chat (agent
     `honeydew`, turn_kind `research_answer`),
  3. attach the `ContextPacket` (recorded as `agent.context_attached`),
  4. run the `research_answer` turn through the existing
     `_run_agent_turn` machinery,
  5. return the validated answer + citations.
- No UI. Verify with curl: "what does the corpus say about X?" → a cited
  answer whose `knowledge://` URIs resolve to real packets/sources.

**Acceptance:** `POST /chat` returns a schema-valid answer with citations
for corpus-answerable questions; ungrounded/unanswerable questions return
`unanswerable: true` rather than a hallucinated answer; the turn honors the
existing retry/session machinery.

### Phase 2 — Web surface + streaming

A minimal web UI (chat pane, citation chips linking to sources, follow-up
input) served by the orchestrator (or a static bundle). Streaming via SSE
is **non-negotiable** (the local model is ~18 tok/s — a blocking response
is a bad experience).

**Acceptance:** a browser session answers questions with clickable
citations; answers stream incrementally; conversation_id threading works.

### Phase 3 — Conversation state and follow-ups

- A `conversation` store: `conversation_id`, `turns[]`, `sources[]`
  (bound sources for the session), `created_by`.
- Follow-ups use the conversation context (prior turns + bound sources) as
  additional retrieval/turn context (bounded: last K turns, token-budgeted).
- Source binding: the operator can bind/upload a document to the
  conversation, and answers cite it.

**Acceptance:** multi-turn follow-ups stay grounded; bound sources are
retrievable and cited; the conversation is recoverable after an agent
restart (via the existing session/checkpoint machinery).

### Phase 4 (optional) — Dedicated `research-chat` service + model choice

When the UX warrants it, extract the chat into a thin service (web UI +
conversation store) that calls the orchestrator through a bounded
`/chat`-family API. Consider a chat-tuned model for conversational quality
(the local 4-bit coding model is strong at extraction, weak at natural
conversation).

## Where to touch (implementation map)

- `services/research-orchestrator/app/schemas.py:64-74` — add `TurnKind`.
- `services/research-orchestrator/app/engine.py` — the
  `_run_agent_turn` + `_required_turn_kind_instruction` machinery; add the
  chat turn driver + retrieval wiring (mirror `_run_agent_turn` usage).
- `services/research-orchestrator/app/knowledge_manager.py:339` —
  `KnowledgeManager.retrieve` (turn-kind scoped).
- `services/research-orchestrator/app/main.py` — `POST /chat` (+ SSE in
  Phase 2).
- `services/research-orchestrator/prompts/honeydew.md` — chat-turn
  instructions (citation discipline already present).
- `services/research-orchestrator/app/spec_feedback.py` — reuse the
  user-facing feedback formatting for unanswerable questions.

## Open questions / risks

- **Retrieval scoping**: chat retrieval must be conversation-scoped, not
  run-scoped (`run_scope` parameter today). Decide a conversation-scope key.
- **Corpus quality**: retrieval quality determines citation quality; Phase 0
  is the real risk.
- **Model ceiling**: citation-faithful answering is a harder task than
  drafting; the local model may need a chat-tuned sibling for production.
- **Turn-budget interaction**: chat turns must not consume a run's turn
  budget — they are not pipeline turns (the `_check_turn_budget` path must
  be bypassed or a chat-scoped budget used).
- **Abuse bounds**: cap per-question retrieval tokens + answer length; reuse
  the existing rate/budget discipline.

## First slice (what future-us should start with)

Phase 0 (dedup + curate the existing 75-source corpus) **and** Phase 1
(turn kind + `/chat` on a
`feat/research-chat` branch from `testing`, in a dedicated worktree given
the parallel main/testing churn). Verify with curl before any UI work.