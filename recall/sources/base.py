"""The source adapter contract. See docs/sources.md to add one."""

import dataclasses
import os
import pathlib


REQUIRED_KEYS = {"ref", "text", "source", "occurred_at", "date_confidence"}


def walk(root):
    """Every file under `root`, entering symlinked directories.

    People symlink a large export into the data directory rather than copy
    tens of gigabytes, and pathlib.rglob skips those without a word. Real
    paths are tracked so a link back to a parent ends the walk rather than
    looping forever.
    """
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames.sort()
        for name in sorted(filenames):
            yield pathlib.Path(dirpath) / name


@dataclasses.dataclass
class Chunk:
    """One embeddable unit.

    `ref` must be stable across runs: the loader skips refs it already holds.
    `date_confidence` travels with `occurred_at` so a guessed date never
    looks like a stated one.
    """
    ref: str
    text: str
    source: str
    occurred_at: str | None = None
    date_confidence: str = "low"
    participants: list[str] = dataclasses.field(default_factory=list)
    thread: str | None = None
    path: str | None = None

    def as_dict(self):
        return dataclasses.asdict(self)


class Source:
    """Base adapter. Subclass, set `name`, implement `detect` and `chunks`."""

    name = "unnamed"

    # The LONGEST texts this source produces. Budgets calibrate from these.
    def samples(self, path):
        return []

    def detect(self, root):
        """Paths under `root` this adapter can ingest. Empty means absent."""
        raise NotImplementedError

    def chunks(self, path, budget, contacts=None):
        """Yield Chunk objects.

        `budget` is the calibrated character budget. `contacts` maps a phone
        or email to a display name; put the name in the TEXT and keep the raw
        identifier in `participants`, which is the join key for the next
        contacts update.
        """
        raise NotImplementedError
