import datetime as dt
import re
import unittest

from recall import db, labels
from tests.test_judge import Conn

HUMAN_OLD = "2026-10-03T16:00:00+00:00"
AGENT = "2026-10-13T10:00:00-06:00"
HUMAN_NEW = "2026-10-14T09:00:00+00:00"


def row(**fields):
    base = {"id": 7, "verdict": None, "labelled_at": None, "judge_correct": None,
            "agent_verdict": None, "agent_at": None}
    base.update(fields)
    return base


class TestLabel(unittest.TestCase):
    """Three labellers, one answer per row. The judge's word stands only
    when neither a human nor an agent labelled the row."""

    def test_the_judge_labels_only_an_unlabelled_row(self):
        cases = {
            "human alone": (row(verdict="good", judge_correct="no"), "good", "human"),
            "agent alone": (row(agent_verdict="bad", agent_at=AGENT,
                                judge_correct="yes"), "bad", "agent"),
            "agent after human": (row(verdict="good", labelled_at=HUMAN_OLD,
                                      agent_verdict="bad", agent_at=AGENT), "bad", "agent"),
            "human after agent": (row(verdict="good", labelled_at=HUMAN_NEW,
                                      agent_verdict="bad", agent_at=AGENT), "good", "human"),
            "judge alone": (row(judge_correct="yes"), "good", "judge"),
            "nothing": (row(), None, None),
        }
        for name, (r, want, who) in cases.items():
            with self.subTest(name):
                self.assertEqual(labels.label(r), want)
                self.assertEqual(labels.labeller(r), who)

    def test_a_human_verdict_without_a_stamp_is_older_than_any_agent(self):
        r = row(verdict="good", labelled_at=None, agent_verdict="bad", agent_at=AGENT)
        self.assertEqual(labels.label(r), "bad")

    def test_partly_is_bad(self):
        self.assertEqual(labels.label(row(judge_correct="partly")), "bad")
        self.assertEqual(labels.label(row(judge_correct="no")), "bad")

    def test_a_judge_word_outside_the_set_is_no_label(self):
        self.assertIsNone(labels.label(row(judge_correct="unknown")))

    def test_timestamps_arrive_as_postgres_json_or_as_datetimes(self):
        """db.fetch hands back ISO text with however many fraction digits
        Postgres kept; a live cursor hands back datetimes. Both compare."""
        texts = ["2026-10-03T16:23:07.48226-06:00", "2026-10-03T22:23:07Z",
                 "2026-10-03 22:23:07.482263+00:00"]
        parsed = [labels.when(t) for t in texts]
        for t, got in zip(texts, parsed):
            with self.subTest(t):
                self.assertIsInstance(got, dt.datetime)
                self.assertIsNotNone(got.tzinfo)
        self.assertEqual(parsed[1], parsed[2].replace(microsecond=0))
        naive = dt.datetime(2026, 10, 3, 22, 23, 7)
        self.assertEqual(labels.when(naive).tzinfo, dt.timezone.utc)
        self.assertIsNone(labels.when(None))


class TestSaveAgent(unittest.TestCase):
    def test_it_writes_only_the_agent_columns(self):
        """A human verdict and a judge column are other labellers' words.
        Neither side may overwrite the other, or comparing them is circular."""
        conn = Conn()
        labels.save_agent(conn, 7, "bad", "claude-fable-5.1", "the reference holds the fact")
        self.assertEqual(len(conn.log), 1)
        sql, params = conn.log[0]
        touched = set(re.findall(r"(\w+) = ", sql)) - {"id"}
        self.assertEqual(touched, set(labels.AGENT_COLUMNS))
        self.assertNotIn("judge_", sql)
        self.assertEqual(params, ["bad", "the reference holds the fact", "claude-fable-5.1", 7])
        self.assertEqual(conn.commits, 1)

    def test_an_unknown_verdict_raises(self):
        with self.assertRaises(ValueError):
            labels.save_agent(Conn(), 7, "partly", "x", "why")

    def test_an_empty_note_raises(self):
        """The reason is the point. An agent label without one is a coin."""
        for note in ("", "  ", None):
            with self.subTest(note=note), self.assertRaises(ValueError):
                labels.save_agent(Conn(), 7, "good", "x", note)

    def test_an_empty_name_raises(self):
        with self.assertRaises(ValueError):
            labels.save_agent(Conn(), 7, "good", " ", "why")


