# Lessons

Every item here cost a real run. They are written down because each one is
invisible until it bites, and most of them fail *silently*: the pipeline
reports success while quietly discarding data.

## Failures that look like success

**A dead model server and an oversized request both answer HTTP 502.** They
need opposite responses. A client that reacts to every failure by shrinking
the request will, against a dead server, bisect a batch down to single items
and write each one off as bad input. One run lost 55 chunks this way and the
log blamed the chunks, including one 279 characters long that could not
possibly have overrun an 8,192 token context. The tell is exactly that: a
tiny payload in a size-related error list means the error is not about size.

The fix is to ask the server's health endpoint before reacting, wait out the
restart, retry the same batch, and only treat a refusal as an oversize signal
once a healthy server has given one. `recall.embed.embed_safe` does this. In
production it later rode out 20 server restarts in a single run with zero
loss.

Related: **502 means the backend is gone, 400 and 500 mean it rejected this
specific request.** The first is worth retrying, the second is not.

**A wrong vector dimension is that same trap in new clothes.** The server
answers HTTP 200 and returns vectors of the wrong length, because the model
running is not the model configured. That reaches the retry layer as an
ordinary error, so the bisect splits the batch down to single items and
writes each one off as bad input. Eight chunks became eight drops and thirty
wasted requests, and a real corpus becomes a total loss under a plausible
"dropped N" line. Shrinking a request cannot change the answer, so a setup
fault has to stop the run rather than enter the retry path.
`recall.embed.EmbeddingMisconfigured` inherits Exception and not
RuntimeError for exactly that reason: the retry paths catch RuntimeError,
and a shared base class lets one of them swallow the fault.

**The check that should have caught it passed.** `recall doctor` posted to
the embedding server and reported success on HTTP 200, without ever reading
the vector it got back. That is the green-target lesson again in a second
place. Doctor now embeds one string and prints the dimension it measured.

**A pipeline piped through `tail` reports the exit code of `tail`.** So
`timeout 3000 job | tail -8` returns 0 even when `timeout` killed the job at
50 minutes. One load looked finished, with a clean exit and a plausible final
progress line, having written 10,842 of 11,529 rows. The tell was a missing
completion line: progress lines survived, the summary never printed. Never
read exit 0 from a piped long-running job as proof. Redirect to a file, or
check the job's own completion marker.

**A green metrics target proves HTTP 200, not that metrics exist.** An
exporter served `up 1` and nothing else for days because it read a device
path that had been renumbered at boot. Alert on a value inside the payload,
never on scrape success alone.

**A missing file in a partly-copied directory says nothing about the
source.** Twice in one day an "absent" export turned out to be a copy still
in flight. Verify by content coverage, not by filename.

## Sizing

**A character budget is a guess about tokens, and the guess is wrong.** The
ratio varies by a factor of three across ordinary content:

| Content | characters per token |
|---|---|
| prose documents | 2.4 to 4.4 |
| workout and sensor lines | 1.8 |
| spreadsheet exports | 1.51 |
| marketing and airline mail | **1.39** |

A 12,000 character budget set for prose is 8,633 tokens of dense mail, which
an 8,192 token server rejects outright. That happened, three separate times,
with three different ratios. `recall.chunking.calibrate` measures the real
ratio against the real tokenizer instead, and takes the *worst* ratio in the
sample rather than the average, because it is the dense tail that fails.

**Splitting a container on whole records is not enough.** One airline notice
arrived as a single record of 188,218 characters, roughly 47,000 tokens, in a
thread holding one message, so a per-record split never touched it. Oversized
records need splitting themselves.

**Verify by tokenizing, never by estimating.** It costs one request.

## Chunk shape

**Roll up high-frequency events, and keep their detail.** 391,896 music plays
as one chunk each would nearly double a corpus with pure noise and make every
unrelated query worse. But a bare monthly total cannot answer "when did I
start jiu jitsu". A rollup chunk summarises the period *and* lists the
individual events inside it, so both questions work.

**A session gap is a property of the medium, not of the code.** Live chat
separates conversations at 30 minutes. Asynchronous messaging does not: at 30
minutes, 39 percent of one export's sessions came out as a single short
message, which embeds to noise. Measured across five gap values, a day put
the median chunk where live chat's 30 minutes put it.

