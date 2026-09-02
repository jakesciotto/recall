import contextlib
import io
import re
import tempfile
import types
import unittest
from unittest import mock

from recall import chunking, cli, db, embed


class TestDoctorReadsTheVector(unittest.TestCase):
    """A green check proved HTTP 200 and nothing else. Doctor never read the
    answer, so the one fault that silently drains a whole corpus passed its
    own health check. Alert on a value inside the payload, never on the
    request succeeding."""

    def doctor(self, embedder):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as data:
            args = types.SimpleNamespace(data=data)
            with mock.patch.object(db, "connect",
                                   side_effect=OSError("no database")), \
                 mock.patch.object(chunking, "measure_density",
                                   return_value=3.0), \
                 mock.patch.object(embed, "embed", embedder), \
                 contextlib.redirect_stdout(out):
                code = cli.cmd_doctor(args)
        return code, out.getvalue()

    def healthy(self):
        return lambda texts, timeout=60: [[0.0] * 1024]

    def problems(self, text):
        m = re.search(r"(\d+) problem", text)
        return int(m.group(1)) if m else 0

    def line(self, text):
        found = [l for l in text.splitlines() if "embedding server" in l]
        self.assertTrue(found, "doctor must name the embedding server")
        return found[0]

    def test_it_reports_the_dimension_it_measured(self):
        _, text = self.doctor(self.healthy())
        self.assertIn("1024", self.line(text))

    def test_a_wrong_dimension_fails_the_check(self):
        _, text = self.doctor(mock.Mock(
            side_effect=embed.EmbeddingMisconfigured(
                "the model returns 768 dimensions, RECALL_EMBED_DIMS "
                "says 1024")))
        self.assertIn("FAIL", self.line(text))
        self.assertIn("RECALL_EMBED_DIMS", text)

    def test_a_wrong_dimension_counts_as_one_more_problem(self):
        _, good = self.doctor(self.healthy())
        _, bad = self.doctor(mock.Mock(
            side_effect=embed.EmbeddingMisconfigured("wrong dims")))
        self.assertEqual(self.problems(bad), self.problems(good) + 1)

    def test_an_unreachable_server_still_says_so(self):
        _, text = self.doctor(mock.Mock(side_effect=OSError("refused")))
        self.assertIn("FAIL", self.line(text))
        self.assertIn("docker compose up -d", text)


class TestDoctorChecksPdftotext(unittest.TestCase):
    """files.py shells out to pdftotext and read_text swallows every error,
    so without it every PDF yields nothing and nothing says why."""

    def doctor(self, found):
        import shutil
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as data:
            args = types.SimpleNamespace(data=data)
            with mock.patch.object(db, "connect",
                                   side_effect=OSError("no database")), \
                 mock.patch.object(chunking, "measure_density",
                                   return_value=3.0), \
                 mock.patch.object(embed, "embed",
                                   lambda texts, timeout=60: [[0.0] * 1024]), \
                 mock.patch.object(shutil, "which",
                                   lambda name: found if name == "pdftotext" else None), \
                 contextlib.redirect_stdout(out):
                cli.cmd_doctor(args)
        return out.getvalue()

    def test_a_missing_binary_is_a_named_failure(self):
        text = self.doctor(None)
        line = next(l for l in text.splitlines() if "pdftotext" in l)
        self.assertIn("FAIL", line)
        self.assertIn("poppler", text)

    def test_a_present_binary_passes(self):
        text = self.doctor("/usr/bin/pdftotext")
        line = next(l for l in text.splitlines() if "pdftotext" in l)
        self.assertNotIn("FAIL", line)
