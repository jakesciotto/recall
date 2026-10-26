# Answering

recall does two separable things. It **indexes and searches** your archive,
and it can optionally **write a prose answer** from what it found.

Only the first half is bundled. `docker compose up` starts a database and an
embedding server, and that is everything indexing and search need.

## Search without generation

Out of the box, `recall ask` returns ranked, cited sources:

```
$ recall ask "what did I write about the Chicago trip in 2018"
date filter: 2018 -> 2018-01-01 .. 2019-01-01
[1] messages/453359 (2018-05-29)
[2] email:1772970206 (2018-06-07)
```

That is often what you actually wanted: the document, not a paraphrase of it.
It also returns in a fraction of a second, against tens of seconds for a
written answer.

## Adding generation

Point `RECALL_CHAT_URL` at any OpenAI-compatible endpoint you already run.
recall does not bundle one, because which model you run is your choice and
your hardware's, not something a tool should decide for you.

```bash
# Ollama
RECALL_CHAT_URL=http://127.0.0.1:11434/v1/chat/completions
RECALL_CHAT_MODEL=llama3.1

# llama.cpp server
RECALL_CHAT_URL=http://127.0.0.1:8081/v1/chat/completions
RECALL_CHAT_MODEL=any

# LM Studio, vLLM, or anything else speaking the same API
```

`recall doctor` reports whether it can reach the endpoint. With none set it
says so plainly and carries on, because nothing else depends on it.

## What recall asks the model to do

The prompt is deliberately narrow. The model sees numbered sources and the
question, and it is told to answer only from those sources and to cite each
claim by number.

**An empty retrieval produces a prompt that forbids an answer.** Sending the
bare question instead lets the model answer from its own weights, and nothing
downstream can distinguish that from real recall about your life. It is the
worst failure this kind of system has, so recall refuses to allow it.

## Counts come from a store, not from the model

"How many texts have I sent to Ada over time", "who did I message most
in 2019", "which contact has the longest streak": a retriever cannot
count, and a model asked to count from eight chunks over-claims. So
`recall ingest` also fills a structured store beside the chunks: one
row per message, mail or direct message (`event`), one row per thread
with its members (`thread`), and the names an export carries itself
(`alias`). The store is replaced on every ingest, so a count matches the
export.

Three metrics run over it, and code owns every query:

| metric | arguments | what it says |
|---|---|---|
| `count` | `source`, `with` (a person or none), `by` (`year`, `month`, `all`) | sent and received per period, in the direct thread and in groups, with first, last, longest streak and longest gap. For `tweets` one column, the user's own tweets, with the same streak and gap |
| `top` | `source`, `year` (or none), `direction`, `n` | the people with the most messages |
| `streaks` | `source`, `n` | the longest daily streaks and gaps per person |

At the terminal, no model is involved:

```
recall stats count --with "Ada Lovelace" --by year
recall stats top --year 2019 --direction received -n 10
recall stats streaks --source twitter
recall stats count --source tweets --by year --json
```

`--json` prints the result as data, for an agent. Sources are `messages`,
`email`, `twitter` (direct messages) and `tweets` (your own tweets, no
other side, so "when did I stop tweeting" is a count with its gap). A
five or six digit sender is a short code, a bank or a 2FA service, and
never appears in a ranking or a streak list.

Through `recall ask` and the API, one planner call runs before
retrieval when a chat endpoint is set. The model sees the question and
the catalogue and answers with a JSON list of up to three calls, or
`null`. Code validates every field; a call outside the catalogue is
dropped before any query. Each valid call runs, and its rendered table
becomes a numbered source ahead of the retrieved chunks, taking one of
the `k` slots. The answering prompt does not change: the model cites
`[1]` like any other source, and every number in the table travels with
the sentence that says what it counted. A planner that fails, or a call
that fails, costs nothing: the ask runs as before and the log records
the error in `query_log.plan`.

A person resolves through the contacts map, so a name owns every
identifier mapped to it. Several names matching a substring is a
question back to the asker, never a guess, and the table says so. A
phone number, an email address or a Twitter id passes through as it is.

What the numbers mean, stated once here and again in every table.
"With" means a thread that includes the person. Direct is the
one-to-one thread; group is any thread with more members, which for
mail means a thread with two or more other addresses. Received in a
group counts the person's own messages there. Tapbacks, system rows
(joins, leaves, renames) and empty rows are not counted, and each table
says how many it excluded. Periods and days are local in `RECALL_TZ`.

The planner costs one short model call. Measured on one box: 0.4 to 6 s
per question across a 26B and a 120B model, and up to 15 s when the 120B
reasons over a question that compares years. The catalogue carries worked
examples because the two models missed different phrasings without them:
one declined "when did I first and last message", the other declined
"in group chats compared with one-to-one", and a question that mixed a
count with something the store cannot answer was declined whole. With the
examples and the rule that a mixed question still gets its count, a
twelve-question bench (eight counts, four content questions) planned
12 of 12. A planner is a model, so it is measured, not trusted: the
bench is a live test, `TestPlannerBench` in `tests/test_stats_live.py`,
thirteen questions with invented names and a floor of eleven. It runs
whenever a chat endpoint is set and skips in CI.

## Tell the model your name

Message sources label your own lines `me:`. That label binds the subject to
you. Asked who did a half ironman, a model answered "the user (me)" from a
source where the user was congratulating somebody else. Relabelling that one
line with a name, and changing nothing else, made the same model name the
right person.

```bash
RECALL_USER_LABEL=Ada
```

With that set, every `me:` line becomes `Ada:` in the prompt, and the system
prompt says who Ada is. Storage never changes: every embedding was computed
from the stored text, and rewriting the corpus would make every vector stale
for no retrieval gain. The relabel anchors to the start of a line, so "tell
me: what time" is untouched.

The prompt also tells the model, whether or not a name is set, that a speaker
often talks ABOUT somebody else and that another person's experience must
never be reported as yours. That instruction removed one misattribution in a
measured case and left another in place, so treat it as a reduction and not a
fix. See [lessons.md](lessons.md).

## Running a model beside the embedding server

If you run both on one machine, size them together. Weights on a
unified-memory machine come out of the same pool as everything else and do
not appear in a process's RSS, so `ps` will happily show 0 GB for a model
occupying tens of gigabytes. Two resident models can push a box into the OOM
killer, which kills a model server mid-request; the client then sees an HTTP
502 that looks exactly like bad input. See [lessons.md](lessons.md).
