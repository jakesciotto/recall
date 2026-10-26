import contextlib
import io
import pathlib
import unittest
from unittest import mock

from recall import captions, cli

VERBS = ("doctor", "ingest", "caption", "ask", "eval", "judge", "review",
         "serve", "label", "show", "report", "stats")


class TestEveryVerbDispatches(unittest.TestCase):
    """A subparser that forgets set_defaults(fn=...) parses cleanly and
    then dies on args.fn. Nothing else would catch it."""

    def test_each_verb_reaches_its_function(self):
        for argv in (["doctor"], ["ingest"], ["caption"], ["ask", "q"],
                     ["eval", "f"], ["judge"], ["review"], ["serve"],
                     ["label", "7", "good", "--by", "a", "--note", "n"],
                     ["show", "7"], ["report"], ["stats", "count"]):
            with self.subTest(verb=argv[0]):
                seen = []
                with contextlib.ExitStack() as stack:
                    for verb in VERBS:
                        stack.enter_context(mock.patch.object(
                            cli, f"cmd_{verb}",
                            lambda a, v=verb: seen.append(v) or 0))
                    self.assertEqual(cli.main(argv), 0)
                self.assertEqual(seen, [argv[0]])


class TestCaptionCommand(unittest.TestCase):
    def test_caption_dispatches_with_jobs_and_limit(self):
        seen = {}

        def run(root, work, jobs=3, limit=None, log=print):
            seen.update(root=root, jobs=jobs, limit=limit)
            return {"ok": 1}
        with mock.patch.object(captions, "run", run), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["caption", "--data", "/d", "-j", "2",
                             "--limit", "5"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"root": pathlib.Path("/d"), "jobs": 2,
                                "limit": 5})

    def test_failures_make_the_exit_code_nonzero(self):
        with mock.patch.object(captions, "run",
                               lambda *a, **k: {"ok": 1, "failed": 2}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["caption"]), 1)


class TestCaptionLogFlushes(unittest.TestCase):
    """A detached run redirected to a file showed an empty log for the
    whole hashing pass: print without a flush sits in the block buffer."""

    def test_each_log_line_reaches_the_file_at_once(self):
        captured = {}

        def run(root, work, jobs=3, limit=None, log=print):
            captured["log"] = log
            return {}

        class Out(io.StringIO):
            flushes = 0

            def flush(self):
                Out.flushes += 1
        with mock.patch.object(captions, "run", run), \
             contextlib.redirect_stdout(Out()):
            cli.main(["caption"])
            captured["log"]("one line")
        self.assertGreaterEqual(Out.flushes, 1)


class TestJudgeLogFlushes(unittest.TestCase):
    """Third command, same trap: a detached judge redo over 65 rows showed
    an empty log until it exited."""

    def test_each_log_line_reaches_the_file_at_once(self):
        from recall import db, judge
        captured = {}

        def run(conn, limit, redo=False, dry_run=False, chat=None, model=None,
                log=print):
            captured["log"] = log
            return {}

        @contextlib.contextmanager
        def connect():
            yield object()

        class Out(io.StringIO):
            flushes = 0

            def flush(self):
                Out.flushes += 1
        with mock.patch.object(judge, "run", run), \
             mock.patch.object(db, "connect", connect), \
             mock.patch.object(db, "apply_schema", lambda conn: None), \
             contextlib.redirect_stdout(Out()):
            cli.main(["judge"])
            captured["log"]("one line")
        self.assertGreaterEqual(Out.flushes, 1)


class TestIngestLogFlushes(unittest.TestCase):
    """Same trap one command over: a detached ingest redirected to a file
    showed an empty log for its first minutes."""

    def test_each_log_line_reaches_the_file_at_once(self):
        from recall import db, ingest
        captured = {}

        def run(root, conn, reindex=True, log=print):
            captured["log"] = log
            return {}

        class Out(io.StringIO):
            flushes = 0

            def flush(self):
                Out.flushes += 1
        with mock.patch.object(ingest, "run", run), \
             mock.patch.object(db, "connect",
                               lambda: contextlib.nullcontext(None)), \
             contextlib.redirect_stdout(Out()):
            cli.main(["ingest"])
            captured["log"]("one line")
        self.assertGreaterEqual(Out.flushes, 1)


@contextlib.contextmanager
def _conn():
    yield object()


