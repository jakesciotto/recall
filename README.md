# recall

Search your own life. recall indexes your documents, messages, and mail, then
answers questions from them with citations, on your own machine. No account,
no upload, no API key. Nothing leaves the box.

```
$ recall ask "what did I write about the Chicago trip in 2018"
date filter: 2018 -> 2018-01-01 .. 2019-01-01
[1] messages/453359 (2018-05-29)
[2] email:1772970206 (2018-06-07)
```

Point it at a generation endpoint you already run and it writes the answer
too, citing the same sources. That half is optional and nothing is bundled;
see [docs/answering.md](docs/answering.md).

## Start

```bash
git clone <this repo> && cd recall
cp .env.example .env
docker compose up -d          # postgres + an embedding server, weights download themselves
pip install -e .

# drop your exports into data/ (see the table below), then:
recall doctor                 # says exactly what is missing, if anything
recall ingest                 # finds what you dropped in and indexes it
recall ask "when did I last see the dentist"
```

`recall doctor` is the one to run when anything looks wrong. It checks every
dependency, names the command that fixes each, and tells you what to do next.

The first `docker compose up` downloads the embedding model and takes a
while. Everything after that is local and immediate.

Writing prose answers is optional and uses whatever OpenAI-compatible
endpoint you already run. recall does not bundle one, because which model you
run is your choice and your hardware's.

## What you drop in

Put exports anywhere under `data/`. Nothing needs renaming or configuring:
each adapter recognises its own files.

| Put this in `data/` | Recognised by | Where to get it |
|---|---|---|
| `chat.db` | Apple Messages | `~/Library/Messages/chat.db` on a Mac |
| any `*.mbox` | Email | Google Takeout, Thunderbird, offlineimap |
| a Twitter/X archive | Tweets and DMs | X, Settings, Download an archive |
| a Spotify export folder | Listening history | Spotify privacy page |
| `export.zip` | Apple Health workouts | Health app, profile, Export |
| `documents/` | PDFs, Office files, text | anywhere |
| any `*.vcf` | Contact names | Contacts app, Export vCard |

`recall doctor` lists what it found. Adding a source means writing one small
class; see [docs/sources.md](docs/sources.md).

## Why it works well

Most of this repo is ordinary. A few decisions are not, and each one exists
because its absence broke a real run. They are written up in
[docs/lessons.md](docs/lessons.md), and the short version is:

- **Chunk budgets are measured, not guessed.** Text runs anywhere from 1.4 to
  4.4 characters per token depending on what it is. A budget picked for prose
  silently rejects a third of your mail.
- **A restarted model server is not bad data.** Both look like HTTP 502.
  Treating the first as the second quietly discards good documents and blames
  them.
- **Search is hybrid, always.** Half of real questions are metadata questions
  in disguise ("the invoice from spring 2023"), and vector search alone
  answers those badly.
- **Every answer cites its sources**, and an empty retrieval is forbidden
  from answering at all. An uncitable answer about your own life is
  indistinguishable from an invented one.
- **Re-runs are cheap.** Identity is stable, so re-ingesting costs only what
  actually changed. A failed twelve hour run resumes instead of restarting.
- **People have names.** Drop in a vCard and participants stop being phone
  numbers. On one corpus that named 52 percent of every chunk stored.
- **Every question is logged, locally.** What retrieval offered, what the
  model cited, and how long each stage took, in the same database as the
  corpus. Nothing new leaves the box. The first real query through it found
  the dense arm returning half the candidates it was asked for.
  `RECALL_QUERY_LOG=0` turns it off.

## Asking from a browser or another app

```bash
export RECALL_API_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
recall serve
```

`POST /ask` returns JSON. `POST /ask/stream` sends Server-Sent Events:
sources first, then answer tokens as they generate, then the parsed answer.
With no generation endpoint configured, both return sources alone.
Both require the bearer token, and the server refuses to start without one.

It binds loopback by default. A retrieval API over a personal archive is a
much more valuable target than a bare model endpoint, so put it on a private
interface and keep the token server-side.

## Requirements

Docker, Python 3.10 or newer, and about 2 GB of disk for the embedding model.
A GPU makes indexing far faster but nothing here requires one.

## Licence

MIT.
