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
| `calendar` | any `*.ics` | one month of events, plus one chunk per described event |
| `activity` | `My Activity/*/MyActivity.html` or `.json` | one product and month, each day's entries inside |
| `files` | a `documents/` directory | paragraph split with overlap |

`health` opens `export.xml` only. The FHIR clinical records that Apple ships
in the same export are never read, and `clinical-records` is excluded from the
catch-all as well, so indexing medical data stays a deliberate choice.

`calendar` writes every event into a month rollup and ALSO gives an event
with a real description its own chunk. That is on purpose: the month gives
context, the event gives detail, and descriptions hold meeting agendas and
forwarded mail, the highest-signal text in the file. A recurring event is
listed once, in its starting month, with its rule named.

Every source with timestamps also writes one **trends** chunk per year:
volume, busiest weekday and month, and what that source can say about
habits. Messages add the most-messaged contacts and the longest daily
streaks; twitter adds the hours you post; email adds the top senders,
labelled person or service by a heuristic; calendar adds recurring events
and how far ahead events were created; activity adds the hours you search
and the searches you repeat most. "On which day of the week did I
text the most in 2023" cannot be answered from eight retrieved chunks, and
a model that tries is over-claiming, so the answer is precomputed once and
retrieved like anything else. Years, weekdays and hours are local:
`RECALL_TZ` names the zone, and every rollup names it in its text.

## Documents

`files` is the catch-all, and it is the adapter that meets the oldest and
messiest part of an archive, so four rules live in it.

**The header is read before the extension is trusted.** A Photoshop file
named `.pdf`, a PDF named `.txt`, and a Word file named `.doc` that is really
RTF all read correctly, because the first bytes decide. Anything that is not
text or a known document yields nothing rather than noise. A spreadsheet
reads as its sheet names and tab-joined rows, numbers included. UTF-16 files with
a byte order mark are decoded, not rejected as binary. Legacy `.doc` needs
`antiword` on the path; without it those files are skipped and `doctor`
says so.

**Dates come from the path first.** A folder or file name holding a year, a
month, or a season is your own claim about when the document belongs, and it
beats everything. Then the creation date the writing application stamped
into a PDF (`pdfinfo`, optional) or an Office file. The mtime is last, and
`date_confidence` says `path`, `metadata`, or `mtime` so a guess never reads
as a fact.

**The ref follows the content.** Two copies of one file are one document,
indexed once under the first path found. A moved or renamed file keeps its
ref and is not re-embedded.

**Your exclusions go in `documents/.recallignore`.** One pattern per line,
`#` comments. A pattern without a slash matches a file or directory name at
any depth (`drafts`, `*.bak`, `datamining.pdf`); a pattern with a slash
matches the path relative to `documents/` (`finance/tax*`). Matching ignores
case. Vendored code, caches, a `books` folder, and macOS `._*` forks are
skipped without being listed.

## Activity

`activity` reads Google Takeout's My Activity export, one file per product:
Search, Chrome, YouTube, Maps, Image Search, and whatever else the account
used. Takeout writes HTML unless JSON is chosen at export time; both are
read, and JSON is a tenth of the size. Every entry is a verb, a title, and a
time. A month of one product is one chunk, with each day's entries listed
under the day and repeats folded into a count, so "what was I researching
in March 2021" reads one chunk and "when did I first look up X" finds the
line. Link targets are dropped: the titles carry the meaning, and a title
that is itself a URL keeps its host and path, never the query string. The
map links Image Search attaches to each entry are a location trail and
never reach a chunk.

Two things to know. Takeout stamps every entry with the account's current
zone abbreviation as a fixed offset, MDT on a February date included, so
the adapter converts by that offset and renders in `RECALL_TZ`. And the
YouTube export's own `watch-history` and `search-history` files hold the
same entries as `My Activity/YouTube`, so drop in one or the other. Search
history is the most sensitive file in a Takeout; dropping it in is the
choice, and deleting a product folder from the data directory removes it.

Drop any `*.vcf` under the data directory as well. It is not a source and
produces no chunks; every adapter uses it to show contact names instead of
phone numbers and email addresses. On one corpus this named 52 percent of all
chunks.

## Image attachments

Images are indexed as text: a caption from a vision model, the text OCR
finds inside the image, and the words said around the message that
carried it. Two steps, deliberately separate.

1. `recall caption` describes every image the adapters declare. It keeps
   one record per image in `RECALL_WORK/captions/`, keyed by the file's
   sha256, and writes a record on success or when it gates a tiny image,
   never on a failure. So a vision server that restarts mid-run costs the
   images in flight and nothing else; the next run retries them. It is
   resumable and safe to interrupt. `-j` sets the workers, `--limit` bounds
   new work for a smoke test.
2. `recall ingest` reads the cache and yields one chunk per captioned
   image. It never reads image bytes and never calls the vision endpoint,
   so an ingest stays cheap. It reports how many images still wait.

Configure `RECALL_VISION_URL` and `RECALL_VISION_MODEL`: any
OpenAI-compatible chat endpoint that accepts image content. Install
`pip install 'recall[captions]'` for Pillow. `magick` (ImageMagick) reads
what Pillow cannot, HEIC in particular, and needs a HEVC decoder plugin;
`tesseract` supplies the text inside images. Both are optional and
`recall doctor` says which are present.

The prompt is fixed. Captions written under different prompts do not
compare inside one index, so a prompt change means delete the cache and
recaption.

Apple Messages: copy `~/Library/Messages/Attachments` beside `chat.db` in
the data directory (a symlink works). The adapter reads the attachment
table, links each file to its message, and dates the image by that
message. Images are recognised by their first bytes: a large share carry
a `.pluginPayloadAttachment` extension and are ordinary photos. Videos
are not captioned.

## Writing one

```python
from recall.sources.base import Chunk, Source

class Journal(Source):
    name = "journal"

    def detect(self, root):
        d = root / "journal"
        return [d] if d.is_dir() else []

    def samples(self, path):
        # The LONGEST texts you produce, and the DENSEST. Budgets are
        # calibrated from these. Short prose measures nothing; a short
        # numeric table measures the most, at a token per character.
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

An adapter with images implements `media(path)`, yielding the files for
`recall caption` to describe. Read the records back in `chunks` through
`recall.captions.read_record` and the hash cache's `known`, never by
hashing: only `recall caption` reads image bytes.

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

One walker rule: a directory named `Attachments` beside a `chat.db` is
skipped. It holds vCards, calendar invites and archives sent in chats,
and every adapter that walks by file name would otherwise claim them.

## Rolling up high-frequency events

If a source emits tens of thousands of small events, do not make a chunk per
event: it drowns the corpus. Roll up to a period and list the events inside,
so both "what was I doing that month" and "when did X first happen" work. See
`recall/sources/spotify.py`, and `recall.chunking.rollup`.
