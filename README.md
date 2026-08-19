# recall

Ask questions about your own life, answered from your own archive, on your
own machine. No account, no upload, no API key. Your mail and messages never
leave the box.

```
$ recall ask "what did I write about the Chicago trip in 2018"
date filter: 2018 -> 2018-01-01 .. 2019-01-01
[1] messages/453359 (2018-05-29)
[2] email:1772970206 (2018-06-07)

In 2018 you wrote about Chicago twice. On 29 May you said you were
"extremely relieved" after worrying you would not be able to go [1]. On
7 June you described the city as "awesome" [2].
```

## Start

```bash
git clone <this repo> && cd recall
cp .env.example .env
docker compose up -d          # postgres + two model servers, weights download themselves
pip install -e .

mkdir -p data
# drop your exports into data/ (see below), then:

recall doctor                 # says exactly what is missing, if anything
recall ingest                 # finds what you dropped in and indexes it
recall ask "when did I last see the dentist"
```

The first `docker compose up` downloads model weights and takes a while.
Everything after that is local and immediate.

## What you drop in

Put exports anywhere under `data/`. Nothing needs renaming or configuring:
each adapter recognises its own files.

| Put this in `data/` | Recognised by | Where to get it |
|---|---|---|
| `chat.db` | Apple Messages | `~/Library/Messages/chat.db` on a Mac |
| any `*.mbox` | Email | Google Takeout, Thunderbird, offlineimap |
| a Spotify export folder | Listening history | Spotify privacy page |
| `documents/` | PDFs, Office files, text | anywhere |

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

## Asking from a browser or another app

```bash
export RECALL_API_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
recall serve
```

`POST /ask` returns JSON. `POST /ask/stream` sends Server-Sent Events:
sources first, then answer tokens as they generate, then the parsed answer.
Both require the bearer token, and the server refuses to start without one.

It binds loopback by default. A retrieval API over a personal archive is a
much more valuable target than a bare model endpoint, so put it on a private
interface and keep the token server-side.

## Requirements

Docker, Python 3.10 or newer, and about 10 GB of disk for the models. A GPU
makes ingestion far faster but nothing here requires one.

## Licence

MIT.
