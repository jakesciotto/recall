import unittest
from unittest import mock

from recall import config, db, querylog, retrieve


class Cursor:
    def __init__(self, log, fail_on=None, next_id=41):
        self.log = log
        self.fail_on = fail_on
        self.next_id = next_id
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"refused: {self.fail_on}")
        self.log.append((sql, params))
        self.result = [self.next_id] if "RETURNING" in sql else None

    def fetchone(self):
        return self.result


class Conn:
    def __init__(self, fail_on=None):
        self.log = []
        self.fail_on = fail_on
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self.log, self.fail_on)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def statements(self):
        return [s for s, _ in self.log]


def hit(ref):
    return {"ref": ref, "text": "t", "occurred_at": None, "source": "messages",
            "path": None, "score": 0.01}


TRACE = {
    "dense": [("a", 0.1), ("b", 0.2), ("c", 0.3)],
    "sparse": [("b", 0.9), ("d", 0.5)],
    "fused": [("b", 0.03), ("a", 0.02), ("d", 0.016), ("c", 0.015)],
    "dense_n": 3, "sparse_n": 2, "fused_n": 4,
    "embed_ms": 12, "dense_ms": 3, "sparse_ms": 4,
}
HITS = [hit("b"), hit("a")]


def entry(**kw):
    base = dict(client="api", question="q", k=2, pool=50, source=None,
                dates=retrieve.NO_DATES, trace=TRACE, hits=HITS,
                answer="It was [1] and [2].", meta={}, model_requested="m")
    base.update(kw)
    return base


