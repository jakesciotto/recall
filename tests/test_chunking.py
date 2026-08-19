import unittest

from recall import chunking


class TestCalibrate(unittest.TestCase):
    """The lesson this whole module exists for: a character budget is a guess
    about tokens, and the guess is wrong by a factor of three between prose
    and dense mail. Measure instead of assuming."""

    def test_it_derives_the_budget_from_the_measured_ratio(self):
        # 2 characters per token: every text is twice its token count.
        budget = chunking.calibrate(["x" * 200], ceiling=1000,
                                    counter=lambda t: len(t) // 2)
        self.assertEqual(budget, int(1000 * chunking.SAFETY * 2))

    def test_a_denser_corpus_gets_a_smaller_budget(self):
        prose = chunking.calibrate(["x" * 400], ceiling=8192,
                                   counter=lambda t: len(t) // 4)
        mail = chunking.calibrate(["x" * 400], ceiling=8192,
                                  counter=lambda t: len(t))
        self.assertLess(mail, prose)

    def test_it_takes_the_worst_ratio_not_the_average(self):
        """The budget has to hold for the DENSEST chunk. An average lets the
        dense tail through, and the dense tail is what gets rejected."""
        counts = {"a" * 100: 25, "b" * 100: 100}      # 4.0 and 1.0 c/tok
        budget = chunking.calibrate(list(counts), ceiling=1000,
                                    counter=lambda t: counts[t])
        self.assertEqual(budget, int(1000 * chunking.SAFETY * 1.0))

    def test_no_tokenizer_falls_back_to_a_pessimistic_ratio(self):
        budget = chunking.calibrate(["x" * 100], ceiling=8192,
                                    counter=lambda t: None)
        self.assertEqual(budget,
                         int(8192 * chunking.SAFETY
                             * chunking.PESSIMISTIC_CHARS_PER_TOKEN))

    def test_the_budget_leaves_headroom_below_the_ceiling(self):
        self.assertLess(chunking.SAFETY, 1.0)


class TestSplitToBudget(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(chunking.split_to_budget("short", 100), ["short"])

    def test_it_splits_on_paragraph_boundaries(self):
        out = chunking.split_to_budget("a" * 60 + "\n\n" + "b" * 60, 100)
        self.assertEqual(len(out), 2)

    def test_one_huge_paragraph_still_splits(self):
        """A single record of 188,218 characters arrived in a real export and
        a per-record split never touched it."""
        out = chunking.split_to_budget("x" * 5000, 1000)
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(p) <= 1000 for p in out))

    def test_nothing_is_lost(self):
        out = chunking.split_to_budget("y" * 5000, 700)
        self.assertEqual(sum(p.count("y") for p in out), 5000)


class TestSessions(unittest.TestCase):
    """The gap is a property of the medium. At a live-chat 30 minutes, 39
    percent of one asynchronous export's sessions were a single short
    message, which embeds to noise."""

    def rec(self, thread, at):
        return {"thread": thread, "at": at}

    def test_messages_inside_the_gap_group(self):
        out = list(chunking.sessions(
            [self.rec("a", 0), self.rec("a", 60)], 1800))
        self.assertEqual(len(out), 1)

    def test_a_longer_pause_splits(self):
        out = list(chunking.sessions(
            [self.rec("a", 0), self.rec("a", 99999)], 1800))
        self.assertEqual(len(out), 2)

    def test_a_different_thread_always_splits(self):
        out = list(chunking.sessions(
            [self.rec("a", 0), self.rec("b", 1)], 1800))
        self.assertEqual(len(out), 2)

    def test_the_turn_cap_splits_a_long_run(self):
        out = list(chunking.sessions(
            [self.rec("a", i) for i in range(45)], 1800, max_turns=20))
        self.assertEqual([len(w) for w in out], [20, 20, 5])

    def test_a_wider_gap_produces_fewer_larger_windows(self):
        recs = [self.rec("a", i * 3600) for i in range(10)]
        tight = list(chunking.sessions(recs, 1800))
        wide = list(chunking.sessions(recs, 86400))
        self.assertGreater(len(tight), len(wide))


class TestPack(unittest.TestCase):
    def test_records_group_under_the_budget(self):
        recs = [{"text": "x" * 300} for _ in range(10)]
        for batch in chunking.pack(recs, 1000):
            self.assertLessEqual(sum(len(r["text"]) for r in batch), 1000)

    def test_an_oversized_record_is_split_not_dropped(self):
        out = list(chunking.pack([{"text": "z" * 4000}], 1000))
        self.assertEqual(sum(r["text"].count("z") for b in out for r in b), 4000)


class TestRollup(unittest.TestCase):
    """391,896 plays as one chunk each would nearly double a corpus with
    noise. Rolled to a month it is about 126 chunks."""

    def test_it_groups_by_period(self):
        recs = [{"at": "2018-06-01"}, {"at": "2018-06-30"}, {"at": "2018-07-01"}]
        self.assertEqual([p for p, _ in chunking.rollup(recs)],
                         ["2018-06", "2018-07"])

    def test_periods_come_back_in_order(self):
        recs = [{"at": "2019-01-01"}, {"at": "2018-06-01"}]
        self.assertEqual([p for p, _ in chunking.rollup(recs)],
                         ["2018-06", "2019-01"])
