"""Postgres access and schema.

One structural decision worth keeping: **build the index AFTER the bulk
load**, never before. Loading into an existing HNSW index makes every insert
pay index maintenance. On one corpus the same data loaded at 1,568 rows per
minute without the index and a fraction of that with it, and building the
index afterwards took two minutes.
"""

import contextlib

from . import config

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunk (
  id              bigserial PRIMARY KEY,
  ref             text NOT NULL UNIQUE,
  text            text NOT NULL,
  source          text NOT NULL,
  occurred_at     timestamptz,
  date_confidence text NOT NULL DEFAULT 'low',
  participants    text[],
  thread          text,
  path            text,
  embedding       vector(%(dims)s),
  -- Generated, so any change to `text` updates full-text search for free.
  tsv             tsvector GENERATED ALWAYS AS
                    (to_tsvector('english', text)) STORED
);

CREATE INDEX IF NOT EXISTS chunk_tsv_idx      ON chunk USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunk_occurred_idx ON chunk (occurred_at);
CREATE INDEX IF NOT EXISTS chunk_source_idx   ON chunk (source);
"""

VECTOR_INDEX = ("CREATE INDEX IF NOT EXISTS chunk_embedding_idx ON chunk "
                "USING hnsw (embedding vector_cosine_ops)")

UPSERT = """
INSERT INTO chunk (ref, text, source, occurred_at, date_confidence,
                   participants, thread, path, embedding)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ref) DO UPDATE SET
  text = EXCLUDED.text, source = EXCLUDED.source,
  occurred_at = EXCLUDED.occurred_at,
  date_confidence = EXCLUDED.date_confidence,
  participants = EXCLUDED.participants, thread = EXCLUDED.thread,
  path = EXCLUDED.path, embedding = EXCLUDED.embedding
"""


@contextlib.contextmanager
def connect(dsn=None):
    import psycopg
    conn = psycopg.connect(dsn or config.PG_DSN)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def apply_schema(conn, dims=None):
    with conn.cursor() as cur:
        cur.execute(SCHEMA % {"dims": dims or config.EMBED_DIMS})


def build_vector_index(conn):
    with conn.cursor() as cur:
        cur.execute(VECTOR_INDEX)
        cur.execute("ANALYZE chunk")


def existing_refs(conn):
    """Refs already stored. This is what makes a re-run cheap and a resume
    possible, and it is why adapters must emit stable refs."""
    with conn.cursor() as cur:
        cur.execute("SELECT ref FROM chunk")
        return {r[0] for r in cur}


def upsert(conn, rows):
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)


def counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT source, count(*) FROM chunk GROUP BY 1 ORDER BY 2 DESC")
        return cur.fetchall()


def fetch(conn, sql, params=None):
    """Rows as dicts. Postgres builds the JSON rather than this code parsing
    a text table: chunk text carries newlines, commas, and quotes, and any
    delimiter parse corrupts exactly the rows containing the delimiter."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t",
                    params or ())
        return cur.fetchone()[0]
