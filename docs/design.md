# Design

The rules live in the coherence specs: `recall.spec.md` at the root, `recall/package.spec.md` for the pipeline and `recall/sources/sources.spec.md` for the adapters. This file keeps the reasoning behind them. `docs/lessons.md` holds the incidents that taught each rule.

## Rationale by component

### recall

Nothing leaves the box. Indexing, search, the query log and the judge all run against a local Postgres and a local embedding server. Prose answers are optional and go to the OpenAI-compatible endpoint the user already runs. No account, no upload, no API key and no bundled chat model.

An answer about your own life that cannot cite its source looks the same as an invented one. Every answer cites. An empty retrieval still reaches the model, and the prompt tells it to say the archive returned nothing.

Search is hybrid, always. Half of real questions are metadata questions in disguise, and vector search alone answers those badly. The dense arm and the sparse arm fuse by reciprocal rank.

Re-runs are cheap. Chunk identity is stable, so a second ingest writes only what changed, and a failed twelve hour run resumes instead of restarting.

A restarted model server is not bad data. Both answer HTTP 502. The client asks the health endpoint before it reacts, waits out the restart and retries the same batch. Only a refusal from a healthy server counts as an oversize signal.

The test suite blocks the database and the network for every test not marked live. A green run on a fresh machine proves the tests are isolated. That cut is itself tested.

`recall doctor` is the first command to run when anything looks wrong. It checks every dependency and names the command that fixes each one.

The 0.37 spec checked that `pyproject.toml` and `docker-compose.yml` exist at the root. Both checks are retired, because a file-existence check proves no behavior.

### Core pipeline

The 0.37 spec for this component held no file-existence check, so none is retired here.

"No hits forbids an answer" is the product. The prompt builder tells the model not to answer from its own knowledge when retrieval returned nothing, because a model asked to answer from no sources answers from itself.

"A whole decline is graded by the reference, not by the model". Eleven of forty labelled rows were whole declines graded correct because the answer identified a gap in the sources, and the rule as a prompt sentence changed none of them. The model reports whether the answer declined the whole question. The eval author states whether a decline is the correct answer, as a marker under the reference. Code grades a whole decline from the two. A part decline keeps the model's grade.

"A restarted server loses nothing" and "a dead server stops the run" are the two halves of one lesson. A restarted embedding server and an oversized request both answer HTTP 502. A client that shrinks on every failure splits a batch down to single items against a dead server and blames each one. `embed_safe` checks health first, waits out a restart, retries the same batch, and stops the run when the server never comes back. It does not drop the corpus one item at a time.

"A re-ingest writes only what changed" makes a twelve hour ingest resumable. The digest of each chunk's text decides pending against stored, and a chunk the source stopped producing stays where it is.

"Filters are bound, never interpolated". The where clause binds dates and source as parameters. The question reaches Postgres only through `plainto_tsquery` and a bound vector literal. The invariant is tested for the source filter only. No test yet proves the same for the question or for the dates.

"A ref in both arms outranks a ref in one" is what hybrid means in practice. Reciprocal rank fusion returns every ref from either arm, ties are deterministic, and agreement between the arms wins.

"A logger that raises never costs the answer". The query log is evidence, not a dependency. It opens its own connection, rolls back on failure, and a broken trace never raises into the request path.

"Rows never carry NUL". Postgres text columns reject NUL, and one export carried it. The cleaner strips every text column before the digest sees it, so the digest and the stored row agree.

"The chunk budget takes the worst measured ratio". Text runs from 1.4 to 4.4 characters per token, by source. A budget from the average silently rejects a third of the mail. Calibration measures the corpus and takes the densest sample.

"The judge labels only a row nobody labelled" and "an agent label never touches a human or judge column" are the two halves of one rule. Three labellers write three column sets. The label of a row is the later of the human and the agent, else the judge. The owner gave 55 labels and stopped. That set is the calibration the judge is measured against, and it survives only if nothing else writes `verdict`. An agent's ruling lives in its own columns with a required note, so the judge against the human and the agent against both stay separate counts.

"The judge reads a source whole unless the row overflows the budget". The answerer reads every source whole. The judge once read the first 1,200 characters of each, and on the real log that cut 78 of 588 sources across 38 of 73 judged rows; on every arbitrated row the cited fact sat past the cut, so the judge called present text absent. One budget per row, `RECALL_JUDGE_SOURCE_CHARS`, replaces the cap. `fit` water-fills it: a source that fits an equal share of what is left keeps every character, and only the longest sources lose their tail, marked as cut. At the default of 40,000 characters one row of 73 is cut and the densest prompt is 21,207 tokens on a 32k model. Rejudging all 73 rows moved agreement with the human labels from 39 to 43 of 53.

"A plan never reaches SQL as text" and "an invalid plan runs nothing" are the two halves of one rule for the structured store. A retriever cannot count, so `recall ingest` fills a store of one row per message, and `recall ask` lets a model fill a small form from a fixed catalogue. The model's answer is the one input on the answer path that nobody wrote, so it fails closed at `validate`, and every value it carries reaches Postgres as a bound parameter. The rendered table is a numbered source like any chunk, so the citation rule needs no exception.

"A planner that raises never costs the answer". The planner is a model call, and a model call fails. The table is an extra; the ask runs as it always did and the log records the error.

"The judge reads the table the answerer read". The ref of a stats source carries a digest of its rendered text, and the text is stored under that ref. A table that could change between the ask and the judgment is the clipped-source fault one layer up.

### Source adapters

`recall ingest` needs no configuration because detection lives in the adapter, not in a config file. To drop a file under `data/` is the whole setup. A new source is one small class that registers itself.

Refs are identity. Every chunk ref must be unique and stable across runs, or the changed-only ingest reloads everything and the citations point at the wrong document. A long message that splits carries a part suffix.

Mail is filtered on purpose. Promotions and noise labels are dropped, receipts stay, binary parts never reach the body, and the Sent label marks a message as the user's own.

The 0.37 spec checked that `recall/sources/base.py` exists. That check is retired, because a file-existence check proves no behavior.

The mbox filter test re-implements the keep predicate. It does not call the `_kept` filter, so a change in `_kept` can pass the test. The work order lists this gap.

"One event per message". A message joined to two chats is one row in the store, as it is one window in the index. The first chat by guid keeps it, so the choice does not move between runs.

Medical data stays a deliberate choice. The health adapter opens `export.xml` only, and it never reads the FHIR clinical records in the same export.
