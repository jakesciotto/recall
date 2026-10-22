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

"A restarted server loses nothing" and "a dead server stops the run" are the two halves of one lesson. A restarted embedding server and an oversized request both answer HTTP 502. A client that shrinks on every failure splits a batch down to single items against a dead server and blames each one. `embed_safe` checks health first, waits out a restart, retries the same batch, and stops the run when the server never comes back. It does not drop the corpus one item at a time.

"A re-ingest writes only what changed" makes a twelve hour ingest resumable. The digest of each chunk's text decides pending against stored, and a chunk the source stopped producing stays where it is.

"Filters are bound, never interpolated". The where clause binds dates and source as parameters. The question reaches Postgres only through `plainto_tsquery` and a bound vector literal. The invariant is tested for the source filter only. No test yet proves the same for the question or for the dates.

"A ref in both arms outranks a ref in one" is what hybrid means in practice. Reciprocal rank fusion returns every ref from either arm, ties are deterministic, and agreement between the arms wins.

"A logger that raises never costs the answer". The query log is evidence, not a dependency. It opens its own connection, rolls back on failure, and a broken trace never raises into the request path.

"Rows never carry NUL". Postgres text columns reject NUL, and one export carried it. The cleaner strips every text column before the digest sees it, so the digest and the stored row agree.

"The chunk budget takes the worst measured ratio". Text runs from 1.4 to 4.4 characters per token, by source. A budget from the average silently rejects a third of the mail. Calibration measures the corpus and takes the densest sample.

### Source adapters

`recall ingest` needs no configuration because detection lives in the adapter, not in a config file. To drop a file under `data/` is the whole setup. A new source is one small class that registers itself.

Refs are identity. Every chunk ref must be unique and stable across runs, or the changed-only ingest reloads everything and the citations point at the wrong document. A long message that splits carries a part suffix.

Mail is filtered on purpose. Promotions and noise labels are dropped, receipts stay, binary parts never reach the body, and the Sent label marks a message as the user's own.

The 0.37 spec checked that `recall/sources/base.py` exists. That check is retired, because a file-existence check proves no behavior.

The mbox filter test re-implements the keep predicate. It does not call the `_kept` filter, so a change in `_kept` can pass the test. The work order lists this gap.

Medical data stays a deliberate choice. The health adapter opens `export.xml` only, and it never reads the FHIR clinical records in the same export.
