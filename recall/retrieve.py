"""Hybrid retrieval: dense and sparse, fused on rank.

Fusion works on rank, not score: cosine distance and ts_rank are not
comparable numbers. See docs/lessons.md.
"""

import collections
import datetime as dt
import re

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
    return (f"SELECT {COLUMNS} FROM chunk{where} "
            f"ORDER BY embedding <=> %s::vector LIMIT {int(pool)}")


def sparse_sql(where, pool):
    """plainto_tsquery, not to_tsquery: to_tsquery rejects ordinary
    punctuation."""
    join = " AND " if where else " WHERE "
    return (f"SELECT {COLUMNS} FROM chunk{where}{join}"
            f"tsv @@ plainto_tsquery('english', %s) "
            f"ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC "
            f"LIMIT {int(pool)}")


def search_width(pool):
    """The HNSW candidate list a pool of this size needs."""
    return max(int(pool) * 2, EF_FLOOR)


def vector_literal(vec):
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def search(question, conn, embedder, k=TOP_K, pool=POOL, source=None,
           today=None, dates=None):
    """Returns (hits, date_filter)."""
    from . import db
    today = today or dt.date.today()
    if dates is None:
        dates = parse_dates(question, today)
    where, params = where_clause(dates, source)

    vec = vector_literal(embedder(QUERY_PREFIX + question))
    db.set_search_width(conn, search_width(pool))
    dense = db.fetch(conn, dense_sql(where, pool), params + [vec])
    sparse = db.fetch(conn, sparse_sql(where, pool),
                      params + [question, question])

    by_ref = {r["ref"]: r for r in list(dense) + list(sparse)}
    fused = rrf([[r["ref"] for r in dense], [r["ref"] for r in sparse]])
    hits = [dict(by_ref[ref], score=score) for ref, score in fused[:k]]
    return hits, dates
