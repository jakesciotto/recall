import unittest
from unittest import mock

from recall import db, judge


class Cursor:
    def __init__(self, log, result=None):
        self.log = log
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchone(self):
        return [self.result]


class Conn:
    def __init__(self, result=None):
        self.log = []
        self.result = result
        self.commits = 0

    def cursor(self):
        return Cursor(self.log, self.result)

    def commit(self):
        self.commits += 1


ROW = {"id": 7, "question": "who raced", "answer": "You did [1].", "k": 8}
SOURCES = [{"n": 1, "ref": "message:1", "text": "me: great first race man",
            "occurred_at": "2021-09-12T00:00:00Z", "source": "messages",
            "path": None},
           {"n": 2, "ref": "message:2", "text": "them: thanks!",
            "occurred_at": "2021-09-12T00:00:00Z", "source": "messages",
            "path": None}]


class TestSpeakerRule(unittest.TestCase):
    """The judge's instruction must name the same label the sources carry.
    An instruction about absent text teaches the model the wrong shape."""

    def test_with_no_label_it_describes_me(self):
        self.assertIn('"me:"', judge.speaker_rule(""))

    def test_with_a_label_it_names_the_label_and_not_me(self):
        rule = judge.speaker_rule("Ada")
        self.assertIn('"Ada:"', rule)
        self.assertNotIn('"me:"', rule)


class TestBuildPrompt(unittest.TestCase):
    def test_sources_keep_the_numbers_the_answer_cited(self):
        """A citation indexes the prompt. Renumber the sources and every
        grounding judgment is wrong."""
        prompt = judge.build_prompt(ROW, SOURCES, label="")
        self.assertIn("[1] message:1", prompt)
        self.assertIn("[2] message:2", prompt)

    def test_the_relabel_reaches_the_judge_too(self):
        """The judge must see the sources the way the answerer saw them."""
        prompt = judge.build_prompt(ROW, SOURCES, label="Ada")
        self.assertIn("Ada: great first race man", prompt)
        self.assertNotIn("me: great", prompt)

    def test_a_long_source_is_cut_and_the_judge_is_told(self):
        long = [dict(SOURCES[0], text="x" * 5000)]
        prompt = judge.build_prompt(ROW, long, label="")
        self.assertIn("[truncated]", prompt)
        self.assertIn("cut", judge.INSTRUCTIONS.lower())

    def test_the_marker_counts_against_the_limit(self):
        """Cutting to the limit and then appending overflows by exactly the
        marker length, which only shows at certain input sizes."""
        for n in (judge.MAX_SOURCE_CHARS - 1, judge.MAX_SOURCE_CHARS,
                  judge.MAX_SOURCE_CHARS + 1, judge.MAX_SOURCE_CHARS + 30):
            with self.subTest(chars=n):
                self.assertLessEqual(len(judge._clip("x" * n,
                                                     judge.MAX_SOURCE_CHARS)),
                                     judge.MAX_SOURCE_CHARS)

    def test_a_missing_answer_is_named_as_such(self):
        prompt = judge.build_prompt(dict(ROW, answer=None), SOURCES, label="")
        self.assertIn("produced no answer", prompt)


class TestParseVerdict(unittest.TestCase):
    """This is why the judge columns carry no CHECK constraint. A CHECK
    fails the whole batch on one strange word; normalising costs one row."""

    def test_json_in_a_code_fence(self):
        out = judge.parse_verdict('```json\n{"grounded": "yes", '
                                  '"retrieval": "partly", "hedged": "no", '
                                  '"question_type": "recall", "note": "ok"}\n```')
        self.assertEqual(out["judge_grounded"], "yes")
        self.assertEqual(out["judge_retrieval"], "partly")
        self.assertEqual(out["judge_question_type"], "recall")

    def test_json_wrapped_in_prose(self):
        out = judge.parse_verdict('Sure. {"grounded": "no"} Hope that helps.')
        self.assertEqual(out["judge_grounded"], "no")

    def test_a_strange_word_becomes_unknown_not_an_error(self):
        out = judge.parse_verdict('{"grounded": "mostly", "hedged": true}')
        self.assertEqual(out["judge_grounded"], "unknown")
        self.assertEqual(out["judge_hedged"], "yes")

    def test_no_json_at_all_is_all_unknown(self):
        out = judge.parse_verdict("I cannot say.")
        self.assertEqual(out["judge_grounded"], "unknown")
        self.assertEqual(out["judge_note"], "")

    def test_an_array_is_not_an_object(self):
        self.assertEqual(judge.parse_verdict("[1, 2]")["judge_grounded"],
                         "unknown")

    def test_the_note_is_bounded(self):
        out = judge.parse_verdict('{"note": "%s"}' % ("n" * 2000))
        self.assertLessEqual(len(out["judge_note"]), judge.MAX_NOTE_CHARS)