**Identity must be stable.** The loader skips a stored ref whose text has not
moved, which is what makes a re-run cheap and a resume possible. When one budget change
re-split some threads, the reload embedded 1,300 chunks and skipped 21,354
untouched. An unstable ref turns every re-run into a full re-embed.

**A stable ref is not the same as unchanged text, and skipping on the ref
alone conflates the two.** A contact map added after the first ingest
rewrites the text of chunks whose refs never move, so every one of them read
as already held and nothing reloaded. The README promised the opposite. The
loader now compares an md5 of the text, which Postgres computes so the
corpus never crosses the wire, and it counts new and rewritten chunks
separately. Re-runs stay exactly as cheap, because an unchanged chunk still
costs one hash comparison and no embedding.

**A window bounds turns, not characters.** A twenty turn session and a busy
month are both unbounded in size. One oversized chunk does not fail loudly:
the server refuses it, the retry bisects a single item down to itself, and it
is dropped. On one export the largest direct message chunk came to 5,871
characters against a 7,700 budget, so nothing was lost. That is data luck, not
a guarantee. Bound the size where the chunk is built.

**The header comes out of the budget.** A body packed right up to the limit
plus a header is over the limit. It only shows at certain line lengths, so a
test with one fixed length passes while the bug is still there. Sweep the
lengths. `chunking.parts` reserves the header and numbers the parts.

**Number a part only when there is more than one.** A period that fits keeps
its bare ref, so a later run does not re-embed every chunk that never changed.

**iCalendar is a line format with three traps, and all three fail silently.**
Long lines fold: a line starting with a space or a tab continues the one
before it. Read the file without unfolding and every long value truncates
at the fold, so 728 real descriptions measured as empty, median length one
character folded and 286 unfolded. Then, a `VTIMEZONE` block carries its own
`DTSTART` for the daylight rule, always dated 1970, so a whole-file scan for
`DTSTART` invents events that never happened; read `VEVENT` blocks only.
Then, `DTSTART` comes in three forms, UTC, an all-day date, and a time in a
named zone, and reading only the UTC form misplaces the other two by up to
seven hours. One more that bites on refs: `RECURRENCE-ID` marks one edited
occurrence and reuses the parent `UID`, so without a suffix the two collide
on the unique ref and one is silently lost.

## Naming people

**Export your address book from wherever it actually lives.** The same corpus
matched 8 percent of chunks from one provider's contact export and **52
percent** from another. The thin one was a stale partial copy, and it did not
contain the single most frequent number in the corpus, which appeared in
73,076 chunks on its own. Measure a contact export against the corpus before
trusting it.

**Put the name in the chunk text, and keep the raw identifier as the join
key.** Replacing a phone number with a display name makes the next contacts
update impossible, because there is nothing left to match on. Text also gets
full-text search for free where a metadata column does not, because `tsv` is
generated from `text`.

**Predict the affected count from the database first, then reconcile.** One
rename pass regenerated 76,679 changed chunks against a predicted 122,501.
The 46,569 gap was a second chunk builder for attachments, which carried the
same participant lists but was never given the contact map. Without the
prediction the pass would have shipped silently missing a third of its work.

**Sort the labels.** Unstable ordering makes an unchanged chunk look changed,
and it re-embeds for nothing.

**An export that names people by opaque id often carries its own name table
somewhere else in the same archive.** A Twitter archive identifies every
direct message sender by numeric account id and ships no directory, so 86,910
messages read as conversations with strangers. The `user_mentions` inside your
own tweets pair an id with a screen name, which resolved about 30 percent of
senders and covered the busiest threads. Leave the rest as their raw id: one
shared "unknown" bucket merges separate people into a single apparent
speaker.

## Retrieval

**Hybrid always.** Pure vector search underperforms on a personal archive
because so many real questions are metadata questions wearing semantic
clothes. Fuse on **rank**, never on score: cosine distance and `ts_rank` are
not comparable numbers, and normalising them invents a relationship.

**Date parsing must be conservative.** A wrong date filter does not degrade
an answer, it removes it, and the user sees a confident "nothing found"
instead of a mistake. So it fires only on an explicit year or an unambiguous
relative phrase. A bare month name does not fire, because "march" is also a
verb. A four-digit run inside a phone number does not fire, because phone
numbers contain things like 2026.

