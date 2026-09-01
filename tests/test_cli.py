import unittest
from unittest import mock

from recall import cli


class TestEveryVerbDispatches(unittest.TestCase):
    """A subparser that forgets set_defaults(fn=...) parses cleanly and
    then dies on args.fn. Nothing else would catch it."""

    def test_each_verb_reaches_its_function(self):
        for argv in (["doctor"], ["ingest"], ["ask", "q"], ["judge"],
                     ["review"], ["serve"]):
            with self.subTest(verb=argv[0]):
                seen = []
                for name in ("cmd_doctor", "cmd_ingest", "cmd_ask",
                             "cmd_judge", "cmd_review", "cmd_serve"):
                    self.enterContext(mock.patch.object(
                        cli, name, lambda a, n=name: seen.append(n) or 0))
                self.assertEqual(cli.main(argv), 0)
                self.assertEqual(seen, [f"cmd_{argv[0]}"])
