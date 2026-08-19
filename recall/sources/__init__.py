"""Adapter registry and auto-detection."""

from .base import Chunk, Source          # noqa: F401
from .files import Files
from .imessage import IMessage
from .mbox import Mbox
from .spotify import Spotify

ADAPTERS = [IMessage(), Mbox(), Spotify(), Files()]


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
