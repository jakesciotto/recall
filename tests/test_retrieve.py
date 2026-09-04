import datetime as dt
import unittest
from unittest import mock

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

    def test_an_iso_date_bounds_that_one_day(self):
        """"Whom did I text on 2021-02-03" filtered to the whole year and
        offered 14,298 chunks where the day held 88."""
        f = retrieve.parse_dates("whom did I text on 2021-02-03?", TODAY)
        self.assertEqual((f.since, f.until, f.phrase),
                         ("2021-02-03", "2021-02-04", "2021-02-03"))

    def test_an_impossible_iso_date_does_not_fire(self):
        f = retrieve.parse_dates("on 2021-13-45", TODAY)
        self.assertEqual(f, retrieve.parse_dates("in 2021", TODAY))

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
    def test_the_question_is_parsed_with_plainto_tsquery(self):
        """to_tsquery rejects ordinary punctuation; plainto_tsquery does not.
        The parse now happens in term selection, not in the search SQL."""
        conn = DFConn(["dentist"], 1000, {"'dentist'": 3})
        retrieve.sparse_terms(conn, "when did I last see the dentist?")
        self.assertTrue(any("plainto_tsquery" in s for s in conn.log))

    def test_the_sparse_arm_takes_a_prebuilt_query(self):
        sql = retrieve.sparse_sql("", 10)
        self.assertIn("%s::tsquery", sql)
        self.assertNotIn("plainto_tsquery", sql)

    def test_the_dense_arm_orders_by_distance(self):
        self.assertIn("<=>", retrieve.dense_sql("", 10))

    def test_the_query_carries_the_instruction_prefix(self):
        """Asymmetric embedding models want the instruction on the query, not
        baked into every stored document."""
        self.assertTrue(retrieve.QUERY_PREFIX.startswith("Instruct:"))



class Cursor:
    """Records every statement and answers the shapes recall asks for."""

    def __init__(self, log, echo=None, rows=None):
        self.log = log
        self.echo = echo
        self.rows = rows or {}
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
        elif "<=>" in sql:
            self.result = [list(self.rows.get("dense", []))]
        elif "ts_rank" in sql:
            self.result = [list(self.rows.get("sparse", []))]
        else:
            self.result = [[]]

    def fetchone(self):
        return self.result


class Conn:
    def __init__(self, echo=None, dense=(), sparse=()):
        self.log = []
        self.echo = echo
        self.rows = {"dense": dense, "sparse": sparse}

    def cursor(self):
        return Cursor(self.log, self.echo, self.rows)

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


def row(ref, **extra):
    base = {"ref": ref, "text": f"text of {ref}", "occurred_at": None,
            "source": "messages", "path": None}
    base.update(extra)
    return base


