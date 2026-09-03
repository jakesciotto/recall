import os
import pathlib
import tempfile
import unittest
from unittest import mock

from recall import config


class TestDotEnv(unittest.TestCase):
    """The README says cp .env.example .env. docker compose reads that file
    and the CLI must too, or a chat URL set there reports as unset."""

    def load(self, text, environ=None):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / ".env"
            path.write_text(text)
            with mock.patch.dict(os.environ, environ or {}, clear=True):
                config.load_dotenv(path)
                return dict(os.environ)

    def test_a_value_in_the_file_reaches_the_environment(self):
        env = self.load("RECALL_CHAT_URL=http://x/v1\n")
        self.assertEqual(env["RECALL_CHAT_URL"], "http://x/v1")

    def test_a_real_environment_variable_wins(self):
        env = self.load("RECALL_CHAT_URL=http://file/v1\n",
                        {"RECALL_CHAT_URL": "http://shell/v1"})
        self.assertEqual(env["RECALL_CHAT_URL"], "http://shell/v1")

    def test_comments_blanks_export_and_quotes(self):
        env = self.load('# comment\n\nexport RECALL_A="quoted"\n'
                        "RECALL_B='single'\nRECALL_C=bare # trailing\n")
        self.assertEqual((env["RECALL_A"], env["RECALL_B"], env["RECALL_C"]),
                         ("quoted", "single", "bare"))

    def test_a_missing_file_is_fine(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config.load_dotenv(pathlib.Path("/nonexistent/.env"))
        self.assertTrue(True)

    def test_only_recall_keys_are_taken(self):
        """A .env can hold POSTGRES_PASSWORD for compose. That is not the
        CLI's business, and importing it would be a surprise."""
        env = self.load("POSTGRES_PASSWORD=x\nRECALL_DATA=/d\n")
        self.assertNotIn("POSTGRES_PASSWORD", env)
        self.assertEqual(env["RECALL_DATA"], "/d")
