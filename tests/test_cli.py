import contextlib
import io
import pathlib
import unittest
from unittest import mock

from recall import captions, cli

VERBS = ("doctor", "ingest", "caption", "ask", "eval", "judge", "review",
         "serve")


class TestEveryVerbDispatches(unittest.TestCase):
    """A subparser that forgets set_defaults(fn=...) parses cleanly and
    then dies on args.fn. Nothing else would catch it."""

    def test_each_verb_reaches_its_function(self):
        for argv in (["doctor"], ["ingest"], ["caption"], ["ask", "q"],
                     ["eval", "f"], ["judge"], ["review"], ["serve"]):
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