class TestLabelCommand(unittest.TestCase):
    def test_label_writes_through_save_agent(self):
        from recall import db, labels
        seen = {}

        def save_agent(conn, query_id, verdict, name, note):
            seen.update(id=query_id, verdict=verdict, name=name, note=note)
        with mock.patch.object(labels, "save_agent", save_agent), \
             mock.patch.object(db, "connect", _conn), \
             mock.patch.object(db, "apply_schema", lambda conn: None), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["label", "7", "bad", "--by", "claude-fable-5.1",
                             "--note", "the reference holds the fact"])
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"id": 7, "verdict": "bad", "name": "claude-fable-5.1",
                                "note": "the reference holds the fact"})

    def test_a_third_verdict_and_a_missing_note_stop_at_the_parser(self):
        for argv in (["label", "7", "partly", "--by", "a", "--note", "n"],
                     ["label", "7", "good", "--by", "a"],
                     ["label", "7", "good", "--note", "n"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli.main(argv)


class TestShowCommand(unittest.TestCase):
    def test_show_prints_the_screen_for_the_id(self):
        from recall import db, review
        out = io.StringIO()
        with mock.patch.object(review, "show", lambda conn, i: f"SCREEN {i}"), \
             mock.patch.object(db, "connect", _conn), \
             mock.patch.object(db, "apply_schema", lambda conn: None), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["show", "42"]), 0)
        self.assertIn("SCREEN 42", out.getvalue())


class TestReportCommand(unittest.TestCase):
    ROWS = [{"id": 1, "verdict": "good", "labelled_at": None, "judge_correct": "no",
             "judge_question_type": "recall", "agent_verdict": None, "agent_at": None,
             "question": "who raced"},
            {"id": 2, "verdict": None, "labelled_at": None, "judge_correct": "yes",
             "judge_question_type": "recall", "agent_verdict": None, "agent_at": None,
             "question": "when"}]

    def run_report(self, *flags):
        import json
        from recall import db, labels
        out = io.StringIO()
        with mock.patch.object(labels, "rows", lambda conn: self.ROWS), \
             mock.patch.object(db, "connect", _conn), \
             mock.patch.object(db, "apply_schema", lambda conn: None), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["report", *flags]), 0)
        return out.getvalue()

    def test_report_prints_the_one_screen(self):
        text = self.run_report()
        self.assertIn("1 open disagreements", text)
        self.assertIn("#1", text)
        self.assertIn("agrees with the human on 0 of 1", text)

    def test_json_carries_the_summary_and_the_dossier(self):
        import json
        data = json.loads(self.run_report("--json"))
        self.assertEqual(data["summary"]["open"], 1)
        self.assertEqual([d["id"] for d in data["disagreements"]], [1])
        self.assertEqual(data["disagreements"][0]["question"], "who raced")


class TestStatsCommand(unittest.TestCase):
    def run_stats(self, argv, result=None):
        from recall import stats
        seen = {}

        @contextlib.contextmanager
        def conn():
            yield object()

        def run(c, call, labels, tz):
            seen["call"] = call
            return {"call": stats.validate(call), "label": None, "handles": [],
                    "candidates": [], "rows": [], "first": None, "last": None,
                    "streak": (0, None, None), "gap": (0, None, None),
                    "excluded": 0}
        out = io.StringIO()
        with mock.patch.object(cli, "_embedder", lambda: None), \
             mock.patch("recall.db.connect", conn), \
             mock.patch("recall.db.apply_schema", lambda c: None), \
             mock.patch.object(stats, "labels", lambda c, s: {}), \
             mock.patch.object(stats, "run", run), \
             contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, seen.get("call"), out.getvalue()

    def test_count_passes_the_person_and_the_period(self):
        code, call, out = self.run_stats(
            ["stats", "count", "--with", "Ada", "--by", "month", "--source", "email"])
        self.assertEqual(code, 0)
        self.assertEqual(call, {"metric": "count", "source": "email",
                                "with": "Ada", "by": "month"})
        self.assertIn("[stats: count, email with", out)

    def test_top_passes_year_direction_and_n(self):
        code, call, _ = self.run_stats(
            ["stats", "top", "--year", "2019", "--direction", "sent", "-n", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(call, {"metric": "top", "source": "messages",
                                "year": 2019, "direction": "sent", "n": 3})

    def test_json_prints_the_result_for_an_agent(self):
        import json
        code, call, out = self.run_stats(["stats", "top", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["call"]["metric"], "top")
        self.assertEqual(data["rows"], [])

    def test_a_call_outside_the_catalogue_exits_two_before_the_database(self):
        code, call, _ = self.run_stats(["stats", "top", "-n", "999"])
        self.assertEqual(code, 2)
        self.assertIsNone(call)
