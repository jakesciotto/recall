"""Postgres access and schema. See docs/lessons.md."""

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
  -- Generated: a text change updates full-text search for free.
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


def stored_digests(conn):
    """{ref: md5 of the stored text}.

    A re-run skips a ref whose text has not moved, which is what makes it
    cheap. It compares the digest rather than the ref alone, so a chunk the
    source now writes differently still reloads.

    Postgres computes the md5 so the text itself never crosses the wire: a
    real corpus holds gigabytes of it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT ref, md5(text) FROM chunk")
        return {r[0]: r[1] for r in cur}


def upsert(conn, rows):
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)


def counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT source, count(*) FROM chunk GROUP BY 1 ORDER BY 2 DESC")
        return cur.fetchall()


def fetch(conn, sql, params=None):
    """Rows as dicts. Postgres builds the JSON; chunk text contains newlines,
    commas and quotes, so no delimiter parse is safe."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t",
                    params or ())
        return cur.fetchone()[0]
