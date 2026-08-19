"""Parse the model's Markdown answer into a structure any UI can render.

The model answers in Markdown. Both front ends escaped it and rendered it
literally, so bullets showed as "*   " and bold as "**text**".

Stripping the Markdown would have been the cheaper fix and is the wrong one:
the structure is real information. A "summarize everything" answer uses
bullets and bold to separate distinct findings, and flattening it produces a
wall of text that reads worse than the stray asterisks.

This parses once, on the server, into blocks and spans. **No HTML string ever
crosses the wire.** The front ends build their own DOM nodes or React
elements from the structure, so archive text can never arrive as markup, and
one tested implementation replaces two untested JavaScript ones.
"""

import re

# Multi-number groups are the trap. The model writes "[4, 5]", so a pattern
# for a single number silently misses half the citations in an answer.
_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_BOLD = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*", re.S)
_BULLET = re.compile(r"^(\s*)[*-]\s+(.*)$")
# The model produces two levels and the UI needs no more. Capping stops one
# stray over-indented line from nesting the whole answer.
MAX_DEPTH = 1
# A section heading arrives as a line that is entirely bold, not as "#".
_HEADING = re.compile(r"^\*\*(.+?)\*\*:?\s*$")


def _cite_spans(text):
    """Split plain text into text spans and cite spans."""
    out, last = [], 0
    for m in _CITE.finditer(text):
        if m.start() > last:
            out.append({"text": text[last:m.start()]})
        out.append({"type": "cite",
                    "n": [int(x) for x in m.group(1).split(",")]})
        last = m.end()
    if last < len(text):
        out.append({"text": text[last:]})
    return out


def spans(text):
    """Inline spans for one line: plain text, bold text, and citations.

    An unclosed "**" stays literal. A lone pair of asterisks is not emphasis,
    and swallowing it would delete a character the user actually wrote.
    """
    out, last = [], 0
    for m in _BOLD.finditer(text):
        if m.start() > last:
            out.extend(_cite_spans(text[last:m.start()]))
        for span in _cite_spans(m.group(1)):
            span["bold"] = True
            out.append(span)
        last = m.end()
    if last < len(text):
        out.extend(_cite_spans(text[last:]))
    return [s for s in out if s.get("text") or s.get("type")]


def blocks(answer):
    """[{type: paragraph, spans}] and [{type: list, items: [spans]}]."""
    if not answer:
        return []
    out = []
    para, items = [], []

    def flush():
        nonlocal para, items
        if items:
            out.append({"type": "list", "items": items})
            items = []
        if para:
            out.append({"type": "paragraph", "spans": spans(" ".join(para))})
            para = []

    for line in answer.splitlines():
        if not line.strip():
            flush()
            continue

        h = _HEADING.match(line.strip())
        if h:
            flush()
            out.append({"type": "heading", "text": h.group(1).strip()})
            continue

        m = _BULLET.match(line)
        if m:
            if para:
                out.append({"type": "paragraph",
                            "spans": spans(" ".join(para))})
                para = []
            depth = min(len(m.group(1).replace("\t", "    ")) // 4, MAX_DEPTH)
            items.append({"depth": depth, "spans": spans(m.group(2).strip())})
            continue

        if items:
            flush()
        para.append(line.strip())
    flush()
    return out
