"""Hybrid retrieval: dense and sparse, fused on rank.

Fusion works on rank, not score: cosine distance and ts_rank are not
comparable numbers. See docs/lessons.md.
"""

import collections
import datetime as dt
import re
import time

RRF_K = 60
POOL = 50
TOP_K = 8

# pgvector caps the HNSW candidate list at hnsw.ef_search, which defaults to
# 40, no matter how many rows LIMIT asks for. A short result set is not an
# error, so a pool of 50 quietly fuses on 25 and nothing anywhere reports the
# shortfall. Measured on a 249k-row corpus: 25 rows at ef 40, 50 at ef 100.
#
# The width follows the pool rather than sitting at a fixed number. A fixed
# default only moves the threshold, so the next caller that raises the pool
# meets the same bug one level up.
EF_FLOOR = 100

# Qwen3-style embedding models are asymmetric: the instruction belongs on the
# QUERY at search time, never baked into every stored document.
QUERY_PREFIX = ("Instruct: Given a search query, retrieve relevant passages\n"
                "Query: ")

DateFilter = collections.namedtuple("DateFilter", "since until phrase")
NO_DATES = DateFilter(None, None, None)

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
SEASONS = {"spring": (3, 3), "summer": (6, 3), "fall": (9, 3),
           "autumn": (9, 3), "winter": (12, 3)}

# A year must not sit inside a longer digit run. Phone numbers contain things
# like 2026, and matching one narrows a decade of messages to a year the
# question never mentioned.
YEAR = r"(?<!\d)((?:19|20)\d{2})(?!\d)"
ISO_DAY = r"(?<!\d)((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)"


