import contextlib
import unittest
from unittest import mock

from recall import querylog, retrieve, serve

HITS = [{"ref": "message:1", "text": "me: hi", "occurred_at": None,
         "source": "messages", "path": None, "score": 0.02}]
TRACE = {"dense": [("message:1", 0.1)], "sparse": [], "fused": [("message:1", 0.02)],
         "dense_n": 1, "sparse_n": 0, "fused_n": 1,
         "embed_ms": 1, "dense_ms": 1, "sparse_ms": 1}


@contextlib.contextmanager
def fake_conn():
    yield object()


def stream(*pieces):
    def chat_stream(prompt, model=None, timeout=600):
        yield from pieces
    return chat_stream


class Logged:
    """Captures what the answer path hands to the log."""

    def __init__(self, raise_=False):
        self.calls = []
        self.raise_ = raise_

    def __call__(self, **kw):
        if self.raise_:
            raise RuntimeError("log exploded")
        self.calls.append(kw)
        return 1


class TestHandleAsk(unittest.TestCase):
    def ask(self, body, logged=None, chat_url="http://x/v1"):
        logged = logged or Logged()
        with mock.patch.object(serve.db, "connect", fake_conn), \
             mock.patch.object(serve.retrieve, "search_traced",
                               return_value=(HITS, retrieve.NO_DATES, TRACE)), \
             mock.patch.object(serve.answer, "chat", return_value="it [1]"), \
             mock.patch.object(serve.config, "CHAT_URL", chat_url), \
             mock.patch.object(serve.querylog, "log", logged):
            out = serve.handle_ask(body)
        return out, logged

    def test_it_answers_and_logs_one_row(self):
        out, logged = self.ask({"question": "what"})
        self.assertEqual(out["answer"], "it [1]")
        self.assertEqual(len(logged.calls), 1)
        row = logged.calls[0]
        self.assertEqual((row["client"], row["question"], row["k"]),
                         ("api", "what", 8))
        self.assertEqual(row["answer"], "it [1]")
        self.assertEqual(row["pool"], retrieve.POOL)
        self.assertIs(row["trace"], TRACE)
        self.assertFalse(row["streamed"])
        self.assertIsInstance(row["total_ms"], int)

    def test_a_sources_only_request_is_still_logged(self):
        """Every retrieval decision happened. The row is written whether or
        not a model ran."""
        out, logged = self.ask({"question": "what", "sources_only": True})
        self.assertIsNone(out["answer"])
        self.assertEqual(len(logged.calls), 1)
        self.assertIsNone(logged.calls[0]["answer"])

    def test_a_logger_that_raises_never_costs_the_answer(self):
        out, _ = self.ask({"question": "what"}, logged=Logged(raise_=True))
        self.assertEqual(out["answer"], "it [1]")

    def test_the_prompt_length_is_recorded(self):
        _, logged = self.ask({"question": "what"})
        self.assertGreater(logged.calls[0]["prompt_chars"], 0)


class TestStreamAsk(unittest.TestCase):
    def test_it_logs_after_the_stream_with_first_token_timing(self):
        logged = Logged()
        with mock.patch.object(serve.db, "connect", fake_conn), \
             mock.patch.object(serve.retrieve, "search_traced",
                               return_value=(HITS, retrieve.NO_DATES, TRACE)), \
             mock.patch.object(serve.answer, "chat_stream", stream("it ", "[1]")), \
             mock.patch.object(serve.config, "CHAT_URL", "http://x/v1"), \
             mock.patch.object(serve.querylog, "log", logged):
            frames = list(serve.stream_ask({"question": "what"}))
        self.assertTrue(frames[-1].startswith("event: done"))
        self.assertEqual(len(logged.calls), 1)
        row = logged.calls[0]
        self.assertTrue(row["streamed"])
        self.assertEqual(row["answer"], "it [1]")
        self.assertIsInstance(row["first_token_ms"], int)


class TestLogOpensItsOwnConnection(unittest.TestCase):
    def test_off_never_touches_the_database(self):
        with mock.patch.dict("os.environ", {"RECALL_QUERY_LOG": "0"}), \
             mock.patch.object(querylog, "log_query") as writer:
            self.assertIsNone(querylog.log(client="cli", question="q", k=1,
                                           pool=1, source=None,
                                           dates=retrieve.NO_DATES, trace=TRACE,
                                           hits=HITS, answer=None, meta={},
                                           model_requested=""))
        writer.assert_not_called()

    def test_an_unreachable_database_never_raises(self):
        """conftest blocks db.connect, which is exactly the failure a
        production log write can meet."""
        self.assertIsNone(querylog.log(client="cli", question="q", k=1,
                                       pool=1, source=None,
                                       dates=retrieve.NO_DATES, trace=TRACE,
                                       hits=HITS, answer=None, meta={},
                                       model_requested=""))
