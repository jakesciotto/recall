"""Judge logged answers with a model. See docs/evaluating.md.

Deterministic checks come free from the query log: how many sources the
model cited, where they ranked, how long each stage took. They cannot tell
you whether an answer was grounded in what it cited, whether retrieval
surfaced the right thing at all, or whether the model hedged while holding
the evidence. Those need a reader.

**The judge should be a different model from the answerer.** A model grading
its own output shows self-preference bias. RECALL_JUDGE_MODEL names the
judge, and it defaults to the answering model only because recall does not
choose your models for you.

**The judge writes an estimate, never the truth.** It fills the judge_*
columns and nothing else. verdict and note belong to the human, and the
judge cannot write them even by accident. That separation is what makes
the judge measurable later, and on the one attribution case measured so
far, it needed measuring: see docs/evaluating.md.

Nothing here touches chunk, and nothing here runs on the answer path.
"""

import datetime as dt
import json

from . import answer, config, db

# Eight sources at 8,000 characters is a 64,000 character prompt. The judge
# is told the text is cut, because a judge that does not know will call a
# grounded answer ungrounded.
MAX_SOURCE_CHARS = 1_200
MAX_ANSWER_CHARS = 6_000
MAX_NOTE_CHARS = 500

TRI = ("yes", "partly", "no")
BOOLISH = ("yes", "no")
QUESTION_TYPES = ("recall", "date", "summary", "open")

# The judge writes only these.
FIELDS = ("judge_grounded", "judge_retrieval", "judge_hedged",
          "judge_question_type", "judge_note", "judge_model")

INSTRUCTIONS = """You grade an answer that a retrieval system produced from a
personal archive. Judge only what the sources support. Do not use outside
knowledge. Long sources are cut; a cut source is not a missing one.

Reply with JSON and nothing else:

{"grounded": "yes|partly|no",
 "retrieval": "yes|partly|no",
 "hedged": "yes|no",
 "question_type": "recall|date|summary|open",
 "note": "one sentence"}

grounded: does every claim in the answer trace to a source it cited, AND
does it name the right person? {speaker_rule} Read who is speaking and who
is being spoken about: a line where the user says "great first race man"
means the user is congratulating somebody else, so the race is not the
user's. An answer that attributes another person's experience to the user
is NOT grounded, however well it cites. Misattribution is a grounding
failure, not a style problem.
retrieval: did the sources contain what the question needed?
hedged: did the answer decline or hedge although the sources held the answer?
question_type: recall is a fact from the archive, date is a when question,
summary asks for a synthesis, open is anything broader."""

_TRUNCATED = " ...[truncated]"


def speaker_rule(label):
    """The one sentence that names the user's label in the sources.

    It must match what the prompt actually carries. With a label set, every
    "me:" line has been rewritten, so describing "me:" would point the
    judge at text that is not there.
    """
    if label:
        return (f'Message sources are conversations. Each line names its '
                f'speaker, and a line starting with "{label}:" is the user.')
    return ('Message sources are conversations. Each line names its '
            'speaker, and a line starting with "me:" is the user.')


