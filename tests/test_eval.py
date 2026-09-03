import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from recall import evalrun, retrieve

FILE = """# My questions

## Facts
1. What is the IP of the box?
2. Which port does the app use?

Some prose that is not a question.

## Negative
3. What did I never write about?
- a bullet, not a question
"""


class TestQuestionFile(unittest.TestCase):
    """Numbered lines are questions. Headings, prose and bullets are not,
    so the file can carry notes about what each group tests."""

    def test_numbered_lines_only(self):
        self.assertEqual(evalrun.questions(FILE), [
            "What is the IP of the box?",
            "Which port does the app use?",
            "What did I never write about?",
        ])

    def test_order_is_the_file_order(self):
        text = "2. second\n1. first\n"
        self.assertEqual(evalrun.questions(text), ["second", "first"])


HITS = [{"ref": "doc:1", "text": "t", "occurred_at": None, "source": "documents",
         "path": "notes/a.md", "score": 0.02}]
TRACE = {"dense": [("doc:1", 0.1)], "sparse": [], "fused": [("doc:1", 0.02)],
         "dense_n": 1, "sparse_n": 0, "fused_n": 1,
         "embed_ms": 1, "dense_ms": 1, "sparse_ms": 1}


@contextlib.contextmanager
def fake_conn():
    yield object()


class TestRun(unittest.TestCase):
    def run_eval(self, text, chat_url="http://x/v1", answer_text="it [1]"):
        logged = []
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "q.md"
            path.write_text(text)
            with mock.patch.object(evalrun.db, "connect", fake_conn), \
                 mock.patch.object(evalrun.retrieve, "search_traced",
                                   return_value=(HITS, retrieve.NO_DATES, TRACE)), \
                 mock.patch.object(evalrun.answer, "chat",
                                   return_value=answer_text), \
                 mock.patch.object(evalrun.config, "CHAT_URL", chat_url), \
                 mock.patch.object(evalrun.querylog, "log",
                                   lambda **kw: logged.append(kw) or 1), \
                 contextlib.redirect_stdout(out):
                n = evalrun.run(path, embedder=lambda t: [0.0] * 4)
        return n, logged, out.getvalue()

    def test_every_question_is_asked_and_logged_as_eval(self):
        n, logged, _ = self.run_eval(FILE)
        self.assertEqual(n, 3)
        self.assertEqual([r["client"] for r in logged], ["eval"] * 3)
        self.assertEqual(logged[0]["question"], "What is the IP of the box?")
        self.assertEqual(logged[0]["answer"], "it [1]")

    def test_it_prints_one_line_per_question_with_the_citation_count(self):
        _, _, text = self.run_eval(FILE)
        lines = [l for l in text.splitlines() if l.strip().startswith(("1", "2", "3"))]
        self.assertEqual(len(lines), 3)
        self.assertIn("cited 1", lines[0])

    def test_without_a_chat_endpoint_it_still_logs_the_retrieval(self):
        n, logged, text = self.run_eval(FILE, chat_url="")
        self.assertEqual(n, 3)
        self.assertIsNone(logged[0]["answer"])
        self.assertIn("no generation endpoint", text)

    def test_a_failed_answer_does_not_end_the_batch(self):
        def boom(prompt, meta=None):
            raise OSError("model down")
        logged = []
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "q.md"
            path.write_text(FILE)
            with mock.patch.object(evalrun.db, "connect", fake_conn), \
                 mock.patch.object(evalrun.retrieve, "search_traced",
                                   return_value=(HITS, retrieve.NO_DATES, TRACE)), \
                 mock.patch.object(evalrun.answer, "chat", boom), \
                 mock.patch.object(evalrun.config, "CHAT_URL", "http://x/v1"), \
                 mock.patch.object(evalrun.querylog, "log",
                                   lambda **kw: logged.append(kw) or 1), \
                 contextlib.redirect_stdout(io.StringIO()):
                n = evalrun.run(path, embedder=lambda t: [0.0] * 4)
        self.assertEqual(n, 3)
        self.assertEqual(logged[0]["error"], "OSError: model down")
        self.assertIsNone(logged[0]["answer"])
