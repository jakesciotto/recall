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

**Identity must be stable.** The loader skips refs it already holds, which is
what makes a re-run cheap and a resume possible. When one budget change
re-split some threads, the reload embedded 1,300 chunks and skipped 21,354
untouched. An unstable ref turns every re-run into a full re-embed.

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
