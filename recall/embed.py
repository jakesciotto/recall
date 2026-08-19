"""Embedding, with the failure handling that a long run actually needs.

The expensive lesson behind this file: an embedding server that dies mid-run
answers HTTP 502, and so does a server that merely refuses one oversized
request. They look identical to the client and need opposite responses.

A client that reacts to every failure by shrinking the request will, against
a dead server, bisect a batch down to single items and write every one of
them off as bad data. That happened. It cost 55 chunks and the log blamed the
chunks, including one 279 characters long that could not possibly have been
too large.

So: on failure, ask the server's health endpoint first. Wait for a restart,
retry the same batch, and only treat a refusal as an oversize signal once a
healthy server has given one.
"""

import json
import time
import urllib.error
import urllib.request

from . import config


class EmbeddingServerDown(RuntimeError):
    """Raised when the server never comes back. Ending the run beats draining
    the rest of the corpus into a drop list."""


def _health_url():
    """Derive /health from the embeddings URL when none is configured."""
    base = config.EMBED_URL.split("/v1/")[0]
    return f"{base}/health"


def embed(texts, timeout=300):
    body = json.dumps({"model": config.EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(config.EMBED_URL, body,
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)["data"]
    vecs = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    if len(vecs) != len(texts):
        raise RuntimeError(f"asked for {len(texts)} embeddings, got {len(vecs)}")
    for v in vecs:
        if len(v) != config.EMBED_DIMS:
            raise RuntimeError(f"expected {config.EMBED_DIMS} dims, got {len(v)}")
    return vecs


def server_ready(timeout=900, url=None):
    """Block until the embedding server answers again."""
    url = url or _health_url()
    deadline = time.monotonic() + timeout
    delay = 2
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                if r.status == 200:
                    return True
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            pass
        time.sleep(delay)
        delay = min(delay * 2, 30)
    return False


def embed_safe(items, on_drop=None, on_wait=None, _ready=None):
    """Embed a batch. Returns (items_kept, vectors).

    A failure is ambiguous, so this distinguishes the two causes before
    reacting. See the module docstring: reacting the wrong way silently
    discards good data and blames it.
    """
    ready = _ready or server_ready
    texts = [i["text"] for i in items]
    try:
        return items, embed(texts)
    except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
        if not ready():
            raise EmbeddingServerDown(
                "embedding server did not come back; stopping so the run can "
                "resume rather than dropping every remaining item") from e
        if on_wait:
            on_wait(len(items))
        try:
            return items, embed(texts)
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError):
            pass
        if len(items) == 1:
            if on_drop:
                on_drop(items[0], e)
            return [], []
        mid = len(items) // 2
        left, lv = embed_safe(items[:mid], on_drop, on_wait, _ready)
        right, rv = embed_safe(items[mid:], on_drop, on_wait, _ready)
        return left + right, lv + rv


def batches(items, budget_chars, cap=64):
    """Group items so one request stays inside the server's batch size.

    Batching by COUNT breaks the moment item length changes: 64 short
    messages fit easily, 64 document chunks do not. Budget by characters.
    """
    batch, size = [], 0
    for item in items:
        n = len(item["text"])
        if batch and (size + n > budget_chars or len(batch) >= cap):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += n
    if batch:
        yield batch