LOG = [
    row(id=1, verdict="good", judge_correct="yes", judge_question_type="recall"),
    row(id=2, verdict="bad", judge_correct="no", judge_question_type="recall"),
    row(id=3, verdict="bad", judge_correct="partly", judge_question_type="aggregate"),
    row(id=4, verdict="good", judge_correct="no", judge_question_type="aggregate"),
    row(id=5, verdict="good", labelled_at=HUMAN_OLD, judge_correct="no",
        agent_verdict="bad", agent_at=AGENT, judge_question_type="recall"),
    row(id=6, verdict="good", labelled_at=HUMAN_NEW, judge_correct="no",
        agent_verdict="bad", agent_at=AGENT, judge_question_type="recall"),
    row(id=7, judge_correct="yes", judge_question_type="aggregate"),
    row(id=8, judge_correct=None, judge_question_type=None),
    row(id=9, verdict="good", judge_correct=None, judge_question_type="recall"),
]


class TestOpenDisagreements(unittest.TestCase):
    def test_a_disagreement_is_open_until_an_agent_newer_than_the_human_rules(self):
        open_ids = [r["id"] for r in labels.open_disagreements(LOG)]
        # 4: human good, judge no, nobody ruled.
        # 6: the agent ruled, then the human relabelled, so it is open again.
        # 5: the agent ruled after the human. 1, 2, 3 agree. 7, 8, 9 lack a side.
        self.assertEqual(open_ids, [4, 6])


class TestSummary(unittest.TestCase):
    def test_counts_by_labeller(self):
        s = labels.summarise(LOG)
        self.assertEqual(s["by_labeller"], {"human": 6, "agent": 1, "judge": 1, "none": 1})
        self.assertEqual(s["rows"], 9)

    def test_the_rate_uses_the_effective_label(self):
        s = labels.summarise(LOG)
        # labels: 1 good, 2 bad, 3 bad, 4 good, 5 bad (agent), 6 good (human),
        # recall rows 1, 2, 5, 6, 9: good 3 of 5. aggregate 3, 4, 7: good 2 of 3.
        # 7 good (judge), 8 none, 9 good. good 5 of 8 labelled.
        self.assertEqual(s["good"], 5)
        self.assertEqual(s["labelled"], 8)
        self.assertEqual(s["by_type"]["recall"], {"good": 3, "labelled": 5})
        self.assertEqual(s["by_type"]["aggregate"], {"good": 2, "labelled": 3})

    def test_calibration_counts_judge_against_human_only(self):
        s = labels.summarise(LOG)
        # rows with both: 1 agree, 2 agree, 3 agree (partly=bad), 4 differ,
        # 5 differ, 6 differ. 9 has no judge word.
        self.assertEqual(s["calibration"], {"both": 6, "agree": 3})
        self.assertEqual(s["open"], 2)


class TestReport(unittest.TestCase):
    def test_the_report_names_every_open_disagreement(self):
        text = labels.format_report(labels.summarise(LOG), labels.open_disagreements(LOG))
        self.assertIn("#4", text)
        self.assertIn("#6", text)
        self.assertNotIn("#5", text)
        self.assertIn("5 good of 8", text)
        self.assertIn("agrees with the human on 3 of 6", text)

    def test_a_clean_log_says_so(self):
        text = labels.format_report(labels.summarise(LOG[:3]), [])
        self.assertIn("no open disagreements", text)


class TestFetch(unittest.TestCase):
    def test_labelled_rows_come_through_db_fetch(self):
        conn = Conn(result=LOG)
        self.assertEqual(len(labels.rows(conn)), 9)
        sql = conn.log[0][0]
        self.assertIn("answer IS NOT NULL", sql)
        for col in labels.AGENT_COLUMNS + ("labelled_at", "judge_correct", "verdict"):
            self.assertIn(col, sql)
