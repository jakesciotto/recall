"""Hybrid retrieval: dense and sparse, fused on rank.

Hybrid ALWAYS. Pure vector search underperforms badly on a personal archive
because so many real questions are metadata questions wearing semantic
clothes ("the invoice from spring 2023"). Dense search alone also misses a
rare literal string that sparse search finds instantly.

Fusion works on RANK, never on score. Cosine distance and ts_rank are not
comparable numbers, and normalising them into one scale invents a
relationship that does not exist.
"""

import collections
import datetime as dt
import re

RRF_K = 60
POOL = 50
TOP_K = 8

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

    `today` is a parameter, never the clock, so tests cannot start failing on
    1 January.

    Deliberately conservative. A wrong date filter does not degrade an
    answer, it REMOVES it, and the user sees a confident "nothing found"
    instead of a mistake. So it fires only on an explicit year or an
    unambiguous relative phrase. A bare month name does not fire: "march" is
    also an ordinary verb.
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
    """Metadata pre-filter, as SQL plus bound parameters.

    Parameters, not interpolation: the question is user text and it must
    never be concatenated into SQL. The upper bound is exclusive because
    parse_dates returns the first instant of the NEXT period.
    """
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
    """plainto_tsquery, not to_tsquery: it takes plain user text and never
    raises on punctuation. to_tsquery rejects an ordinary question."""
    join = " AND " if where else " WHERE "
    return (f"SELECT {COLUMNS} FROM chunk{where}{join}"
            f"tsv @@ plainto_tsquery('english', %s) "
            f"ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC "
            f"LIMIT {int(pool)}")


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
    dense = db.fetch(conn, dense_sql(where, pool), params + [vec])
    sparse = db.fetch(conn, sparse_sql(where, pool),
                      params + [question, question])

    by_ref = {r["ref"]: r for r in list(dense) + list(sparse)}
    fused = rrf([[r["ref"] for r in dense], [r["ref"] for r in sparse]])
    hits = [dict(by_ref[ref], score=score) for ref, score in fused[:k]]
    return hits, dates
