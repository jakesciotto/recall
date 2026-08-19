"""The source adapter contract.

A user should drop an export into the data directory and run one command.
That works because every adapter answers two questions for itself: "is my
data in here?" and "what chunks does it produce?". Nobody has to tell the
tool what they downloaded.

Adding a source means writing one class and importing it. Nothing else in the
pipeline changes.
"""

import dataclasses


REQUIRED_KEYS = {"ref", "text", "source", "occurred_at", "date_confidence"}


@dataclasses.dataclass
class Chunk:
    """One embeddable unit.

    `ref` is the identity and it must be STABLE across runs. The loader skips
    refs it already holds, which is what makes a re-run cheap and a resume
    possible. An unstable ref turns every re-run into a full re-embed, and a
    colliding ref silently drops data.

    `occurred_at` carries `date_confidence` with it, because a guessed date
    and a real one must never look alike downstream.
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

    # Records this source emits, used to calibrate the character budget.
    # Return the LONGEST texts it produces: short samples measure nothing,
    # because it is the long dense ones that overrun the context.
    def samples(self, path):
        return []

    def detect(self, root):
        """Paths under `root` this adapter can ingest. Empty means absent."""
        raise NotImplementedError

    def chunks(self, path, budget):
        """Yield Chunk objects. `budget` is the calibrated character budget."""
        raise NotImplementedError
