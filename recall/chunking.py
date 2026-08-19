"""Turning source records into chunks that will actually embed.

Two lessons are built into this file, and both cost real runs.

**A character budget is a guess about tokens, and the guess is usually
wrong.** The conversion varies far more by content type than intuition
suggests. Measured across one real corpus:

    prose documents            2.4 to 4.4 characters per token
    workout and sensor lines   1.8
    spreadsheet exports        1.51
    marketing and airline mail 1.39

A budget set for prose rejects half the mail. So `calibrate` measures the
real ratio against the real tokenizer and derives the budget from it. Where
no tokenizer is reachable it falls back to the most pessimistic ratio seen in
practice, which is safe but wastes capacity.

**High-frequency events must roll up, and must keep their detail.** 391,896
music plays as one chunk each would triple a corpus with noise. A bare
monthly total, though, cannot answer "when did I start jiu jitsu". So a
rollup chunk summarises the period AND lists the individual events inside it.
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
    """Worst (lowest) characters-per-token across the samples.

    Lowest, not average: the budget has to hold for the densest chunk in the
    corpus, and it is the dense tail that gets rejected.
    """
    ratios = []
    for text in samples:
        if not text:
            continue
        n = counter(text)
        if n:
            ratios.append(len(text) / n)
    return min(ratios) if ratios else None


def calibrate(samples, ceiling=None, counter=tokenize_count):
    """A character budget that will not overrun the embedding context.

    Pass a handful of the LONGEST texts a source produces. Sampling short
    ones measures nothing: it is the long dense ones that fail.
    """
    ceiling = ceiling or config.EMBED_CONTEXT
    ratio = measure_density(samples, counter) or PESSIMISTIC_CHARS_PER_TOKEN
    return int(ceiling * SAFETY * ratio)


def split_to_budget(text, budget):
    """Break one text into pieces under the budget, on paragraph boundaries.

    Splitting a container on whole records is not enough on its own. One
    airline notice arrived as a single record of 188,218 characters, about
    47,000 tokens, and a per-record split never touched it.
    """
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

    The gap is a parameter because it is a property of the MEDIUM, not of
    this code. Live chat separates conversations at 30 minutes. Asynchronous
    messaging does not: at 30 minutes, 39 percent of one export's sessions
    came out as a single short message, which embeds to noise. A day put the
    median where live chat's 30 minutes put it.
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
