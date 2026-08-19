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

## Running a model beside the embedding server

If you run both on one machine, size them together. Weights on a
unified-memory machine come out of the same pool as everything else and do
not appear in a process's RSS, so `ps` will happily show 0 GB for a model
occupying tens of gigabytes. Two resident models can push a box into the OOM
killer, which kills a model server mid-request; the client then sees an HTTP
502 that looks exactly like bad input. See [lessons.md](lessons.md).
