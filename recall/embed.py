"""Embedding with health-aware retry.

A dead server and an oversized request both answer HTTP 502, and they need
opposite responses. See docs/lessons.md.
"""

import json
import time
import urllib.error
import urllib.request

from . import config


class EmbeddingMisconfigured(Exception):
    """The server answered, but not with what this configuration expects.

    A wrong dimension is a setup fault, not oversized input. It must never
    reach the bisect: shrinking the request cannot change the answer, so the
    bisect drops every chunk one at a time and the log blames the data. That
    is the 502 lesson wearing new clothes, and swapping the embedding model
    is an advertised thing to do.

    It inherits Exception, NOT RuntimeError, on purpose. The retry paths in
    embed_safe catch RuntimeError, and a base class they catch would let one
    of them swallow this silently.
    """


class EmbeddingServerDown(RuntimeError):
    """The server never came back. Stopping beats draining the corpus into a
    drop list, because stored work survives and the next run resumes."""


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
        raise EmbeddingMisconfigured(
            f"asked for {len(texts)} embeddings, got {len(vecs)}; "
            f"the server at {config.EMBED_URL} did not answer the whole batch")
    for v in vecs:
        if len(v) != config.EMBED_DIMS:
            raise EmbeddingMisconfigured(
                f"the model returns {len(v)} dimensions, RECALL_EMBED_DIMS "
                f"says {config.EMBED_DIMS}; set it to {len(v)} and re-create "
                f"the chunk table, whose vector column is fixed at the old "
                f"size")
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

    Checks health before bisecting, so a restarted server is never mistaken
    for oversized input.
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
    """Group items by character budget, not by count: 64 short messages fit
    where 64 document chunks do not."""
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
