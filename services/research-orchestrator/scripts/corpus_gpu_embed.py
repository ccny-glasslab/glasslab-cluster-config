"""GPU embedding of knowledge chunks missing current-lineage vectors.

Reads chunks from the orchestrator Postgres store, embeds them with
Snowflake/snowflake-arctic-embed-m-v1.5 on CUDA, and upserts canonical
vector bytes with the SAME model/revision/dims lineage the in-process
orchestrator path uses, so the orchestrator's numpy index reloads and
serves them without any service change. Idempotent: only chunks lacking a
vector for the model are processed; re-runs are no-ops.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import psycopg

MODEL_ID = 'Snowflake/snowflake-arctic-embed-m-v1.5'
DIMS = 768
INDEX_VERSION = 'dense-v1'
BATCH = 64


def main() -> int:
    dsn = os.environ['GLASSLAB_ORCHESTRATOR_STORE_POSTGRES_DSN']
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT DISTINCT revision FROM orchestrator_knowledge_chunk_vectors'
                ' WHERE model_id=%s LIMIT 1',
                (MODEL_ID,),
            )
            row = cur.fetchone()
            revision = row[0] if row else ''
        with conn.cursor() as cur:
            cur.execute(
                'SELECT chunk_id, text FROM orchestrator_knowledge_chunks'
                ' WHERE chunk_id NOT IN'
                ' (SELECT chunk_id FROM orchestrator_knowledge_chunk_vectors'
                '  WHERE model_id=%s)',
                (MODEL_ID,),
            )
            todo = cur.fetchall()
    print(f'pending chunks: {len(todo)} revision={revision}', flush=True)
    if not todo:
        print('nothing to embed; no-op')
        return 0

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_ID, device='cuda')
    done = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        vectors = model.encode(
            [text for _, text in batch],
            batch_size=BATCH,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for (chunk_id, _text), vector in zip(batch, vectors):
                    blob = np.asarray(vector, dtype='<f4').tobytes()
                    cur.execute(
                        'INSERT INTO orchestrator_knowledge_chunk_vectors'
                        ' (chunk_id, vec, model_id, revision, dims, index_version)'
                        ' VALUES (%s,%s,%s,%s,%s,%s)'
                        ' ON CONFLICT (chunk_id) DO UPDATE SET'
                        ' vec=EXCLUDED.vec, model_id=EXCLUDED.model_id,'
                        ' revision=EXCLUDED.revision, dims=EXCLUDED.dims,'
                        ' index_version=EXCLUDED.index_version',
                        (chunk_id, blob, MODEL_ID, revision, DIMS, INDEX_VERSION),
                    )
        done += len(batch)
        print(f'{done}/{len(todo)}', flush=True)
    print('embed complete')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())