import unittest
import urllib.request

import pytest

from recall import db


class TestTheCutIsReal(unittest.TestCase):
    """A fixture nobody has seen fail is a fixture nobody can trust."""

    def test_the_database_is_unreachable(self):
        with self.assertRaisesRegex(RuntimeError, "blocked in tests"):
            with db.connect():
                pass

    def test_the_network_is_unreachable(self):
        with self.assertRaisesRegex(RuntimeError, "blocked in tests"):
            urllib.request.urlopen("http://127.0.0.1:1/")


@pytest.mark.live
def test_a_live_test_keeps_the_real_functions():
    assert db.connect.__name__ == "connect"
    assert urllib.request.urlopen.__name__ == "urlopen"
