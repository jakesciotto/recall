"""Adapter registry and auto-detection."""

from .base import Chunk, Source          # noqa: F401
from .files import Files
from .health import Health
from .imessage import IMessage
from .mbox import Mbox
from .spotify import Spotify
from .twitter import Twitter

ADAPTERS = [IMessage(), Mbox(), Twitter(), Spotify(), Health(), Files()]


def detect_all(root):
    """[(adapter, path)] for every source found under root.

    Order matters only in that Files runs last: it is the catch-all for
    loose documents, and a specific adapter should claim its own export
    first.
    """
    found = []
    for adapter in ADAPTERS:
        for path in adapter.detect(root):
            found.append((adapter, path))
    return found