class TestSearchTraced(unittest.TestCase):
    """The trace keeps every fused candidate, not only the k that survive.
    Which chunks retrieval offered and did NOT use is half of the
    offered-against-cited signal the query decision log measures."""

    def conn(self):
        return Conn(dense=[row("a", distance=0.1), row("b", distance=0.2),
                           row("c", distance=0.3)],
                    sparse=[row("b", rank=0.9), row("d", rank=0.5)])

    def traced(self, **kw):
        conn = self.conn()
        with mock.patch.object(retrieve, "sparse_terms",
                               return_value=["'happen'"]):
            hits, dates, trace = retrieve.search_traced(
                "what happened", conn, lambda t: [0.0] * 4, k=2, today=TODAY,
                **kw)
        return hits, dates, trace, conn

    def test_it_returns_hits_dates_and_a_trace(self):
        hits, dates, trace, _ = self.traced()
        self.assertEqual(len(hits), 2)
        self.assertEqual(dates, retrieve.NO_DATES)
        self.assertIsInstance(trace, dict)

    def test_the_trace_keeps_every_fused_candidate(self):
        hits, _, trace, _ = self.traced()
        self.assertEqual([r for r, _ in trace["fused"]], ["b", "a", "d", "c"])
        self.assertEqual(len(hits), 2)

    def test_each_arm_is_recorded_in_rank_order_with_its_score(self):
        _, _, trace, _ = self.traced()
        self.assertEqual(trace["dense"], [("a", 0.1), ("b", 0.2), ("c", 0.3)])
        self.assertEqual(trace["sparse"], [("b", 0.9), ("d", 0.5)])
        self.assertEqual((trace["dense_n"], trace["sparse_n"],
                          trace["fused_n"]), (3, 2, 4))

    def test_timings_are_integers(self):
        _, _, trace, _ = self.traced()
        for key in ("embed_ms", "dense_ms", "sparse_ms"):
            self.assertIsInstance(trace[key], int)

    def test_hits_carry_no_arm_scores(self):
        """The API returns hits as JSON. The arm scores are for the log."""
        hits, _, _, _ = self.traced()
        for h in hits:
            self.assertNotIn("distance", h)
            self.assertNotIn("rank", h)
            self.assertIn("score", h)

    def test_search_is_the_same_path_with_the_trace_dropped(self):
        conn = self.conn()
        with mock.patch.object(retrieve, "sparse_terms",
                               return_value=["'happen'"]):
            hits, dates = retrieve.search("what happened", conn,
                                          lambda t: [0.0] * 4, k=2, today=TODAY)
        traced, _, _, _ = self.traced()
        self.assertEqual([h["ref"] for h in hits], [h["ref"] for h in traced])

    def test_the_vector_binds_before_and_after_the_where_clause(self):
        """The distance is selected AND ordered by, so the vector binds
        twice, around the filter parameters. Get the order wrong and the
        date lands where the vector should be."""
        conn = Conn(dense=[], sparse=[])
        with mock.patch.object(retrieve, "sparse_terms",
                               return_value=["'happen'"]):
            retrieve.search_traced("what happened in 2018", conn,
                                   lambda t: [0.5] * 2, today=TODAY)
        sql, params = next((s, p) for s, p in conn.log if "<=>" in s)
        vec = retrieve.vector_literal([0.5] * 2)
        self.assertEqual(params[0], vec)
        self.assertEqual(params[-1], vec)
        self.assertEqual(params[1:-1], ["2018-01-01", "2019-01-01"])



class DFConn:
    """Answers the three statements term selection makes: the lexeme parse,
    the corpus size, and one document-frequency count per lexeme."""

    def __init__(self, lexemes, total, df):
        self.lexemes, self.total, self.df = lexemes, total, df
        self.log = []

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(s): return s
            def __exit__(s, *a): return False

            def execute(s, sql, params=None):
                conn.log.append(sql)
                if "plainto_tsquery" in sql:
                    s.result = [" & ".join(f"'{l}'" for l in conn.lexemes)]
                elif "reltuples" in sql:
                    s.result = [conn.total]
                else:
                    s.result = [conn.df[params[0]]]

            def fetchone(s): return s.result
        return Cur()


class TestSparseTerms(unittest.TestCase):
    """plainto_tsquery ANDs every lexeme and matched nothing on 26 of the
    first 30 real questions. OR-ing them all let common words outrank the
    one that mattered, and no Postgres ranking has document frequency. So
    the arm searches by the distinctive words: lexemes rare in the corpus."""

    def test_common_lexemes_are_dropped(self):
        conn = DFConn(["first", "told", "chatgpt", "channel"], 100000,
                      {"'first'": 40000, "'told'": 30000, "'chatgpt'": 64,
                       "'channel'": 9000})
        self.assertEqual(retrieve.sparse_terms(conn, "q"), ["'chatgpt'"])

    def test_when_nothing_is_rare_the_rarest_two_survive(self):
        """A question of ordinary words still gets an arm."""
        conn = DFConn(["text", "friend", "dinner"], 100000,
                      {"'text'": 50000, "'friend'": 20000, "'dinner'": 8000})
        self.assertEqual(retrieve.sparse_terms(conn, "q"),
                         ["'dinner'", "'friend'"])

    def test_a_lexeme_absent_from_the_corpus_is_dropped(self):
        conn = DFConn(["quantum", "tweet"], 100000,
                      {"'quantum'": 0, "'tweet'": 300})
        self.assertEqual(retrieve.sparse_terms(conn, "q"), ["'tweet'"])

    def test_at_most_four_terms(self):
        conn = DFConn([f"w{i}" for i in range(6)], 100000,
                      {f"'w{i}'": 10 + i for i in range(6)})
        self.assertEqual(len(retrieve.sparse_terms(conn, "q")), 4)

    def test_no_lexemes_means_no_terms(self):
        conn = DFConn([], 100000, {})
        self.assertEqual(retrieve.sparse_terms(conn, "the and of"), [])

    def test_the_query_ors_the_terms(self):
        self.assertEqual(retrieve.tsquery(["'a'", "'b'"]), "'a' | 'b'")