class TestEnabled(unittest.TestCase):
    """An unset variable keeps the log on. A log that quietly defaults to
    off would look healthy while recording nothing."""

    def test_unset_means_on(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(querylog.enabled())

    def test_zero_means_off(self):
        with mock.patch.dict("os.environ", {"RECALL_QUERY_LOG": "0"}):
            self.assertFalse(querylog.enabled())

    def test_anything_else_means_on(self):
        with mock.patch.dict("os.environ", {"RECALL_QUERY_LOG": "off"}):
            self.assertTrue(querylog.enabled())


class TestCitedNumbers(unittest.TestCase):
    def test_single_and_grouped_citations_both_count(self):
        """"[4, 5]" is one citation of two sources. A single-number pattern
        misses half the citations and under-reports use, which looks fine."""
        valid, invalid = querylog.cited_numbers("see [1] and [2, 3]", 3)
        self.assertEqual((valid, invalid), ([1, 2, 3], []))

    def test_a_number_past_the_prompt_is_invalid_not_dropped(self):
        """A model that cites [12] against 8 sources is telling you
        something worth keeping."""
        valid, invalid = querylog.cited_numbers("[2] then [12]", 8)
        self.assertEqual((valid, invalid), ([2], [12]))

    def test_fullwidth_brackets_count(self):
        valid, invalid = querylog.cited_numbers("see【1】and【2, 9】", 3)
        self.assertEqual((valid, invalid), ([1, 2], [9]))

    def test_no_answer_cites_nothing(self):
        self.assertEqual(querylog.cited_numbers(None, 3), ([], []))


class TestCandidates(unittest.TestCase):
    """Every fused candidate is kept, not only the k that reached the
    prompt. What retrieval offered and did NOT use is half of the signal."""

    def test_every_fused_ref_becomes_a_row(self):
        rows = querylog.candidates_from(TRACE, HITS, [1])
        self.assertEqual([r["ref"] for r in rows], ["b", "a", "d", "c"])

    def test_arm_ranks_and_scores_travel(self):
        rows = {r["ref"]: r for r in querylog.candidates_from(TRACE, HITS, [])}
        self.assertEqual((rows["b"]["dense_rank"], rows["b"]["sparse_rank"]),
                         (2, 1))
        self.assertEqual((rows["b"]["distance"], rows["b"]["ts_rank"]),
                         (0.2, 0.9))
        self.assertIsNone(rows["d"]["dense_rank"])
        self.assertIsNone(rows["a"]["sparse_rank"])

    def test_final_rank_is_null_past_k(self):
        rows = {r["ref"]: r for r in querylog.candidates_from(TRACE, HITS, [])}
        self.assertEqual(rows["b"]["final_rank"], 1)
        self.assertEqual(rows["a"]["final_rank"], 2)
        self.assertIsNone(rows["d"]["final_rank"])

    def test_a_citation_indexes_the_prompt_not_the_ref(self):
        """"[2]" means the second source given to the model."""
        rows = {r["ref"]: r for r in querylog.candidates_from(TRACE, HITS, [2])}
        self.assertTrue(rows["a"]["cited"])
        self.assertFalse(rows["b"]["cited"])

    def test_a_citation_past_the_prompt_marks_nothing(self):
        rows = querylog.candidates_from(TRACE, HITS, [9])
        self.assertFalse(any(r["cited"] for r in rows))


class TestRecord(unittest.TestCase):
    def test_it_commits_the_parent_before_it_writes_the_candidates(self):
        """Two commits on purpose. A candidate failure must not roll the
        parent back with it."""
        conn = Conn()
        qid = querylog.log_query(conn, **entry())
        self.assertEqual(qid, 41)
        stmts = conn.statements()
        self.assertIn("query_log", stmts[0])
        self.assertIn("query_candidate", stmts[1])
        self.assertEqual(conn.commits, 2)

    def test_a_failure_rolls_back_so_the_connection_stays_usable(self):
        """psycopg leaves a connection in an aborted transaction after an
        error. Every later statement fails until something rolls back."""
        conn = Conn(fail_on="query_candidate")
        querylog.log_query(conn, **entry())
        self.assertEqual(conn.rollbacks, 1)

    def test_the_parent_binds_every_column_as_a_parameter(self):
        conn = Conn()
        querylog.log_query(conn, **entry(question="o'brien"))
        sql, params = conn.log[0]
        self.assertNotIn("o'brien", sql)
        self.assertIn("o'brien", params)

    def test_cited_numbers_land_in_the_row(self):
        conn = Conn()
        querylog.log_query(conn, **entry(answer="[1] and [2] and [7]"))
        _, params = conn.log[0]
        self.assertIn([1, 2], params)
        self.assertIn([7], params)

    def test_a_candidate_failure_keeps_the_parent(self):
        """The question is recorded. Losing its candidates is bad, losing the
        row entirely is worse."""
        conn = Conn(fail_on="query_candidate")
        self.assertEqual(querylog.log_query(conn, **entry()), 41)

    def test_a_parent_failure_returns_none_and_never_raises(self):
        conn = Conn(fail_on="query_log")
        self.assertIsNone(querylog.log_query(conn, **entry()))

    def test_a_broken_trace_never_raises(self):
        """Safety covers assembly as well as the write."""
        conn = Conn()
        self.assertIsNone(querylog.log_query(conn, **entry(trace="nonsense")))

    def test_off_means_no_write(self):
        conn = Conn()
        with mock.patch.dict("os.environ", {"RECALL_QUERY_LOG": "0"}):
            self.assertIsNone(querylog.log_query(conn, **entry()))
        self.assertEqual(conn.log, [])


class TestSchema(unittest.TestCase):
    def test_the_log_tables_are_part_of_the_schema(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS query_log", db.LOG_SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS query_candidate",
                      db.LOG_SCHEMA)

    def test_apply_schema_applies_them(self):
        conn = Conn()
        db.apply_schema(conn, dims=4)
        self.assertTrue(any("query_candidate" in s for s in conn.statements()))

    def test_the_candidate_rows_cascade_with_their_parent(self):
        self.assertIn("ON DELETE CASCADE", db.LOG_SCHEMA)


class TestModelName(unittest.TestCase):
    def test_a_gguf_path_becomes_its_bare_name(self):
        """llama.cpp reports the full GGUF path, which embeds a home
        directory and buries the name."""
        self.assertEqual(querylog.model_name("/models/x/gemma-4-26b-Q8.gguf"),
                         "gemma-4-26b-Q8")

    def test_a_plain_name_passes_through(self):
        self.assertEqual(querylog.model_name("llama3.1"), "llama3.1")
