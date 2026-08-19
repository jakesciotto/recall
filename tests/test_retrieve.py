import datetime as dt
import unittest

from recall import retrieve

TODAY = dt.date(2026, 8, 17)


class TestRRF(unittest.TestCase):
    """Fusion works on RANK. Cosine distance and ts_rank are not comparable
    numbers, and normalising them invents a relationship."""

    def test_a_ref_in_both_lists_beats_a_ref_in_one(self):
        out = [r for r, _ in retrieve.rrf([["a", "b", "c"], ["c", "d"]])]
        self.assertEqual(out[0], "c")

    def test_it_returns_every_ref(self):
        out = {r for r, _ in retrieve.rrf([["a"], ["b"]])}
        self.assertEqual(out, {"a", "b"})

    def test_ties_are_deterministic(self):
        self.assertEqual([r for r, _ in retrieve.rrf([["a"], ["b"]])], ["a", "b"])

    def test_empty_input_fuses_to_nothing(self):
        self.assertEqual(retrieve.rrf([[], []]), [])


class TestParseDates(unittest.TestCase):
    """A wrong date filter does not degrade an answer, it removes it, and the
    user sees a confident "nothing found" rather than a mistake."""

    def test_a_bare_year_bounds_that_year(self):
        f = retrieve.parse_dates("the trip in 2018", TODAY)
        self.assertEqual((f.since, f.until), ("2018-01-01", "2019-01-01"))

    def test_a_month_and_year_bounds_that_month(self):
        f = retrieve.parse_dates("invoice from March 2023", TODAY)
        self.assertEqual((f.since, f.until), ("2023-03-01", "2023-04-01"))

    def test_last_year_resolves_against_the_supplied_today(self):
        f = retrieve.parse_dates("what did I buy last year", TODAY)
        self.assertEqual((f.since, f.until), ("2025-01-01", "2026-01-01"))

    def test_last_spring_uses_the_most_recent_finished_spring(self):
        f = retrieve.parse_dates("planted last spring", TODAY)
        self.assertEqual((f.since, f.until), ("2026-03-01", "2026-06-01"))

    def test_a_bare_month_name_does_not_fire(self):
        """"march" is also an ordinary verb. Guessing a year hides answers."""
        self.assertIsNone(retrieve.parse_dates("the march to the stadium", TODAY).since)

    def test_a_year_inside_a_phone_number_does_not_fire(self):
        self.assertIsNone(retrieve.parse_dates("texts with 12026467057", TODAY).since)

    def test_no_date_words_means_no_filter(self):
        self.assertEqual(retrieve.parse_dates("summarize the program", TODAY),
                         retrieve.NO_DATES)


class TestWhereClause(unittest.TestCase):
    def test_no_filters_produce_no_where(self):
        self.assertEqual(retrieve.where_clause(None, None), ("", []))

    def test_the_question_is_never_interpolated(self):
        """User text goes in as a bound parameter, never concatenated."""
        sql, params = retrieve.where_clause(None, "o'brien")
        self.assertIn("%s", sql)
        self.assertNotIn("o'brien", sql)
        self.assertEqual(params, ["o'brien"])

    def test_the_upper_bound_is_exclusive(self):
        f = retrieve.parse_dates("in 2018", TODAY)
        sql, _ = retrieve.where_clause(f, None)
        self.assertIn("<", sql)
        self.assertNotIn("<=", sql)

    def test_both_filters_bind_in_order(self):
        f = retrieve.parse_dates("in 2018", TODAY)
        sql, params = retrieve.where_clause(f, "email")
        self.assertEqual(params, ["2018-01-01", "2019-01-01", "email"])


class TestSQLShape(unittest.TestCase):
    def test_the_sparse_arm_uses_plainto_tsquery(self):
        """to_tsquery rejects ordinary punctuation; plainto_tsquery does not."""
        self.assertIn("plainto_tsquery", retrieve.sparse_sql("", 10))

    def test_the_dense_arm_orders_by_distance(self):
        self.assertIn("<=>", retrieve.dense_sql("", 10))

    def test_the_query_carries_the_instruction_prefix(self):
        """Asymmetric embedding models want the instruction on the query, not
        baked into every stored document."""
        self.assertTrue(retrieve.QUERY_PREFIX.startswith("Instruct:"))