def rrf(rank_lists, k=RRF_K):
    """Reciprocal Rank Fusion. Ties keep first-seen order, so the same
    question returns the same chunks on every run."""
    scores = {}
    for refs in rank_lists:
        for rank, ref in enumerate(refs, start=1):
            scores[ref] = scores.get(ref, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def _span(year, month, months):
    end = month + months
    return (dt.date(year, month, 1).isoformat(),
            dt.date(year + (end - 1) // 12, (end - 1) % 12 + 1, 1).isoformat())


def parse_dates(query, today):
    """Pull a date range out of the question.

    `today` is a parameter, never the clock, so tests cannot fail on 1
    January. Deliberately conservative: a wrong filter removes an answer
    rather than degrading it.
    """
    q = (query or "").lower()
    m = re.search(ISO_DAY, q)
    if m:
        try:
            day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            day = None
        if day and day.year <= today.year + 1:
            return DateFilter(day.isoformat(),
                              (day + dt.timedelta(days=1)).isoformat(),
                              phrase=m.group(0))

    m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+" + YEAR, q)
    if m:
        return DateFilter(*_span(int(m.group(2)), MONTHS[m.group(1)], 1),
                          phrase=m.group(0))
    m = re.search(r"\b(" + "|".join(SEASONS) + r")\s+" + YEAR, q)
    if m:
        start, length = SEASONS[m.group(1)]
        return DateFilter(*_span(int(m.group(2)), start, length),
                          phrase=m.group(0))
    m = re.search(r"\blast\s+(" + "|".join(SEASONS) + r")\b", q)
    if m:
        start, length = SEASONS[m.group(1)]
        year = today.year
        if today.isoformat() < _span(year, start, length)[1]:
            year -= 1
        return DateFilter(*_span(year, start, length), phrase=m.group(0))
    m = re.search(r"\b(last|this)\s+year\b", q)
    if m:
        year = today.year - 1 if m.group(1) == "last" else today.year
        return DateFilter(*_span(year, 1, 12), phrase=m.group(0))
    m = re.search(YEAR, q)
    if m and int(m.group(1)) <= today.year + 1:
        return DateFilter(*_span(int(m.group(1)), 1, 12), phrase=m.group(1))
    return NO_DATES


COLUMNS = "ref, text, occurred_at, source, path"


def where_clause(dates, source):
    """Metadata pre-filter, as SQL plus bound parameters. The upper bound is
    exclusive: parse_dates returns the first instant of the next period."""
    parts, params = [], []
    if dates and dates.since:
        parts.append("occurred_at >= %s")
        params.append(dates.since)
        parts.append("occurred_at < %s")
        params.append(dates.until)
    if source:
        parts.append("source = %s")
        params.append(source)
    return (" WHERE " + " AND ".join(parts) if parts else ""), params


def dense_sql(where, pool):
    """The vector binds twice: once selected as the distance, once in the
    ORDER BY. The ORDER BY keeps the exact expression the HNSW index was
    built on, so the plan stays an index scan."""
    return (f"SELECT {COLUMNS}, embedding <=> %s::vector AS distance "
            f"FROM chunk{where} "
            f"ORDER BY embedding <=> %s::vector LIMIT {int(pool)}")


# The sparse arm searches by the DISTINCTIVE words in the question.
#
# plainto_tsquery joins every lexeme with AND. A natural-language question
# has four or five content words, a chunk holding all of them is rare, and
# the arm returned nothing on 26 of the first 30 real questions: the hybrid
# retriever was dense-only in practice. OR-ing every lexeme fixed the zero
# rows and broke the ranking instead, because ts_rank has no notion of
# document frequency, so "first" and "channel" outranked "chatgpt" and the
# one chunk that mattered never reached the pool. No Postgres ranking
# variant changed that; the five-question sweep is in docs/lessons.md.
#
# So the lexemes are counted against the corpus, one GIN lookup each, and
# only the rare ones are searched. A lexeme in more than RARE_SHARE of all
# chunks carries no signal. When nothing is rare the rarest two survive,
# so a question of ordinary words still gets an arm.
RARE_SHARE = 0.02
MAX_TERMS = 4
_LEXEME = re.compile(r"'([^']+)'")


def sparse_terms(conn, question):
    """The quoted lexemes to search, rarest first, at most MAX_TERMS."""
    with conn.cursor() as cur:
        cur.execute("SELECT plainto_tsquery('english', %s)::text", [question])
        lexemes = [f"'{l}'" for l in _LEXEME.findall(cur.fetchone()[0] or "")]
        if not lexemes:
            return []
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = 'chunk'")
        total = max(int(cur.fetchone()[0] or 0), 1)
        counted = []
        for lex in lexemes:
            cur.execute("SELECT count(*) FROM chunk WHERE tsv @@ %s::tsquery", [lex])
            counted.append((int(cur.fetchone()[0]), lex))
    counted = sorted((df, lex) for df, lex in counted if df > 0)
    rare = [lex for df, lex in counted if df <= total * RARE_SHARE]
    chosen = rare or [lex for _, lex in counted[:2]]
    return chosen[:MAX_TERMS]


def tsquery(terms):
    return " | ".join(terms)


def sparse_sql(where, pool):
    """The prebuilt query binds three times: rank, filter, order."""
    join = " AND " if where else " WHERE "
    return (f"SELECT {COLUMNS}, ts_rank(tsv, %s::tsquery, 1) AS rank "
            f"FROM chunk{where}{join}"
            f"tsv @@ %s::tsquery "
            f"ORDER BY ts_rank(tsv, %s::tsquery, 1) DESC "
            f"LIMIT {int(pool)}")


def search_width(pool):
    """The HNSW candidate list a pool of this size needs."""
    return max(int(pool) * 2, EF_FLOOR)


def vector_literal(vec):
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _ms(started):
    return int((time.monotonic() - started) * 1000)


ARM_KEYS = ("distance", "rank")


def search_traced(question, conn, embedder, k=TOP_K, pool=POOL, source=None,
                  today=None, dates=None):
    """Hybrid search that also returns what it decided on the way.

    Returns (hits, date_filter, trace). The trace keeps every fused
    candidate, not only the k that survive. Which chunks retrieval offered
    and did NOT use is half of the offered-against-cited signal the query
    decision log exists to measure.
    """
    from . import db
    today = today or dt.date.today()
    if dates is None:
        dates = parse_dates(question, today)
    where, params = where_clause(dates, source)

    t = time.monotonic()
    vec = vector_literal(embedder(QUERY_PREFIX + question))
    embed_ms = _ms(t)

    db.set_search_width(conn, search_width(pool))
    t = time.monotonic()
    dense = list(db.fetch(conn, dense_sql(where, pool), [vec] + params + [vec]))
    dense_ms = _ms(t)

    t = time.monotonic()
    terms = sparse_terms(conn, question)
    if terms:
        tsq = tsquery(terms)
        sparse = list(db.fetch(conn, sparse_sql(where, pool),
                               [tsq] + params + [tsq, tsq]))
    else:
        sparse = []
    sparse_ms = _ms(t)

    by_ref = {r["ref"]: r for r in dense + sparse}
    fused = rrf([[r["ref"] for r in dense], [r["ref"] for r in sparse]])
    hits = [dict({c: v for c, v in by_ref[ref].items() if c not in ARM_KEYS},
                 score=score)
            for ref, score in fused[:k]]
    trace = {
        "dense": [(r["ref"], r.get("distance")) for r in dense],
        "sparse": [(r["ref"], r.get("rank")) for r in sparse],
        "fused": fused,
        "dense_n": len(dense), "sparse_n": len(sparse), "fused_n": len(fused),
        "embed_ms": embed_ms, "dense_ms": dense_ms, "sparse_ms": sparse_ms,
    }
    return hits, dates, trace


def search(question, conn, embedder, k=TOP_K, pool=POOL, source=None,
           today=None, dates=None):
    """Returns (hits, date_filter). One path: this is search_traced with
    the trace dropped, so a test aimed here exercises the real thing."""
    hits, dates, _ = search_traced(question, conn, embedder, k=k, pool=pool,
                                   source=source, today=today, dates=dates)
    return hits, dates
