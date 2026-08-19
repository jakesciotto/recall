"""Chunk sizing and grouping.

Budgets are measured against the real tokenizer, never assumed: content runs
1.4 to 4.4 characters per token. See docs/lessons.md.
"""

import collections
import json
import urllib.request

from . import config

# The worst ratio observed in practice. Only used when no tokenizer answers.
PESSIMISTIC_CHARS_PER_TOKEN = 1.35

# Leave the ceiling room for a header, joins, and the tokenizer disagreeing
# with itself at the tail.
SAFETY = 0.70


def tokenize_count(text, url=None):
    """Token count from the server, or None if no tokenizer is reachable."""
    url = url or config.TOKENIZE_URL
    if not url:
        base = config.EMBED_URL.split("/v1/")[0]
        url = f"{base}/tokenize"
    try:
        req = urllib.request.Request(
            url, json.dumps({"content": text}).encode(),
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return len(json.load(r)["tokens"])
    except Exception:
        return None


def measure_density(samples, counter=tokenize_count):
    """Lowest characters-per-token in the samples. Lowest, not average: the
    budget must hold for the densest chunk."""
    ratios = []
    for text in samples:
        if not text:
            continue
        n = counter(text)
        if n:
            ratios.append(len(text) / n)
    return min(ratios) if ratios else None


def calibrate(samples, ceiling=None, counter=tokenize_count):
    """A character budget that fits the embedding context. Pass the LONGEST
    texts a source produces; short samples measure nothing."""
    ceiling = ceiling or config.EMBED_CONTEXT
    ratio = measure_density(samples, counter) or PESSIMISTIC_CHARS_PER_TOKEN
    return int(ceiling * SAFETY * ratio)


def split_to_budget(text, budget):
    """Break one text into pieces under the budget, on paragraph boundaries."""
    if len(text) <= budget:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        while len(para) > budget:
            if buf:
                out.append(buf)
                buf = ""
            out.append(para[:budget])
            para = para[budget:]
        if buf and len(buf) + len(para) + 2 > budget:
            out.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def pack(records, budget, text_of=lambda r: r.get("text") or ""):
    """Group records into batches under the budget, splitting any that alone
    exceed it. Yields lists of records."""
    batch, size = [], 0
    for rec in records:
        pieces = split_to_budget(text_of(rec), budget)
        for piece in pieces:
            item = dict(rec)
            item["text"] = piece
            n = len(piece) + 80
            if batch and size + n > budget:
                yield batch
                batch, size = [], 0
            batch.append(item)
            size += n
    if batch:
        yield batch


def sessions(records, gap_seconds, max_turns=20,
             key=lambda r: r["thread"], when=lambda r: r["at"]):
    """Group consecutive records into conversation windows.

    The gap is a parameter because it belongs to the medium: live chat splits
    at 30 minutes, asynchronous messaging needs a day.
    """
    batch = []
    for r in records:
        if batch and (key(r) != key(batch[-1])
                      or when(r) - when(batch[-1]) > gap_seconds
                      or len(batch) >= max_turns):
            yield batch
            batch = []
        batch.append(r)
    if batch:
        yield batch


def rollup(records, period=lambda r: r["at"][:7]):
    """[(period, [record, ...])] in order. The caller writes the summary."""
    buckets = collections.defaultdict(list)
    for r in records:
        buckets[period(r)].append(r)
    return sorted(buckets.items())
