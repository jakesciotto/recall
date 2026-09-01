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

# The query decision log. It lives in this database, not a separate one,
# because query_candidate.ref must join to chunk.ref: "which sources get
# cited, against how often they get offered" is the main tuning question,
# and a separate database makes it impossible to ask.
LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_log (
  id                bigserial PRIMARY KEY,
  asked_at          timestamptz NOT NULL DEFAULT now(),
  client            text NOT NULL,
  streamed          boolean NOT NULL DEFAULT false,
  question          text NOT NULL,
  source_filter     text,
  k                 int NOT NULL,
  pool              int NOT NULL,
  date_phrase       text,
  date_since        date,
  date_until        date,
  dense_n           int,
  sparse_n          int,
  fused_n           int,
  model_requested   text,
  model_resolved    text,
  answer            text,
  cited             int[],
  cited_invalid     int[],
  prompt_chars      int,
  prompt_tokens     int,
  completion_tokens int,
  embed_ms          int,
  dense_ms          int,
  sparse_ms         int,
  generate_ms       int,
  first_token_ms    int,
  total_ms          int,
  error             text,
  -- Human columns. Nothing automated writes these.
  verdict           text,
  note              text
);

-- final_rank is NULL when fusion dropped the candidate before k. Keeping
-- the dropped ones is the point: what retrieval offered and did NOT use is
-- half of the offered-against-cited signal.
CREATE TABLE IF NOT EXISTS query_candidate (
  query_id     bigint NOT NULL REFERENCES query_log(id) ON DELETE CASCADE,
  ref          text NOT NULL,
  dense_rank   int,
  sparse_rank  int,
  distance     double precision,
  ts_rank      double precision,
  rrf_score    double precision NOT NULL,
  final_rank   int,
  cited        boolean NOT NULL DEFAULT false,
  PRIMARY KEY (query_id, ref)
);

CREATE INDEX IF NOT EXISTS query_candidate_ref_idx ON query_candidate (ref);
CREATE INDEX IF NOT EXISTS query_log_asked_idx ON query_log (asked_at);
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
        cur.execute(LOG_SCHEMA)


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


def query_log_count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM query_log")
        return int(cur.fetchone()[0])


def set_search_width(conn, width):
    """Widen the HNSW candidate list on this connection, then confirm it.

    pgvector caps the candidate list at hnsw.ef_search, default 40, so a
    query that asks for 50 rows returns fewer and raises nothing. See
    retrieve.EF_FLOOR for the measurement.

    It reads the applied value back rather than trusting that the statement
    ran. A check that reads only success is the exact fault this guards
    against, and set_config returns what it applied, so the check is free.

    Session scope, not SET LOCAL. SET LOCAL outside a transaction block does
    nothing and only warns, which would restore the silent shortfall in the
    one case hardest to notice.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('hnsw.ef_search', %s, false)",
                    (str(int(width)),))
        applied = cur.fetchone()[0]
    if int(applied) != int(width):
        raise RuntimeError(
            f"asked Postgres for hnsw.ef_search={int(width)}, "
            f"it reports {applied}")
    return int(applied)


def fetch(conn, sql, params=None):
    """Rows as dicts. Postgres builds the JSON; chunk text contains newlines,
    commas and quotes, so no delimiter parse is safe."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t",
                    params or ())
        return cur.fetchone()[0]
