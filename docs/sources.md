# Sources

An adapter answers two questions for itself: "is my data in here?" and "what
chunks does it produce?". That is why `recall ingest` needs no configuration.

## Built in

| Adapter | Detects | Chunk shape |
|---|---|---|
| `imessage` | any `chat.db` | one conversation window, 30 minute gap |
| `mbox` | any `*.mbox` | one email thread, allowlist filtered |
| `twitter` | any `tweets.js` | one day of tweets, or one DM window |
| `spotify` | `*Streaming_History_Audio*.json` | one month, with detail inside |
| `health` | `export.xml`, or the `export.zip` around it | one month of workouts |
| `files` | a `documents/` directory | paragraph split with overlap |

`health` opens `export.xml` only. The FHIR clinical records that Apple ships
in the same export are never read, and `clinical-records` is excluded from the
catch-all as well, so indexing medical data stays a deliberate choice.

Drop any `*.vcf` under the data directory as well. It is not a source and
produces no chunks; every adapter uses it to show contact names instead of
phone numbers and email addresses. On one corpus this named 52 percent of all
chunks.

## Writing one

```python
from recall.sources.base import Chunk, Source

class Journal(Source):
    name = "journal"

    def detect(self, root):
        d = root / "journal"
        return [d] if d.is_dir() else []

    def samples(self, path):
        # The LONGEST texts you produce. Budgets are calibrated from these,
        # and short samples measure nothing: the dense long ones are what
        # overrun the context.
        return [p.read_text()[:20000] for p in sorted(path.glob("*.md"))[:8]]

    def chunks(self, path, budget, contacts=None):
        # `contacts` maps a phone or email to a name. Put the name in the
        # TEXT and keep the raw identifier in `participants`.
        for p in sorted(path.glob("*.md")):
            yield Chunk(
                ref=f"journal:{p.stem}",     # STABLE across runs
                text=p.read_text(),
                source=self.name,
                occurred_at=f"{p.stem}T00:00:00Z",
                date_confidence="exact",
            )
```

Add it to `ADAPTERS` in `recall/sources/__init__.py`. Nothing else changes.

## Three rules

1. **`ref` must be stable and unique.** The loader skips a ref it already
   holds whose text has not changed, which is what makes a re-run cheap and
   a resume possible. It compares the text digest rather than the ref alone,
   so a chunk you now write differently still reloads. An unstable ref
   re-embeds your whole corpus; a colliding one drops data.
2. **Respect the budget.** It is calibrated to your embedding server. Use
   `recall.chunking.pack` or `split_to_budget` rather than assuming a size.
3. **Be honest about dates.** `date_confidence` travels with `occurred_at`
   so a guessed date never looks like a real one. Use `exact` only when the
   source actually stated it.

## Rolling up high-frequency events

If a source emits tens of thousands of small events, do not make a chunk per
event: it drowns the corpus. Roll up to a period and list the events inside,
so both "what was I doing that month" and "when did X first happen" work. See
`recall/sources/spotify.py`, and `recall.chunking.rollup`.
