# PostgreSQL Store-Contract Testing

The research orchestrator uses `PostgresStore` in production and
`SqliteStore` for the local smoke path. The backend-neutral contract suite
keeps durable behavior aligned without requiring the local developer workflow
to run PostgreSQL.

## Coverage

`services/research-orchestrator/tests/test_store_contract.py` runs each case
against both backends when PostgreSQL is configured. It covers:

- legal run transitions and optimistic version conflicts;
- contiguous event sequences and cursor reads;
- action and job idempotency;
- Honeydew and human approval gates plus execution failure;
- retry lineage event persistence;
- knowledge sources, chunks, FTS retrieval, and context packets;
- restart recovery for running turns.

## Local execution

SQLite-only:

```bash
cd services/research-orchestrator
PYTHONPATH=. pytest tests/test_store_contract.py -q
```

Both backends, using a disposable local PostgreSQL container:

```bash
docker run --rm --name glasslab-contract-postgres \
  -e POSTGRES_USER=glasslab \
  -e POSTGRES_PASSWORD=glasslab \
  -e POSTGRES_DB=orchestrator_contract \
  -p 55433:5432 postgres:16-alpine

cd services/research-orchestrator
GLASSLAB_TEST_POSTGRES_DSN='postgresql://glasslab:glasslab@127.0.0.1:55433/orchestrator_contract' \
  PYTHONPATH=. pytest tests/test_store_contract.py -q
```

The test fixture generates unique record identifiers and idempotency keys, so
the PostgreSQL database may be reused during one local session. CI uses a new
ephemeral service container for every workflow job.

## Migration implications

This change adds no tables, columns, indexes, or migration steps. The existing
additive `PostgresStore._ensure_schema()` initialization remains unchanged.
The implementation fix corrects parameter binding in PostgreSQL full-text
search; it does not alter the SQLite FTS behavior or the public store
interface.

## CI

The reusable Python workflow starts PostgreSQL 16 and runs the contract suite as
its own required job. The existing SQLite-backed orchestrator suite remains in
place, so the PR Gate fails if either backend contract or the existing suite
fails.
