# Corpus-RAG Method-Advisory Rubric

Each generated advisory is scored per dimension, 0–2 (0 = absent/wrong,
1 = partial, 2 = fully satisfied). Total is reported alongside the raw
advisory; the automated `check_advisory` gate enforces the mechanical
subset marked **[gate]**.

| Dimension | What 2 looks like |
|---|---|
| Groundedness | Every substantive methodological claim traces to retrieved evidence; no uncited expert assertions. |
| Citation validity **[gate]** | Every citation resolves to an existing chunk row; quote matches stored span text. |
| Methodological relevance | Candidates match the stated objective and dataset profile rather than generic textbook lists. |
| Candidate diversity | Multiple sensible model families / evaluation strategies represented, not near-duplicates of one idea. |
| Assumptions surfaced | Key statistical assumptions named per candidate (e.g., linearity, independence, exchangeability). |
| Failure modes surfaced | Known failure/degradation modes named per candidate where evidence supports them. |
| Overreach penalty | No claim exceeds what the corpus supports; unsupported areas defer explicitly (score 0 when the advisory overclaims). |
| Experiment-matrix usefulness | Output maps onto concrete baselines/comparisons/metrics an experiment plan can consume. |

Contradiction handling: when sources disagree (e.g., consensus clustering
advocacy vs its documented limitations), the advisory must surface BOTH
positions as a contradiction pair instead of collapsing them into fake
consensus.

## Qrels key convention

`questions.jsonl` grades relevance with keys that are **manifest entry ids**
(e.g., `glmnet-jss`) because stable `source_id`s only exist after ingestion.
The benchmark runner resolves manifest id -> ingested `source_id` at runtime
via corpus membership (`rag_documents.source_id`). Grades: 0 irrelevant,
1 supporting context, 2 directly answers the question. Excluded from qrels by
design: the paywalled entries (`he-garcia-imbalanced`, `monti-consensus`) and
the image-only scans (`gap-statistic`, `efron-bootstrap`), which contain no
extractable text and are skipped at ingestion.