**pgvector returns fewer rows than your LIMIT, and calls it success.**
`hnsw.ef_search` caps the HNSW candidate list at 40 by default, whatever
`LIMIT` asks for. A short result set is not an error, so a query written
`ORDER BY embedding <=> $1 LIMIT 50` returns 25 rows and raises nothing. The
dense arm of a hybrid retriever then fuses on half the candidates it thinks
it has, and the code, the plan and the logs all still say 50. Measured on a
249k-row corpus: 25 rows at ef 40, 50 rows at ef 100, asking for 50 each
time. Diagnose it by counting rows returned against rows requested. Reading
the query cannot show you this.

Fix it in the code, not only in the database. `ALTER DATABASE ... SET
hnsw.ef_search` is a reasonable floor, but a fixed default only moves the
threshold: the next caller with a bigger pool under-fills again, which is the
same bug one level up. So the width follows the pool the caller asked for.
Two details carry their own reasons. `set_config` runs at session scope
rather than `SET LOCAL`, because `SET LOCAL` outside a transaction block does
nothing and only warns, which would restore the silent shortfall in the case
hardest to notice. And recall reads the applied value back, because
`set_config` returns what it set, so checking the payload instead of the
statement costs nothing.

**Citations are mandatory, and an empty retrieval must forbid an answer.**
Send the bare question through and the model answers from its weights.
Nothing downstream can distinguish that from real recall about the user's own
life. It is the worst failure this kind of system has.

## Ingestion

**Decide exclusions before ingesting, not after.** A filter anchored to a
rooted path fails *open* once the tree is reorganised: it stops matching
while the include rules keep matching. One such filter would have leaked
about 19,000 repository files and a credentials file into a corpus. Match on
path segments and names.

**`pathlib.rglob` never enters a symlinked directory.** Someone with a 136 GB
message archive links it into the data directory instead of copying it, and
every adapter then reports nothing while `doctor` says "no recognised
sources". The tool looks like it ran. `sources.base.walk` uses `os.walk` with
`followlinks=True` and remembers real paths, so a link back to a parent ends
the walk instead of looping forever.

**Do not trust file extensions.** A Photoshop file named `.pdf` becomes two
million characters of noise. Sniff the content.

**Never write a checkpoint when the work failed.** A captioning run wrote a
"done" record on model failure. A server restart then made those files
permanently skipped, because the checkpoint claimed success.

**Build the vector index last.** Loading into an existing HNSW index makes
every insert pay maintenance. Building it afterwards took two minutes.

**Let Postgres build the JSON.** Chunk text carries newlines, commas, and
quotes, so any delimiter-based parse of a text table corrupts exactly the
rows containing the delimiter, and does it silently.

## Memory, on a shared-memory machine

**Model weights do not appear in process RSS.** On a unified-memory APU they
land in GPU-accessible system memory, so `ps` showed 0 GB for a model
occupying 70 GB. Measure with the GPU's own counters and with free memory.

**A configuration comment claiming a footprint is not evidence.** One budget
block read "embed ~1 GB"; the real figure was 8 GB, and that single error was
the whole margin. All models resident, the box sat at 2 GB free and the OOM
killer took a model server repeatedly, which surfaced to the client as HTTP
502, which then looked like bad data. Two lessons chained into one outage.

**A router that cold-starts its largest model refills the memory you just
freed.** One ordinary request pulled 70 GB back in with nobody involved.

## Interface

**Streaming makes nothing faster and changes everything.** A fifty second
wait behind a static message reads as broken. Sending sources first (they
take a fifth of a second) and then tokens as they generate cut the perceived
wait by 95 percent, with identical total time.

**Three separate layers will buffer your stream** and any one of them
restores the stall: the server must flush every frame, a reverse proxy needs
buffering off, and an intermediate service must pipe rather than await.

**Render the model's Markdown, do not strip it.** The structure is real
information: bullets and bold separate distinct findings, and flattening them
produces a wall of text that reads worse than stray asterisks. Parse it once,
server-side, into a structure, and never send HTML built from archive text.
