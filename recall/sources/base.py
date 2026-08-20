"""The source adapter contract. See docs/sources.md to add one."""

import dataclasses


REQUIRED_KEYS = {"ref", "text", "source", "occurred_at", "date_confidence"}


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
