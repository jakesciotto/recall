"""Who labelled a row, and what the row's label is.

Three labellers write to query_log and none writes another's columns: the
human through `recall review` (verdict, note, labelled_at), the judge
through `recall judge` (judge_*), an agent through `recall label`
(agent_*). Keeping them apart is what lets the judge be measured against
the human, and the agent against both.

The label of a row is the later of the human and the agent when both
spoke, the one that spoke when one did, and the judge's word otherwise.
The judge's `partly` is bad: the frame grades an answer against the
reference, and a partial answer does not state it.
"""

import datetime as dt
import re

from . import db

VERDICTS = ("good", "bad")
JUDGE_TO_LABEL = {"yes": "good", "no": "bad", "partly": "bad"}
AGENT_COLUMNS = ("agent_verdict", "agent_note", "agent_name", "agent_at")

COLUMNS = ("id, question, expected, expected_decline, answer, cited, asked_at, k, "
           "verdict, note, labelled_at, judge_correct, judge_declined, "
           "judge_question_type, judge_note, "
           "agent_verdict, agent_note, agent_name, agent_at")

_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?"
                    r"\s*(Z|[+-]\d{2}:?\d{2})?$")


def when(value):
    """A timestamp as db.fetch delivers it (Postgres JSON text, any number
    of fraction digits) or as a cursor does (datetime). Aware, UTC when
    naive. None stays None, and sorts before anything."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    m = _STAMP.match(str(value).strip())
    if not m:
        raise ValueError(f"not a timestamp: {value!r}")
    day, clock, fraction, zone = m.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    zone = "+00:00" if zone in (None, "Z") else zone
    if ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    stamp = dt.datetime.fromisoformat(f"{day}T{clock}{zone}")
    return stamp.replace(microsecond=micro)


def labeller(row):
    """Whose word the label is: human, agent, judge, or None."""
    human, agent = row.get("verdict"), row.get("agent_verdict")
    if human and agent:
        h, a = when(row.get("labelled_at")), when(row.get("agent_at"))
        return "human" if (h is not None and (a is None or h > a)) else "agent"
    if human:
        return "human"
    if agent:
        return "agent"
    if row.get("judge_correct") in JUDGE_TO_LABEL:
        return "judge"
    return None


def label(row):
    who = labeller(row)
    if who == "human":
        return row["verdict"]
    if who == "agent":
        return row["agent_verdict"]
    if who == "judge":
        return JUDGE_TO_LABEL[row["judge_correct"]]
    return None


def save_agent(conn, query_id, verdict, name, note):
    """Write an agent's label. Only the agent columns, never verdict, never
    a judge column. The name says which agent; the note says why, and a
    label without a reason is a coin, so both are required."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    if not (name or "").strip():
        raise ValueError("an agent label needs a name (--by)")
    if not (note or "").strip():
        raise ValueError("an agent label needs a note saying why (--note)")
    with conn.cursor() as cur:
        cur.execute("UPDATE query_log SET agent_verdict = %s, agent_note = %s, "
                    "agent_name = %s, agent_at = now() WHERE id = %s",
                    [verdict, note.strip(), name.strip(), int(query_id)])
    conn.commit()


def rows(conn):
    """Every row that carries an answer, with all three labellers' columns."""
    return db.fetch(conn, f"SELECT {COLUMNS} FROM query_log "
                          "WHERE answer IS NOT NULL ORDER BY id")


def _judge_word(row):
    return JUDGE_TO_LABEL.get(row.get("judge_correct"))


def is_open(row):
    """A human and the judge disagree, and no agent newer than the human
    label has ruled. A human relabel after an agent reopens the row."""
    human, judge = row.get("verdict"), _judge_word(row)
    if not human or not judge or human == judge:
        return False
    return labeller(row) == "human"


def open_disagreements(rows_):
    return [r for r in rows_ if is_open(r)]


def summarise(rows_):
    by_labeller = {"human": 0, "agent": 0, "judge": 0, "none": 0}
    by_type = {}
    good = labelled = 0
    both = agree = 0
    for r in rows_:
        by_labeller[labeller(r) or "none"] += 1
        word = label(r)
        if word:
            labelled += 1
            good += word == "good"
            t = by_type.setdefault(r.get("judge_question_type") or "untyped",
                                   {"good": 0, "labelled": 0})
            t["labelled"] += 1
            t["good"] += word == "good"
        if r.get("verdict") and _judge_word(r):
            both += 1
            agree += r["verdict"] == _judge_word(r)
    return {"rows": len(rows_), "by_labeller": by_labeller, "good": good,
            "labelled": labelled, "by_type": by_type,
            "calibration": {"both": both, "agree": agree},
            "open": sum(1 for r in rows_ if is_open(r))}


def format_report(summary, open_rows):
    """The one screen a human opens."""
    b = summary["by_labeller"]
    lines = [
        f"{summary['rows']} answered rows: {b['human']} labelled by a human, "
        f"{b['agent']} by an agent, {b['judge']} by the judge alone, "
        f"{b['none']} without a label",
        "",
        f"{summary['good']} good of {summary['labelled']} labelled",
    ]
    for name, t in sorted(summary["by_type"].items()):
        lines.append(f"  {name:<12} {t['good']:>4} good of {t['labelled']}")
    c = summary["calibration"]
    lines += ["", f"the judge agrees with the human on {c['agree']} of {c['both']} "
                  "rows both labelled"]
    if not open_rows:
        lines += ["", "no open disagreements"]
    else:
        lines += ["", f"{len(open_rows)} open disagreements (human / judge):"]
        for r in open_rows:
            lines.append(f"  #{r['id']:<5} {r.get('verdict'):<4} / "
                         f"{_judge_word(r):<4} {str(r.get('question'))[:60]}")
        lines.append("  recall show <id> reads one; recall label <id> rules on it")
    return "\n".join(lines)