def _clip(text, limit):
    """Text no longer than `limit`, marker included.

    The marker counts against the budget. Cutting to the limit and then
    appending overflows by exactly the marker length, which is a bug that
    only shows at certain input sizes.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - len(_TRUNCATED))] + _TRUNCATED


def build_prompt(row, sources, label=None):
    """The judging prompt: the question, the numbered sources, the answer.

    The sources keep the numbering the answer cited, because a citation
    indexes the prompt. Renumber them and every grounding judgment is
    wrong. The sources are relabelled exactly as the answerer saw them.
    """
    label = config.USER_LABEL if label is None else label
    blocks = []
    for s in sources:
        where = s.get("path") or s.get("ref")
        day = (s.get("occurred_at") or "undated")[:10]
        text = answer.speaker_labels(s.get("text"), label)
        blocks.append(f"[{s['n']}] {where} ({day}, {s.get('source')})\n"
                      f"{_clip(text, MAX_SOURCE_CHARS)}")
    text = row.get("answer") or "(the system produced no answer)"
    return (INSTRUCTIONS.replace("{speaker_rule}", speaker_rule(label)) + "\n\n"
            f"Question: {row.get('question')}\n\n"
            f"Sources:\n\n" + "\n\n".join(blocks) + "\n\n"
            f"Answer:\n{_clip(text, MAX_ANSWER_CHARS)}\n")


def _json_object(text):
    """The first JSON object in whatever the model wrote.

    A model wraps JSON in a code fence, or in prose, or writes an array.
    This finds the outermost object and gives up quietly rather than
    raising: the caller turns a failure into one 'unknown' row, never a
    dead batch.
    """
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        out = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _one_of(value, allowed):
    """A value from the allowed set, or 'unknown'.

    This is why the judge columns carry no CHECK constraint. A CHECK would
    fail the whole batch on one strange word; normalising costs one row.
    """
    if isinstance(value, bool):
        value = "yes" if value else "no"
    text = str(value or "").strip().lower()
    return text if text in allowed else "unknown"


def parse_verdict(text):
    """A model reply as the judge columns. Never raises."""
    data = _json_object(text)
    return {
        "judge_grounded": _one_of(data.get("grounded"), TRI),
        "judge_retrieval": _one_of(data.get("retrieval"), TRI),
        "judge_hedged": _one_of(data.get("hedged"), BOOLISH),
        "judge_question_type": _one_of(data.get("question_type"),
                                       QUESTION_TYPES),
        "judge_note": _clip(str(data.get("note") or ""), MAX_NOTE_CHARS),
    }


def judge_model():
    return config.JUDGE_MODEL or config.CHAT_MODEL


def judge_row(row, sources, chat=None, model=None):
    """Judge one logged answer. Never raises.

    One dead request must not end a batch of two hundred, so a failure
    comes back as an 'unknown' verdict carrying the error in its note.
    """
    chat = chat or answer.chat
    model = model or judge_model()
    try:
        out = parse_verdict(chat(build_prompt(row, sources), model=model))
    except Exception as e:
        out = parse_verdict(None)
        out["judge_note"] = f"{type(e).__name__}: {e}"[:MAX_NOTE_CHARS]
    out["judge_model"] = model
    return out


def save(conn, query_id, fields):
    """Write one judgment. Only query_log, and only the judge columns."""
    keep = [(k, v) for k, v in fields.items() if k in FIELDS]
    sets = ", ".join(f"{k} = %s" for k, _ in keep) + ", judged_at = now()"
    with conn.cursor() as cur:
        cur.execute(f"UPDATE query_log SET {sets} WHERE id = %s",
                    [v for _, v in keep] + [int(query_id)])
    conn.commit()


def unjudged(conn, limit, redo=False):
    """Logged questions waiting for a judgment, oldest first.

    A row with no answer is skipped, always. A sources-only request never
    asked for an answer, so there is nothing to grade, and judging it "no"
    is a false negative that skews every aggregate this log exists for.
    """
    parts = ["answer IS NOT NULL"]
    if not redo:
        parts.append("judged_at IS NULL")
    return db.fetch(conn, "SELECT id, question, answer, k, date_phrase "
                          f"FROM query_log WHERE {' AND '.join(parts)} "
                          f"ORDER BY id LIMIT {int(limit)}")


def sources_for(conn, query_id):
    """The sources that reached the prompt, in the order the answer cited.

    The log stores refs, and the text lives in chunk. This join is why both
    tables share one database.
    """
    return db.fetch(conn, "SELECT qc.final_rank AS n, qc.ref, c.text, "
                          "c.occurred_at, c.source, c.path "
                          "FROM query_candidate qc JOIN chunk c ON c.ref = qc.ref "
                          "WHERE qc.query_id = %s AND qc.final_rank IS NOT NULL "
                          "ORDER BY qc.final_rank", [int(query_id)])


def run(conn, limit, redo=False, dry_run=False, chat=None, model=None,
        log=print):
    """Judge a batch. Returns {grounded verdict: count}."""
    model = model or judge_model()
    rows = unjudged(conn, limit, redo=redo)
    if not rows:
        log("nothing to judge")
        return {}
    log(f"judging {len(rows):,} rows with {model}")
    counts = {}
    for row in rows:
        verdict = judge_row(row, sources_for(conn, row["id"]), chat=chat,
                            model=model)
        counts[verdict["judge_grounded"]] = \
            counts.get(verdict["judge_grounded"], 0) + 1
        if not dry_run:
            save(conn, row["id"], verdict)
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        log(f"  {stamp}  #{row['id']:<5} grounded={verdict['judge_grounded']:<7}"
            f" retrieval={verdict['judge_retrieval']:<7}"
            f" type={verdict['judge_question_type']:<8}"
            f" {str(row['question'])[:44]}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    log(f"judged {len(rows):,} rows. grounded: {summary}")
    return counts