class TestJudgeRow(unittest.TestCase):
    def test_a_dead_request_becomes_one_unknown_row_not_a_dead_batch(self):
        def chat(prompt, model=None, timeout=600):
            raise OSError("refused")
        out = judge.judge_row(ROW, SOURCES, chat=chat, model="m")
        self.assertEqual(out["judge_grounded"], "unknown")
        self.assertIn("OSError", out["judge_note"])
        self.assertEqual(out["judge_model"], "m")

    def test_it_records_the_model_that_judged(self):
        chat = mock.Mock(return_value='{"grounded": "yes"}')
        out = judge.judge_row(ROW, SOURCES, chat=chat, model="critic")
        self.assertEqual(out["judge_model"], "critic")
        self.assertEqual(chat.call_args.kwargs["model"], "critic")


class TestSave(unittest.TestCase):
    def test_it_writes_only_the_judge_columns(self):
        """verdict and note belong to the human. The judge must not be able
        to overwrite ground truth even by accident."""
        conn = Conn()
        judge.save(conn, 7, {"judge_grounded": "yes", "judge_model": "m",
                             "verdict": "good", "note": "smuggled"})
        sql, params = conn.log[0]
        self.assertIn("judge_grounded", sql)
        self.assertIn("judged_at = now()", sql)
        self.assertNotIn("verdict", sql)
        self.assertNotIn("note =", sql)
        self.assertNotIn("smuggled", params)
        self.assertEqual(conn.commits, 1)


class TestQueue(unittest.TestCase):
    def test_unjudged_skips_rows_with_no_answer(self):
        """A sources-only request never asked for an answer, so there is
        nothing to grade. Judging it "no" skews every aggregate."""
        with mock.patch.object(db, "fetch", return_value=[]) as fetch:
            judge.unjudged(Conn(), 5)
        sql = fetch.call_args.args[1]
        self.assertIn("answer IS NOT NULL", sql)
        self.assertIn("judged_at IS NULL", sql)

    def test_redo_lifts_the_judged_filter(self):
        with mock.patch.object(db, "fetch", return_value=[]) as fetch:
            judge.unjudged(Conn(), 5, redo=True)
        self.assertNotIn("judged_at IS NULL", fetch.call_args.args[1])

    def test_sources_come_back_in_prompt_order(self):
        with mock.patch.object(db, "fetch", return_value=[]) as fetch:
            judge.sources_for(Conn(), 7)
        sql = fetch.call_args.args[1]
        self.assertIn("ORDER BY qc.final_rank", sql)
        self.assertIn("JOIN chunk", sql)


class TestRun(unittest.TestCase):
    def test_dry_run_judges_and_writes_nothing(self):
        conn = Conn()
        chat = mock.Mock(return_value='{"grounded": "yes"}')
        with mock.patch.object(judge, "unjudged", return_value=[ROW]), \
             mock.patch.object(judge, "sources_for", return_value=SOURCES):
            counts = judge.run(conn, limit=5, dry_run=True, chat=chat,
                               model="m", log=lambda *a, **k: None)
        self.assertEqual(counts, {"yes": 1})
        self.assertEqual(conn.log, [])

    def test_a_real_run_saves_each_verdict(self):
        conn = Conn()
        chat = mock.Mock(return_value='{"grounded": "partly"}')
        with mock.patch.object(judge, "unjudged", return_value=[ROW, ROW]), \
             mock.patch.object(judge, "sources_for", return_value=SOURCES):
            counts = judge.run(conn, limit=5, chat=chat, model="m",
                               log=lambda *a, **k: None)
        self.assertEqual(counts, {"partly": 2})
        self.assertEqual(len(conn.log), 2)
