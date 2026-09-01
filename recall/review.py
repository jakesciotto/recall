"""Label logged answers by hand. The ground truth the model judge lacks.

  recall review
  recall review --limit 5
  recall review --redo        # revisit rows you already labelled

judge.py writes an estimate. This writes the truth. The two never overlap:
the judge cannot write verdict or note, and this cannot write any judge_*
column. Neither side may overwrite the other, or comparing them is
circular.

**The judge's verdict stays hidden until you decide.** Showing it first
anchors you to it, the two then agree more often than they should, and the
measurement quietly becomes worthless. The review screen carries the
question, the answer, and the sources, and nothing else. The judge's
opinion prints after your keypress.

Nothing here touches chunk, and nothing runs on the answer path.
"""

from . import answer, config, db, judge

VERDICTS = ("good", "bad")

# One key each, so a pass over twenty rows costs a few minutes rather than
# an evening. Anything else is not an action, which is what stops a stray
# Enter from labelling a row.
KEYS = {"g": "good", "b": "bad", "s": "skip", "q": "quit"}

MAX_SOURCE_CHARS = 700
MAX_ANSWER_CHARS = 4_000


def parse_action(key):
    """One keypress as an action, or None when it is not one."""
    return KEYS.get((key or "").strip().lower())


def unlabelled(conn, limit, redo=False):
    """Rows waiting for your label, newest first.

    Newest first because recall decays. You remember a question from an
    hour ago and you do not remember one from three weeks back, so an old
    row invites a guess, and a guessed label is worse than a missing one.

    A row with no answer is skipped for the same reason the judge skips it:
    a sources-only request never asked for an answer.
    """
    parts = ["answer IS NOT NULL"]
    if not redo:
        parts.append("verdict IS NULL")
    return db.fetch(conn, "SELECT id, question, answer, asked_at, k, verdict, "
                          "note, judge_grounded, judge_retrieval, judge_hedged, "
                          "judge_question_type, judge_note "
                          f"FROM query_log WHERE {' AND '.join(parts)} "
                          f"ORDER BY id DESC LIMIT {int(limit)}")


def _clip(text, limit):
    return judge._clip((text or "").strip(), limit)


def format_row(row, sources, label=None):
    """The review screen. Carries no judge opinion, on purpose."""
    label = config.USER_LABEL if label is None else label
    lines = [
        "=" * 72,
        f"#{row['id']}   asked {str(row.get('asked_at'))[:19]}   k={row.get('k')}",
        "",
        f"QUESTION  {row.get('question')}",
        "",
        "ANSWER",
        _clip(row.get("answer"), MAX_ANSWER_CHARS),
        "",
        f"SOURCES ({len(sources)})",
    ]
    if not sources:
        lines.append("  none")
    for s in sources:
        where = s.get("path") or s.get("ref")
        day = (s.get("occurred_at") or "undated")[:10]
        lines.append(f"  [{s['n']}] {where}  ({day}, {s.get('source')})")
        lines.append(f"      {_clip(answer.speaker_labels(s.get('text'), label), MAX_SOURCE_CHARS)}")
    return "\n".join(lines)


def format_judge(row):
    """What the model thought. Printed only after you decide."""
    if not row.get("judge_grounded"):
        return "  judge: not judged yet"
    return (f"  judge: grounded={row.get('judge_grounded')} "
            f"retrieval={row.get('judge_retrieval')} "
            f"hedged={row.get('judge_hedged')} "
            f"type={row.get('judge_question_type')}\n"
            f"    {row.get('judge_note') or ''}")


def save_verdict(conn, query_id, verdict, note=None):
    """Write your label. Only verdict and note, never a judge column.

    An unknown verdict raises rather than writing. The set is small and
    fixed, and a typo becoming a third category would split the very
    counts this exists to produce.

    An empty note is NULL, not "". NULL means unanswered; an empty string
    means answered with nothing, and a later count cannot tell them apart.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    note = note if (note or "").strip() else None
    with conn.cursor() as cur:
        cur.execute("UPDATE query_log SET verdict = %s, note = %s WHERE id = %s",
                    [verdict, note, int(query_id)])
    conn.commit()


def run(conn, limit, redo=False, ask=input, log=print):
    """The labelling loop. Returns how many rows were labelled."""
    rows = unlabelled(conn, limit, redo=redo)
    if not rows:
        log("nothing to label")
        return 0
    log(f"{len(rows)} to review. g=good  b=bad  s=skip  q=quit\n")
    labelled = 0
    for row in rows:
        log(format_row(row, judge.sources_for(conn, row["id"])))
        action = None
        while action is None:
            action = parse_action(ask("\n  [g/b/s/q] "))
            if action is None:
                log("  press g, b, s, or q")
        if action == "quit":
            break
        # The judge's opinion appears only now, after the decision is made.
        log(format_judge(row))
        if action == "skip":
            log("  skipped\n")
            continue
        note = ask("  note (optional): ")
        save_verdict(conn, row["id"], action, note)
        labelled += 1
        log(f"  saved: {action}\n")
    log(f"\nlabelled {labelled} of {len(rows)}")
    return labelled
