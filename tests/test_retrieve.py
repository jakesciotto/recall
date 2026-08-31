import datetime as dt
import unittest

from recall import db, retrieve

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



class Cursor:
    """Records every statement and answers the two shapes recall asks for."""

    def __init__(self, log, echo=None):
        self.log = log
        self.echo = echo
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if "set_config" in sql:
            # Postgres returns the value it applied, which is what makes the
            # read-back check possible.
            self.result = [self.echo if self.echo is not None else params[0]]
        else:
            self.result = [[]]

    def fetchone(self):
        return self.result


class Conn:
    def __init__(self, echo=None):
        self.log = []
        self.echo = echo

    def cursor(self):
        return Cursor(self.log, self.echo)

    def statements(self):
        return [sql for sql, _ in self.log]


class TestSearchWidth(unittest.TestCase):
    """pgvector caps the candidate list at hnsw.ef_search, default 40, so a
    pool of 50 fuses on 25 rows and raises nothing. Measured: 25 rows at ef
    40, 50 rows at ef 100, asking for 50 both times."""

    def test_the_default_pool_clears_the_floor(self):
        self.assertGreaterEqual(retrieve.search_width(retrieve.POOL), 100)

    def test_a_small_pool_still_gets_the_floor(self):
        self.assertEqual(retrieve.search_width(1), 100)

    def test_the_width_follows_a_larger_pool(self):
        """A fixed default only moves the threshold. The next caller that
        raises the pool would meet the same bug one level up."""
        self.assertEqual(retrieve.search_width(200), 400)

    def test_the_width_always_exceeds_the_pool(self):
        """The property under test, swept rather than sampled: one fixture
        cannot show that a bound holds."""
        for pool in [1, 8, 40, 49, 50, 51, 100, 500, 5000]:
            with self.subTest(pool=pool):
                self.assertGreater(retrieve.search_width(pool), pool)


class TestSetSearchWidth(unittest.TestCase):
    def test_it_sets_the_value_it_was_given(self):
        conn = Conn()
        self.assertEqual(db.set_search_width(conn, 250), 250)
        self.assertEqual(conn.log[0][1], ("250",))

    def test_it_reads_the_applied_value_back(self):
        """A check that reads only success is the fault this guards against."""
        conn = Conn(echo="40")
        with self.assertRaises(RuntimeError) as caught:
            db.set_search_width(conn, 250)
        self.assertIn("40", str(caught.exception))


class TestSearchWidensBeforeItQueries(unittest.TestCase):
    def run_search(self, **kw):
        conn = Conn()
        retrieve.search("what did I write in 2018", conn,
                        lambda text: [0.0] * 4, today=TODAY, **kw)
        return conn

    def test_it_widens_the_candidate_list(self):
        self.assertTrue(any("set_config" in s
                            for s in self.run_search().statements()))

    def test_it_widens_before_the_dense_query(self):
        """Order is the whole point. Setting it afterwards changes nothing."""
        stmts = self.run_search().statements()
        widen = next(i for i, s in enumerate(stmts) if "set_config" in s)
        dense = next(i for i, s in enumerate(stmts) if "<=>" in s)
        self.assertLess(widen, dense)

    def test_the_width_matches_the_pool_the_caller_asked_for(self):
        conn = self.run_search(pool=300)
        self.assertEqual(conn.log[0][1], (str(retrieve.search_width(300)),))
