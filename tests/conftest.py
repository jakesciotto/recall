"""Suite-wide isolation from the database and the network.

A passing suite is not evidence of isolation. Two tests once patched a
function that production had stopped calling, so the patches applied to
nothing, the tests ran the real retrieval path against a live database,
wrote rows into a production log table, and still passed. mock.patch fails
open when the code under test stops calling the name you patched.

So the cut is made once, here, at the two functions that reach out, and it
covers tests nobody has written yet. A test that wants the real thing must
say so with the `live` marker.
"""

import urllib.request

import pytest

from recall import db


class Isolated(RuntimeError):
    pass


def _refuse(what):
    def refuse(*args, **kwargs):
        raise Isolated(f"{what} is blocked in tests; mark the test `live` "
                       f"if it really needs it")
    return refuse


@pytest.fixture(autouse=True)
def isolate(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        yield
        return
    monkeypatch.setattr(db, "connect", _refuse("db.connect"))
    monkeypatch.setattr(urllib.request, "urlopen",
                        _refuse("urllib.request.urlopen"))
    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: the test may reach the real database or network")
