"""Ask a whole question file, so the log fills in one sitting.

  recall eval ~/my-questions.md

Numbered lines are questions. Headings, prose and bullets are not, so the
file can carry notes about what each group of questions tests. An
`expect:` line under a question is the author's reference, and it travels
with the question into the log: the review shows it before the keypress
and the judge grades against it. A `decline: yes` line under the reference
says a whole decline is the correct answer, which the author knows once
per question and a model could not read from the reference text. Every
question is logged with client = eval, which keeps a batch separable from
the questions you ask by hand.

Nothing is scored here. The judge and the review do that, on the log this
fills. See docs/evaluating.md.
"""

import datetime as dt
import re
import time

from . import answer, config, db, querylog, retrieve

_NUMBERED = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
_EXPECT = re.compile(r"^\s*expect:\s*(.+?)\s*$", re.I)
_DECLINE = re.compile(r"^\s*decline:\s*(\S+)\s*$", re.I)


def parse(text):
    """(question, reference, decline) triples in file order.

    reference is None when no expect: line follows the question. decline
    is "yes" when a decline: yes line follows, "no" when a reference exists
    without one, and None when there is neither. An expect: or decline:
    line before any question belongs to nothing and is dropped.
    """
    rows = []
    for line in text.splitlines():
        if m := _NUMBERED.match(line):
            rows.append([m.group(1), None, None])
        elif (m := _EXPECT.match(line)) and rows:
            rows[-1][1] = m.group(1)
            rows[-1][2] = rows[-1][2] or "no"
        elif (m := _DECLINE.match(line)) and rows:
            rows[-1][2] = "yes" if m.group(1).lower() == "yes" else "no"
    return [tuple(r) for r in rows]


def questions(text):
    return [q for q, _, _ in parse(text)]


def _ms(started):
    return int((time.monotonic() - started) * 1000)


def ask_one(question, embedder, k=retrieve.TOP_K, expected=None,
            expected_decline=None):
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
                 total_ms=_ms(started), error=error, expected=expected,
                 expected_decline=expected_decline)
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
    for i, (q, expected, decline) in enumerate(qs, start=1):
        started = time.monotonic()
        text, cited, error = ask_one(q, embedder, k=k, expected=expected,
                                     expected_decline=decline)
        if error:
            status = f"ERROR {error[:50]}"
        elif text is None:
            status = "sources only"
        else:
            status = f"cited {len(cited)} of {k}"
        log(f"  {i:>3}. {status:<24} {_ms(started):>6} ms  {q[:60]}")
    log(f"\nasked {len(qs)}. next:  recall judge, then recall review")
    return len(qs)
