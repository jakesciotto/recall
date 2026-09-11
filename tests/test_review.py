import unittest
from unittest import mock

from recall import db, review
from tests.test_judge import Conn

ROW = {"id": 7, "question": "who raced", "answer": "You did [1].",
       "asked_at": "2026-08-30T10:00:00Z", "k": 8, "verdict": None,
       "note": None, "judge_grounded": "yes", "judge_retrieval": "yes",
       "judge_hedged": "no", "judge_question_type": "recall",
       "judge_note": "looks fine"}
SOURCES = [{"n": 1, "ref": "message:1", "text": "me: great first race man",
            "occurred_at": "2021-09-12T00:00:00Z", "source": "messages",
            "path": None}]


class TestKeys(unittest.TestCase):
    """One key each. Anything else is not an action, which is what stops a
    stray Enter from labelling a row."""

    def test_the_four_keys(self):
        self.assertEqual([review.parse_action(k) for k in "gbsqe"],
                         ["good", "bad", "skip", "quit", "expand"])

    def test_case_and_whitespace_do_not_matter(self):
        self.assertEqual(review.parse_action(" G\n"), "good")

    def test_anything_else_is_not_an_action(self):
        for key in ("", "\n", "y", "gg", None):
            with self.subTest(key=key):
                self.assertIsNone(review.parse_action(key))


class TestScreen(unittest.TestCase):
    def test_the_review_screen_carries_no_judge_opinion(self):
        """Showing it first anchors the human to it, the two then agree
        more often than they should, and the measurement quietly becomes
        worthless."""
        screen = review.format_row(ROW, SOURCES)
        self.assertIn("who raced", screen)
        self.assertIn("You did [1].", screen)
        self.assertIn("[1] message:1", screen)
        self.assertNotIn("judge", screen.lower())
        self.assertNotIn("looks fine", screen)

    def test_the_judge_prints_only_afterwards(self):
        self.assertIn("grounded=yes", review.format_judge(ROW))
        self.assertIn("looks fine", review.format_judge(ROW))

    def test_an_unjudged_row_says_so(self):
        self.assertIn("not judged", review.format_judge(dict(ROW, judge_grounded=None)))

    def test_sources_are_relabelled_as_the_answerer_saw_them(self):
        text = review.format_sources(SOURCES, cited=[1], label="Ada")
        self.assertIn("Ada: great first race man", text)


class TestSaveVerdict(unittest.TestCase):
    def test_it_writes_only_verdict_and_note(self):
        """The judge cannot write these, and this cannot write judge_*.
        Neither side may overwrite the other, or comparing them is
        circular."""
        conn = Conn()
        review.save_verdict(conn, 7, "good", "fine")
        sql, params = conn.log[0]
        self.assertIn("verdict = %s", sql)
        self.assertIn("note = %s", sql)
        self.assertNotIn("judge", sql)
        self.assertEqual(params, ["good", "fine", 7])
        self.assertEqual(conn.commits, 1)

    def test_an_empty_note_is_null_not_empty_string(self):
        """NULL means unanswered. An empty string means answered with
        nothing, and a later count cannot tell them apart."""
        conn = Conn()
        review.save_verdict(conn, 7, "bad", "   ")
        self.assertIsNone(conn.log[0][1][1])

    def test_an_unknown_verdict_raises_rather_than_writing(self):
        """A typo becoming a third category splits the very counts this
        exists to produce."""
        with self.assertRaises(ValueError):
            review.save_verdict(Conn(), 7, "meh")


class TestQueue(unittest.TestCase):
    def test_unlabelled_is_newest_first_and_skips_the_answerless(self):
        """Recall decays. An old row invites a guess, and a guessed label
        is worse than a missing one."""
        with mock.patch.object(db, "fetch", return_value=[]) as fetch:
            review.unlabelled(Conn(), 5)
        sql = fetch.call_args.args[1]
        self.assertIn("answer IS NOT NULL", sql)
        self.assertIn("verdict IS NULL", sql)
        self.assertIn("ORDER BY id DESC", sql)


