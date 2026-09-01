"""Grounded answers from the local model.

Citations are mandatory, and an empty retrieval produces a prompt that
forbids answering. See docs/lessons.md.
"""

import json
import re
import urllib.request

from . import config

SYSTEM = (
    "You answer questions about the user's own archive of documents, "
    "messages, and mail. Answer only from the numbered sources given to you. "
    "Cite the sources you use by their number, like [2]. If the sources do "
    "not contain the answer, say so plainly and do not guess. Never invent a "
    "document, a date, or a quotation. "
    "Message sources are conversations, and every line names its speaker. "
    "Attribute every statement, event, and opinion to the speaker who "
    "actually said it, and remember that a speaker often talks ABOUT "
    "somebody else. Never report another person's experience as the user's "
    "own, and say whose experience it is when you report it. If a source "
    "does not make the speaker clear, say so rather than assuming the user."
)


def system_prompt(label=None):
    """SYSTEM with the user's own name filled in, when one is configured.

    The prompt must name the same label the sources carry. An earlier
    version told the model that "me:" marks the user while speaker_labels
    had already rewritten every such line, so the instruction pointed at a
    string that no longer appeared. An instruction about absent text is
    worse than no instruction: it teaches the model the wrong shape.
    """
    label = config.USER_LABEL if label is None else label
    if not label:
        return SYSTEM
    return SYSTEM.replace("the user", f"the user ({label})", 1)


# Anchored to the start of a line. A pattern that matched anywhere would
# rewrite "tell me: what time" and any word ending in "me".
_ME_LINE = re.compile(r"(?m)^me:")


def speaker_labels(text, label=None):
    """Replace the "me:" speaker label with the user's name.

    That label binds the subject to the user: asked who did a half ironman,
    a model answered "the user (me)" from a source where the user was
    congratulating somebody else. Relabelling the same line with a name,
    and changing nothing else, made the same model name the right person.

    This happens at prompt build time, never in storage. Every embedding
    was computed from the stored text, and rewriting the corpus would make
    every vector stale for no retrieval gain.
    """
    label = config.USER_LABEL if label is None else label
    if not label:
        return text or ""
    return _ME_LINE.sub(f"{label}:", text or "")


def cite(hit):
    where = hit.get("path") or hit["ref"]
    day = (hit.get("occurred_at") or "")[:10] or "undated"
    return f"{where} ({day})"


def build_prompt(question, hits, label=None):
    if not hits:
        return (f"No sources matched this question.\n\nQuestion: {question}\n\n"
                "Tell the user that the archive returned no sources, and do "
                "not answer from your own knowledge.")
    blocks = [f"[{i}] {cite(h)}\n{speaker_labels(h['text'], label)}"
              for i, h in enumerate(hits, start=1)]
    return ("Sources:\n\n" + "\n\n".join(blocks) +
            f"\n\nQuestion: {question}\n\n"
            "Answer using only the sources above, and cite each claim by its "
            "number. If the sources do not answer the question, say so.")


def _request(prompt, model, stream):
    body = json.dumps({
        "model": model or config.CHAT_MODEL,
        "messages": [{"role": "system", "content": system_prompt()},
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
