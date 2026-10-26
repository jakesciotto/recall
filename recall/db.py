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

-- Model judgments over the log. Deliberately NO CHECK constraints: a model
-- populates these, and a CHECK would fail a whole batch on one unexpected
-- word. The code normalises to a known set and writes 'unknown' instead.
-- verdict and note above stay human columns; the judge never writes them.
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_model text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judged_at timestamptz;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_grounded text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_retrieval text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_hedged text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_question_type text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_note text;
-- The eval file's expect: line, the author's reference. Only recall eval
-- writes it. The review shows it before the keypress, the judge grades the
-- answer against it as judge_correct, and NULL there means no reference.
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS expected text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_correct text;
-- The eval file's decline: marker, the author saying a whole decline is
-- the correct answer. The judge reads the answer as judge_declined (whole,
-- part, no), and code grades a whole decline from the two, so the grade is
-- recomputable from the row.
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS expected_decline text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS judge_declined text;
CREATE INDEX IF NOT EXISTS query_log_unjudged_idx
  ON query_log (id) WHERE judged_at IS NULL;
-- Who labelled, and when. labelled_at stamps the human verdict so an
-- agent's ruling and a later human relabel order by time. The agent
-- columns are a third labeller's word: recall label writes them, and
-- nothing else does. No side writes another's columns.
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS labelled_at timestamptz;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS agent_verdict text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS agent_note text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS agent_name text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS agent_at timestamptz;
-- What the stats planner asked of the structured store, as JSON: the
-- calls, the model's raw answer, and any error. NULL when the planner
-- made no call and met no error. plan_ms is the planner's own time.
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS plan text;
ALTER TABLE query_log ADD COLUMN IF NOT EXISTS plan_ms int;
"""

# The structured store: one row per message beside the chunks, so a count
# is a query and not a guess. The store is replaced per source on every
# ingest, because a count must match the export, where a chunk keeps its
# stale ref on purpose. `computed` holds every table an answer cited,
# under the ref the answer cited, so the judge reads what the model read.
STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
  source      text NOT NULL,
  ref         text NOT NULL,
  occurred_at timestamptz NOT NULL,
  thread      text,
  sender      text NOT NULL DEFAULT '',
  mine        boolean NOT NULL,
  kind        text NOT NULL,
  chars       int NOT NULL DEFAULT 0,
  PRIMARY KEY (source, ref)
);
CREATE INDEX IF NOT EXISTS event_thread_idx ON event (source, thread, occurred_at);
CREATE INDEX IF NOT EXISTS event_at_idx     ON event (source, occurred_at);

CREATE TABLE IF NOT EXISTS thread (
  source  text NOT NULL,
  thread  text NOT NULL,
  name    text,
  members text[] NOT NULL DEFAULT '{}',
  direct  boolean NOT NULL DEFAULT false,
  PRIMARY KEY (source, thread)
);
ALTER TABLE thread ADD COLUMN IF NOT EXISTS direct boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS thread_members_idx ON thread USING gin (members);

CREATE TABLE IF NOT EXISTS alias (
  source text NOT NULL,
  handle text NOT NULL,
  label  text NOT NULL,
  PRIMARY KEY (source, handle)
);

CREATE TABLE IF NOT EXISTS computed (
  ref         text PRIMARY KEY,
  text        text NOT NULL,
  source      text NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now()
);
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
        cur.execute(STATS_SCHEMA)


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


_LOADS = (
    ("events", "event",
     ("source", "ref", "occurred_at", "thread", "sender", "mine", "kind", "chars"),
     lambda s, r: (s, r["ref"], r["at"], r.get("thread"), r.get("sender") or "",
                   bool(r["mine"]), r["kind"], int(r.get("chars") or 0))),
    ("threads", "thread", ("source", "thread", "name", "members", "direct"),
     lambda s, r: (s, r["thread"], r.get("name"), list(r.get("members") or []),
                   bool(r.get("direct")))),
    ("aliases", "alias", ("source", "handle", "label"),
     lambda s, r: (s, r["handle"], r["label"])),
)


def replace_events(conn, source, events, threads, aliases, clear=True):
    """Replace one source's rows in the structured store. Returns counts.

    Every row set is copied into a temporary table first, the source's
    rows are deleted, and the copies are inserted, all in one transaction.
    A reader never sees an empty store, and a duplicate ref inside one
    export lands once instead of ending the load. `clear` is False for
    the second file of a source that owns several, so the first file's
    rows survive.
    """
    counts = {}
    try:
        with conn.cursor() as cur:
            for (key, table, cols, row), rows in zip(
                    _LOADS, (events, threads, aliases)):
                cur.execute(f"CREATE TEMP TABLE {table}_in (LIKE {table}) "
                            f"ON COMMIT DROP")
                n = 0
                with cur.copy(f"COPY {table}_in ({', '.join(cols)}) "
                              f"FROM STDIN") as copy:
                    for r in rows:
                        copy.write_row(row(source, r))
                        n += 1
                counts[key] = n
            for key, table, cols, row in _LOADS:
                if clear:
                    cur.execute(f"DELETE FROM {table} WHERE source = %s", [source])
                cur.execute(f"INSERT INTO {table} SELECT * FROM {table}_in "
                            f"ON CONFLICT DO NOTHING")
                # The rows that landed, not the rows yielded: a conversation
                # id that two export files share lands once.
                if isinstance(getattr(cur, "rowcount", None), int) and cur.rowcount >= 0:
                    counts[key] = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts


def save_computed(conn, ref, text, source):
    """Keep a table an answer cited, under the ref it cited."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO computed (ref, text, source) VALUES (%s, %s, %s) "
                    "ON CONFLICT (ref) DO UPDATE SET text = EXCLUDED.text, "
                    "source = EXCLUDED.source, computed_at = now()",
                    [ref, text, source])
    conn.commit()


def counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT source, count(*) FROM chunk GROUP BY 1 ORDER BY 2 DESC")
        return cur.fetchall()


def event_counts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT source, count(*) FROM event GROUP BY 1 ORDER BY 2 DESC")
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
