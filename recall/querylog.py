"""Record what retrieval and the model decided when they answered.

The pair that earns this module is offered against cited. Retrieval offers
k sources and the model cites some subset. If the model never cites past
source 3, then k=8 spends context for nothing, and nothing else in the
system can answer that. The first real query through this log also found
that the dense arm returned 36 rows against a pool of 50. See
docs/lessons.md.

**A failed write here must never break a query.** log_query catches every
exception and returns None. A broken log costs data, never an answer. A
layer that observes a path must not raise into it.

The rows live in the same database as the corpus they describe, so nothing
new leaves the box. RECALL_QUERY_LOG=0 turns the log off.
"""

import os

from . import render

# The exact insertable column set. A key absent from here is dropped in
# silence and its column reads NULL forever, so the tests pin the set the
# call sites actually pass.
COLUMNS = (
    "client", "streamed", "question", "source_filter", "k", "pool",
    "date_phrase", "date_since", "date_until", "dense_n", "sparse_n",
    "fused_n", "model_requested", "model_resolved", "answer", "cited",
    "cited_invalid", "prompt_chars", "prompt_tokens", "completion_tokens",
    "embed_ms", "dense_ms", "sparse_ms", "generate_ms", "first_token_ms",
    "total_ms", "error",
)

CANDIDATE_COLUMNS = (
    "query_id", "ref", "dense_rank", "sparse_rank", "distance", "ts_rank",
    "rrf_score", "final_rank", "cited",
)


def enabled():
    """False only when RECALL_QUERY_LOG is exactly 0.

    An unset variable keeps the log on. A log that quietly defaults to off
    would look healthy while recording nothing.
    """
    return os.environ.get("RECALL_QUERY_LOG", "1").strip() != "0"


def model_name(reported):
    """The bare model name from whatever the server reported.

    llama.cpp answers with the full GGUF path, so the raw value embeds a
    home directory and buries the name.
    """
    if not reported:
        return ""
    name = os.path.basename(str(reported).rstrip("/"))
    return name[:-5] if name.lower().endswith(".gguf") else name


def cited_numbers(answer, n_sources):
    """(valid, invalid) source numbers the answer cites.

    The model writes groups like "[4, 5]". A pattern matching a single
    number misses half the citations, and it fails in the safe-looking
    direction: the log simply under-reports use. render._CITE already
    solved this and is reused, because a second pattern drifts from the
    first.

    A number outside 1..n_sources goes to `invalid`. A model that cites
    [12] against 8 sources is telling you something worth keeping.
    """
    valid, invalid = set(), set()
    for m in render._CITE.finditer(answer or ""):
        for part in m.group(1).split(","):
            n = int(part.strip())
            (valid if 1 <= n <= n_sources else invalid).add(n)
    return sorted(valid), sorted(invalid)


def candidates_from(trace, hits, cited_valid):
    """One candidate row per fused ref, arm ranks and scores included.

    Every fused candidate is kept, not only the k that reached the prompt.
    What retrieval offered and did NOT use is half of the signal.

    A citation number indexes the PROMPT, not the ref. "[2]" means the
    second source given to the model, so it maps through `hits`, and a
    number outside that range marks nothing rather than raising.
    """
    dense = {ref: (i + 1, score)
             for i, (ref, score) in enumerate(trace.get("dense") or [])}
    sparse = {ref: (i + 1, score)
              for i, (ref, score) in enumerate(trace.get("sparse") or [])}
    final = {h["ref"]: i + 1 for i, h in enumerate(hits)}
    cited = {hits[n - 1]["ref"] for n in cited_valid if 1 <= n <= len(hits)}

    rows = []
    for ref, rrf_score in (trace.get("fused") or []):
        d = dense.get(ref)
        s = sparse.get(ref)
        rows.append({
            "ref": ref,
            "dense_rank": d[0] if d else None,
            "sparse_rank": s[0] if s else None,
            "distance": d[1] if d else None,
            "ts_rank": s[1] if s else None,
            "rrf_score": rrf_score,
            "final_rank": final.get(ref),
            "cited": ref in cited,
        })
    return rows


def log_query(conn, *, client, question, k, pool, source, dates, trace, hits,
              answer, meta, model_requested, prompt_chars=None,
              streamed=False, total_ms=None, first_token_ms=None, error=None):
    """Assemble one query decision row and write it. Never raises.

    Safety covers assembly as well as the write. A row that cannot be built
    must not take the answer down with it, so the whole body sits inside
    one guard rather than only the database call.
    """
    if not enabled():
        return None
    try:
        meta = meta or {}
        valid, invalid = cited_numbers(answer, len(hits))
        entry = {
            "client": client,
            "streamed": bool(streamed),
            "question": question,
            "source_filter": source,
            "k": k,
            "pool": pool,
            "date_phrase": getattr(dates, "phrase", None),
            "date_since": getattr(dates, "since", None),
            "date_until": getattr(dates, "until", None),
            "dense_n": trace.get("dense_n"),
            "sparse_n": trace.get("sparse_n"),
            "fused_n": trace.get("fused_n"),
            "model_requested": model_requested,
            "model_resolved": model_name(meta.get("model_resolved")),
            "answer": answer,
            "cited": valid,
            "cited_invalid": invalid,
            "prompt_chars": prompt_chars,
            "prompt_tokens": meta.get("prompt_tokens"),
            "completion_tokens": meta.get("completion_tokens"),
            "embed_ms": trace.get("embed_ms"),
            "dense_ms": trace.get("dense_ms"),
            "sparse_ms": trace.get("sparse_ms"),
            "generate_ms": meta.get("generate_ms"),
            "first_token_ms": first_token_ms,
            "total_ms": total_ms,
            "error": error,
        }
        candidates = candidates_from(trace, hits, valid)
    except Exception:
        return None
    return record(conn, entry, candidates)


def log(**kw):
    """Write one row on a connection of its own. Never raises.

    The answer path closed its search connection before generation, and a
    connection held open across a sixty second generation would be a
    connection held for nothing. Opening one here costs a local socket.
    """
    if not enabled():
        return None
    try:
        from . import db
        with db.connect() as conn:
            return log_query(conn, **kw)
    except Exception:
        return None


def _rollback(conn):
    try:
        conn.rollback()
    except Exception:
        pass


def record(conn, entry, candidates):
    """Write one query and its candidates. Return the id, or None.

    This never raises. Every caller sits on the answer path, and an answer
    must not fail because a log write did.

    The candidate rows go in only after the log row commits, because a
    candidate with no parent is unreadable noise, and a parent without its
    candidates is still a question worth having.
    """
    cols = [c for c in COLUMNS if c in entry]
    holes = ", ".join(["%s"] * len(cols))
    try:
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO query_log ({', '.join(cols)}) "
                        f"VALUES ({holes}) RETURNING id",
                        [entry[c] for c in cols])
            query_id = int(cur.fetchone()[0])
        conn.commit()
    except Exception:
        _rollback(conn)
        return None
    if not candidates:
        return query_id
    try:
        rows = [[query_id, c.get("ref")] + [c.get(k) for k in CANDIDATE_COLUMNS[2:]]
                for c in candidates]
        holes = ", ".join(["(" + ", ".join(["%s"] * len(CANDIDATE_COLUMNS)) + ")"]
                          * len(rows))
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO query_candidate "
                        f"({', '.join(CANDIDATE_COLUMNS)}) VALUES {holes} "
                        f"ON CONFLICT DO NOTHING",
                        [v for row in rows for v in row])
        conn.commit()
    except Exception:
        _rollback(conn)
    return query_id
