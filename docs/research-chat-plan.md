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
- **Discord is the chat surface; a page is only for citation resolution.**
  The conversational interaction (ask, answer, follow-up thread) lives on
  Discord, reusing the existing command/thread/artifact-delivery machinery.
  The one thing Discord cannot do is an interactive citation panel — so
  citation *lookup* resolves to a single read-only page, not a chat app.
  See "Surface decision" below.

## Architecture

**Phase 1 and 2 live on the orchestrator** (reuse the runtime + retrieval
in-process; fastest to a working demo). **Phase 4 migrates the chat to a
thin `research-chat` service** (web UI + conversation store calling a
bounded orchestrator API) once the UX warrants it — matching the repo's
separation (orchestrator = durable control plane; interfaces are
projections).

## Surface decision — Discord-native chat with a citation-resolution page

Discord is flat text; it cannot render an interactive NotebookLM-style
citation panel. But the *value* of that panel is separable from its UI:
**(a) verification** (check a claim against the source) and **(b) context**
(the surrounding text). Both are content — and the content already resolves
today: `knowledge://context:<packet_id>` maps via
`KnowledgeManager.get_context_packet` (`main.py:573` serves it over HTTP)
to a `ContextPacket` carrying `exact_text_supplied` (the literal text the
model saw) + source metadata (URI, digest). The panel's content is already
retrievable; only a render surface is missing.

Three interaction levels, cheapest first:

- **Level 1 — inline excerpts (zero new code, ships with the answer
  schema).** Each `Citation` carries a 1–2 sentence `excerpt`, rendered in
  the Discord message itself. Closes most of the trust loop in flat text.
- **Level 2 — `/citation <n>` thread command.** Resolves the cited packet
  and posts the full excerpt into the thread. Reuses the existing resolver.
- **Level 3 — clickable citations to one read-only page (the "panel"
  experience).** Each citation in the message is a link to a single route
  that renders the packet's exact text + source metadata — one HTML
  template, no auth. The chat stays in Discord; only citation lookup is a
  page.

**Access model for Level 3:** the page is *not public* — it is reachable
only inside the Glasslab network or through the existing port-forward
pattern (the orchestrator is already internal-only and operators already
tunnel to it). A Discord member with Glasslab access (i.e. who can reach
the provisioner/orchestrator, typically via the documented `ssh -L` port
forward) clicks the citation link and the page opens in their browser. The
link is meaningful only to someone who can reach the cluster — which is
exactly the population allowed to ask research questions.

**Recommendation:** ship Level 1 with Phase 1 (it is schema work); add
Level 3 when someone actually clicks a citation and complains (the resolver
exists, so it is a day of work on demand). A full chat web app is never on
the critical path.

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

### Phase 2 — Discord streaming + the citation-resolution page

- **Streaming to Discord** is **non-negotiable** (the local model is
  ~18 tok/s): stream the answer into the thread via message edits or
  chunked sends rather than a blocking post.
- **Level 3 citation page** (see "Surface decision"): one read-only route
  rendering a `ContextPacket` (exact text + source URI/digest) + one HTML
  template, no auth, internal/port-forwarded like the orchestrator itself.
  Citations in Discord messages become links to it.

**Acceptance:** answers stream into the thread; citations are clickable
links (for members with Glasslab access); the page renders the surrounding
source text + source metadata.

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

### Phase 4 (optional) — Full chat web app + model choice

Only if Discord-native answers prove good enough to deserve a richer
surface: extract the chat into a thin service (web UI + conversation store)
calling the orchestrator through a bounded `/chat`-family API. Consider a
chat-tuned model for conversational quality (the local 4-bit coding model
is strong at extraction, weak at natural conversation). This is the phase
the repo should *avoid rushing* — the citation page in Phase 2 covers the
verification interaction without it.

## Where to touch (implementation map)

- `services/research-orchestrator/app/schemas.py:64-74` — add `TurnKind`.
- `services/research-orchestrator/app/engine.py` — the
  `_run_agent_turn` + `_required_turn_kind_instruction` machinery; add the
  chat turn driver + retrieval wiring (mirror `_run_agent_turn` usage).
- `services/research-orchestrator/app/knowledge_manager.py:339` —
  `KnowledgeManager.retrieve` (turn-kind scoped).
- `services/research-orchestrator/app/main.py` — `POST /chat`; the
  citation-resolution route (packet rendering; `get_context_packet` is
  already served at `main.py:573`).
- `services/research-orchestrator/app/discord_controls.py` — the Discord
  Q&A command + `Level 2` `/citation <n>` command (existing command
  surface).
- `services/research-orchestrator/prompts/honeydew.md` — chat-turn
  instructions (citation discipline already present).
- `services/research-orchestrator/app/spec_feedback.py` — reuse the
  user-facing feedback formatting for unanswerable questions.

## Open questions / risks

- **Retrieval scoping**: chat retrieval must be conversation-scoped, not
  run-scoped (`run_scope` parameter today). Decide a conversation-scope key.
- **Retrieval quality (live, 2026-08-30)**: the deployed search ORs query
  tokens but has **no stopword removal** (`'simple'` config keeps
  the/and/from as mandatory tokens), so long instruction-heavy queries rank
  by common-word overlap — live probes returned weakly-relevant chunks
  (e.g. an S3-deployment chunk ranked #1 for an "UCI Adult Income" query).
  A stopword-aware fix (better `ts_config`, term filtering) is a
  prerequisite for citation quality.
- **RAG is plumbed but never exercised live**: zero `context_attached`
  events across the last 10 production runs (all predate the RAG deploy),
  and Honeydew's system prompt has no standing knowledge instructions — the
  only knowledge framing lives in the injected `<knowledge-context>` block.
  The first live end-to-end run is itself a milestone.
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
the parallel main/testing churn). Verify with curl before building the
citation page. Include the stopword-aware retrieval fix in Phase 1, not
later — citation quality is the whole product.