class TestLoop(unittest.TestCase):
    def loop(self, keys, notes=("",)):
        conn = Conn()
        answers = iter(list(keys) + list(notes) * 10)
        out = []
        with mock.patch.object(review, "unlabelled", return_value=[ROW, ROW]), \
             mock.patch.object(review.judge, "sources_for", return_value=SOURCES):
            n = review.run(conn, limit=5, ask=lambda _: next(answers),
                           log=out.append)
        return n, conn, "\n".join(out)

    def test_a_label_is_saved_and_the_judge_shows_after_the_keypress(self):
        n, conn, text = self.loop(["g", "", "q"])
        self.assertEqual(n, 1)
        self.assertEqual(len(conn.log), 1)
        self.assertLess(text.index("who raced"), text.index("grounded=yes"))

    def test_skip_writes_nothing(self):
        n, conn, _ = self.loop(["s", "q"])
        self.assertEqual((n, conn.log), (0, []))

    def test_a_stray_key_is_asked_again(self):
        n, conn, text = self.loop(["", "x", "b", "", "q"])
        self.assertEqual(n, 1)
        self.assertIn("press g, b, s, q, or e", text)


class TestReference(unittest.TestCase):
    """The eval file's expect: line is the author's reference. Unlike the
    judge it shows BEFORE the keypress: it is what the human grades
    against, and without it an aggregate question is graded from memory."""

    def test_the_reference_shows_before_the_answer(self):
        screen = review.format_row(dict(ROW, expected="the same person both years"), SOURCES)
        self.assertIn("REFERENCE", screen)
        self.assertLess(screen.index("the same person both years"), screen.index("ANSWER"))

    def test_a_row_without_a_reference_has_no_reference_line(self):
        self.assertNotIn("REFERENCE", review.format_row(ROW, SOURCES))

    def test_the_queue_carries_the_reference(self):
        with mock.patch.object(db, "fetch", return_value=[]) as fetch:
            review.unlabelled(Conn(), 5)
        self.assertIn("expected", fetch.call_args.args[1])


TWO = SOURCES + [{"n": 2, "ref": "message:2", "text": "them: thanks!",
                  "occurred_at": "2021-09-12T00:00:00Z", "source": "messages",
                  "path": None}]


class TestTheAsk(unittest.TestCase):
    """The screen says what you are deciding. Ten thousand characters of
    answer and sources with "g=good b=bad" under them is a guess with a
    name, because good is never defined."""

    def test_with_a_reference_the_ask_is_the_reference(self):
        screen = review.format_row(dict(ROW, expected="x"), SOURCES)
        self.assertIn("ASK", screen)
        self.assertIn("state what the reference states", screen)

    def test_without_one_the_ask_is_your_own_knowledge(self):
        self.assertIn("as far as you know", review.format_row(ROW, SOURCES))

    def test_the_ask_is_the_last_thing_before_the_keypress(self):
        screen = review.format_row(dict(ROW, expected="x"), SOURCES)
        self.assertGreater(screen.index("ASK"), screen.index("[1] message:1"))


class TestSourceIndex(unittest.TestCase):
    """Sources are one line each by default. Most decisions compare the
    answer with the reference and never need the text."""

    def test_the_screen_lists_sources_without_their_text(self):
        screen = review.format_row(ROW, SOURCES)
        self.assertIn("[1] message:1", screen)
        self.assertNotIn("great first race man", screen)

    def test_a_cited_source_is_marked(self):
        screen = review.format_row(dict(ROW, cited=[1]), TWO)
        marked = [l for l in screen.splitlines() if "[1] message:1" in l][0]
        plain = [l for l in screen.splitlines() if "[2] message:2" in l][0]
        self.assertIn("*", marked)
        self.assertNotIn("*", plain)

    def test_expand_prints_the_cited_sources_in_full(self):
        text = review.format_sources(TWO, cited=[1])
        self.assertIn("great first race man", text)
        self.assertNotIn("thanks!", text)

    def test_expand_with_nothing_cited_prints_them_all(self):
        text = review.format_sources(TWO, cited=[])
        self.assertIn("great first race man", text)
        self.assertIn("thanks!", text)

    def test_the_queue_carries_the_citations(self):
        with mock.patch.object(db, "fetch", return_value=[]) as fetch:
            review.unlabelled(Conn(), 5)
        self.assertIn("cited", fetch.call_args.args[1])


class TestExpandKey(unittest.TestCase):
    loop = TestLoop.loop

    def test_e_expands_and_asks_again(self):
        n, conn, text = self.loop(["e", "g", "", "q"])
        self.assertEqual(n, 1)
        self.assertGreater(text.index("great first race man"), text.index("ASK"))

    def test_the_header_defines_the_keys_once(self):
        _, _, text = self.loop(["q"])
        self.assertIn("g=yes", text.splitlines()[0])
        self.assertIn("e=expand", text.splitlines()[0])
