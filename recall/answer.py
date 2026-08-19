"""Grounded answers from the local model.

Citations are mandatory, and an empty retrieval produces a prompt that
forbids answering. See docs/lessons.md.
"""

import json
import urllib.request

from . import config

SYSTEM = (
    "You answer questions about the user's own archive of documents, "
    "messages, and mail. Answer only from the numbered sources given to you. "
    "Cite the sources you use by their number, like [2]. If the sources do "
    "not contain the answer, say so plainly and do not guess. Never invent a "
    "document, a date, or a quotation."
)


def cite(hit):
    where = hit.get("path") or hit["ref"]
    day = (hit.get("occurred_at") or "")[:10] or "undated"
    return f"{where} ({day})"


def build_prompt(question, hits):
    if not hits:
        return (f"No sources matched this question.\n\nQuestion: {question}\n\n"
                "Tell the user that the archive returned no sources, and do "
                "not answer from your own knowledge.")
    blocks = [f"[{i}] {cite(h)}\n{h['text']}"
              for i, h in enumerate(hits, start=1)]
    return ("Sources:\n\n" + "\n\n".join(blocks) +
            f"\n\nQuestion: {question}\n\n"
            "Answer using only the sources above, and cite each claim by its "
            "number. If the sources do not answer the question, say so.")


def _request(prompt, model, stream):
    body = json.dumps({
        "model": model or config.CHAT_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
        "stream": stream,
    }).encode()
    return urllib.request.Request(config.CHAT_URL, body,
                                  {"Content-Type": "application/json"})


def chat(prompt, model=None, timeout=600):
    with urllib.request.urlopen(_request(prompt, model, False), timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def chat_stream(prompt, model=None, timeout=600):
    """Yield text as the model produces it.

    A malformed frame is skipped rather than ending the stream: truncating a
    live answer is worse than dropping a token.
    """
    with urllib.request.urlopen(_request(prompt, model, True), timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                delta = json.loads(payload)["choices"][0].get("delta") or {}
            except (ValueError, KeyError, IndexError):
                continue
            if delta.get("content"):
                yield delta["content"]
