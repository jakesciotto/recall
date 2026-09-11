"""Ask a whole question file, so the log fills in one sitting.

  recall eval ~/my-questions.md

Numbered lines are questions. Headings, prose and bullets are not, so the
file can carry notes about what each group of questions tests. An
`expect:` line under a question is the author's reference, and it travels
with the question into the log: the review shows it before the keypress
and the judge grades against it. Every question is logged with
client = eval, which keeps a batch separable from the questions you ask
by hand.

Nothing is scored here. The judge and the review do that, on the log this
fills. See docs/evaluating.md.
"""

import datetime as dt
import re
import time

from . import answer, config, db, querylog, retrieve

_NUMBERED = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
_EXPECT = re.compile(r"^\s*expect:\s*(.+?)\s*$", re.I)


def parse(text):
    """(question, reference) pairs in file order; reference is None when
    no expect: line follows the question. An expect: line before any
    question belongs to nothing and is dropped."""
    pairs = []
    for line in text.splitlines():
        if m := _NUMBERED.match(line):
            pairs.append([m.group(1), None])
        elif (m := _EXPECT.match(line)) and pairs:
            pairs[-1][1] = m.group(1)
    return [tuple(p) for p in pairs]


def questions(text):
    return [q for q, _ in parse(text)]


def _ms(started):
    return int((time.monotonic() - started) * 1000)


def ask_one(question, embedder, k=retrieve.TOP_K, expected=None):
    """Ask, answer if an endpoint is set, log. Returns (answer, cited, error).

    A failed answer is logged with its error and the batch continues. One
    dead request must not end a batch of fifty.
    """
    started = time.monotonic()
    with db.connect() as conn:
        hits, dates, trace = retrieve.search_traced(question, conn, embedder,
                                                    k=k, today=dt.date.today())
    prompt = answer.build_prompt(question, hits)
    text, meta, error = None, {}, None
    if config.CHAT_URL:
        try:
            text = answer.chat(prompt, meta=meta)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    querylog.log(client="eval", question=question, k=k, pool=retrieve.POOL,
                 source=None, dates=dates, trace=trace, hits=hits, answer=text,
                 meta=meta, model_requested=config.CHAT_MODEL,
                 prompt_chars=len(prompt), streamed=False,
                 total_ms=_ms(started), error=error, expected=expected)
    cited, _ = querylog.cited_numbers(text, len(hits))
    return text, cited, error


def run(path, embedder, k=retrieve.TOP_K, log=print):
    """Ask every question in the file. Returns how many were asked."""
    qs = parse(open(path, encoding="utf-8").read())
    if not qs:
        log(f"no numbered questions in {path}")
        return 0
    if not config.CHAT_URL:
        log("no generation endpoint set; logging retrieval only. "
            "See docs/answering.md.")
    log(f"asking {len(qs)} questions from {path}\n")
    for i, (q, expected) in enumerate(qs, start=1):
        started = time.monotonic()
        text, cited, error = ask_one(q, embedder, k=k, expected=expected)
        if error:
            status = f"ERROR {error[:50]}"
        elif text is None:
            status = "sources only"
        else:
            status = f"cited {len(cited)} of {k}"
        log(f"  {i:>3}. {status:<24} {_ms(started):>6} ms  {q[:60]}")
    log(f"\nasked {len(qs)}. next:  recall judge, then recall review")
    return len(qs)
