import contextlib
import unittest
from unittest import mock

from recall import cli

VERBS = ("doctor", "ingest", "ask", "eval", "judge", "review", "serve")


class TestEveryVerbDispatches(unittest.TestCase):
    """A subparser that forgets set_defaults(fn=...) parses cleanly and
    then dies on args.fn. Nothing else would catch it."""

    def test_each_verb_reaches_its_function(self):
        for argv in (["doctor"], ["ingest"], ["ask", "q"], ["eval", "f"],
                     ["judge"], ["review"], ["serve"]):
            with self.subTest(verb=argv[0]):
                seen = []
                with contextlib.ExitStack() as stack:
                    for verb in VERBS:
                        stack.enter_context(mock.patch.object(
                            cli, f"cmd_{verb}",
                            lambda a, v=verb: seen.append(v) or 0))
                    self.assertEqual(cli.main(argv), 0)
                self.assertEqual(seen, [argv[0]])
