# recall — map for agents

> Generated from the spec tree by the coherence harness. Do not edit by hand.

Search your own archive on your own machine, and answer with citations or not at all.

## Components

### recall  `.`
Search your own archive on your own machine, and answer with citations or not at all.

_why:_ Nothing leaves the box. Indexing, search, the query log, and the judge all run against a local Postgres and a local embedding server. Prose answers are optional and go to whatever OpenAI-compatible endpoint the user already runs. No account, no upload, no API key, and no bundled chat model. An answer about your own life that cannot cite its source is indistinguishable from an invented one. Every answer cites, and an empty retrieval is forbidden from answering at all. Search is hybrid, always. Half of real questions are metadata questions in disguise, and vector search alone answers those badly. The dense arm and the sparse arm fuse by reciprocal rank. Re-runs are cheap. Chunk identity is stable, so a second ingest writes only what changed and a failed twelve hour run resumes instead of restarting. A restarted model server is not bad data. Both answer HTTP 502. The client asks the health endpoint before it reacts, waits out the restart, and retries the same batch. Only a healthy server's refusal counts as an oversize signal. The test suite blocks the database and the network for every test. A green run on a fresh machine proves the tests are isolated. That cut is itself tested. `recall doctor` is the first command to run when anything looks wrong. It checks every dependency and names the command that fixes each one.

_works when:_
- pyproject.toml exists at root
- docker-compose.yml exists at root
- passes test "TestTheCutIsReal"
- passes test "TestEveryModuleImports"

_files:_ `conftest.py`, `test_activity.py`, `test_answer.py`, `test_attachments.py`, `test_captions.py`, `test_chunking.py`, `test_cli.py`, `test_config.py`, `test_contacts.py`, `test_doctor.py`, `test_embed.py`, `test_eval.py`, `test_files.py`, `test_ical.py`, `test_imagery.py`, `test_ingest.py`, `test_isolation.py`, `test_judge.py`, `test_querylog.py`, `test_render.py`, `test_retrieve.py`, `test_review.py`, `test_serve.py`, `test_sources.py`, `test_trends.py`, `test_twitter_media.py`, `test_vision.py`

### Core pipeline  `recall`
Ingest, embed, retrieve, answer, serve, and log, with every failure mode named.

_why:_ "No hits forbids an answer" is the product. The prompt builder refuses to ask the model anything when retrieval returned nothing, because a model asked to answer from no sources answers from itself. "A restarted server loses nothing" and "a dead server stops the run" are the two halves of one lesson. A restarted embedding server and an oversized request both answer HTTP 502. A client that shrinks on every failure bisects a batch down to single items against a dead server and blames each one. The chokepoint checks health first, waits out a restart, retries the same batch, and stops the run when the server never comes back, instead of dropping the corpus one item at a time. "A re-ingest writes only what changed" makes a twelve hour ingest resumable. The digest of each chunk's text and ref decides pending against stored, and a chunk the source stopped producing is left alone. "The question is never interpolated into SQL". The where clause binds dates and source as parameters, and the question reaches Postgres only through `plainto_tsquery` and a bound vector literal. "A ref in both arms outranks a ref in one" is what hybrid means in practice. Reciprocal rank fusion returns every ref from either arm, ties are deterministic, and agreement between the arms wins. "A logger that raises never costs the answer". The query log is evidence, not a dependency. It opens its own connection, rolls back on failure, and a broken trace never raises into the request path. "Rows never carry NUL". Postgres text columns reject NUL, and one export carried it. The cleaner strips every text column before the digest sees it, so the digest and the stored row agree. "The chunk budget takes the worst measured ratio". Text runs from 1.4 to 4.4 characters per token depending on the source. A budget derived from the average silently rejects a third of the mail. Calibration measures the corpus and takes the densest sample.

_works when:_
- boundary "no hits forbids an answer" at build_prompt via test "test_no_hits_forbids_an_answer"
- boundary "a restarted server loses nothing" at embed_safe via test "test_a_restarted_server_loses_nothing"
- boundary "a dead server stops the run" at embed_safe via test "test_a_dead_server_stops_the_run_instead_of_dropping_everything"
- boundary "a re-ingest writes only what changed" at changed via test "test_an_unchanged_chunk_is_skipped"
- boundary "the question is never interpolated into SQL" at where_clause via test "test_the_question_is_never_interpolated"
- boundary "a ref in both arms outranks a ref in one" at rrf via test "test_a_ref_in_both_lists_beats_a_ref_in_one"
- boundary "a logger that raises never costs the answer" at handle_ask via test "test_a_logger_that_raises_never_costs_the_answer"
- boundary "rows never carry NUL" at clean via test "test_nul_is_stripped_from_every_text_column"
- boundary "the chunk budget takes the worst measured ratio" at calibrate via test "test_it_takes_the_worst_ratio_not_the_average"
- passes test "test_it_checks_health_before_bisecting"
- passes test "test_a_genuinely_oversized_item_is_isolated_and_dropped"
- passes test "test_a_wrong_dimension_is_a_setup_fault"
- passes test "test_every_item_appears_exactly_once_and_in_order"
- passes test "test_it_demands_attribution"
- passes test "test_a_number_past_the_prompt_is_invalid_not_dropped"
- passes test "test_a_failure_rolls_back_so_the_connection_stays_usable"
- passes test "test_the_second_run_writes_nothing"
- passes test "test_a_vcard_dropped_in_later_renames_the_stored_chunk"
- passes test "test_nothing_is_lost"

_files:_ `__init__.py`, `answer.py`, `captions.py`, `chunking.py`, `cli.py`, `config.py`, `contacts.py`, `db.py`, `embed.py`, `evalrun.py`, `imagery.py`, `ingest.py`, `judge.py`, `naming.py`, `querylog.py`, `render.py`, `retrieve.py`, `review.py`, `serve.py`, `trends.py`, `vision.py`

### Source adapters  `recall/sources`
Each adapter answers two questions for itself: is my data in here, and what chunks does it produce.

_why:_ `recall ingest` needs no configuration because detection lives in the adapter, not in a config file. Dropping a file under `data/` is the whole setup. Adding a source means one small class that registers itself. Refs are identity. Every chunk ref must be unique and stable across runs, or the changed-only ingest reloads everything and the citations point at the wrong document. A long message that splits carries a part suffix. Mail is filtered on purpose. Promotions and noise labels are dropped, receipts survive, binary parts never reach the body, and the Sent label marks a message as the user's own. Medical data stays a deliberate choice. The health adapter opens `export.xml` only and the FHIR clinical records in the same export are never read.

_works when:_
- base.py exists at this node
- passes test "TestDetection"
- passes test "TestRegistration"
- passes test "TestMboxRefsAreUnique"
- passes test "TestMboxTakesTextPartsOnly"
- passes test "TestMboxFilter"

_files:_ `__init__.py`, `activity.py`, `base.py`, `files.py`, `health.py`, `ical.py`, `imessage.py`, `mbox.py`, `spotify.py`, `twitter.py`

## Structure

```
recall/
├─ recall/  ●
│  ├─ sources/  ●
│  │  ├─ __init__.py
│  │  ├─ activity.py
│  │  ├─ base.py
│  │  ├─ files.py
│  │  ├─ health.py
│  │  ├─ ical.py
│  │  ├─ imessage.py
│  │  ├─ mbox.py
│  │  ├─ spotify.py
│  │  └─ twitter.py
│  ├─ __init__.py
│  ├─ answer.py
│  ├─ captions.py
│  ├─ chunking.py
│  ├─ cli.py
│  ├─ config.py
│  ├─ contacts.py
│  ├─ db.py
│  ├─ embed.py
│  ├─ evalrun.py
│  ├─ imagery.py
│  ├─ ingest.py
│  ├─ judge.py
│  ├─ naming.py
│  ├─ querylog.py
│  ├─ render.py
│  ├─ retrieve.py
│  ├─ review.py
│  ├─ serve.py
│  ├─ trends.py
│  └─ vision.py
└─ tests/
   ├─ conftest.py
   ├─ test_activity.py
   ├─ test_answer.py
   ├─ test_attachments.py
   ├─ test_captions.py
   ├─ test_chunking.py
   ├─ test_cli.py
   ├─ test_config.py
   ├─ test_contacts.py
   ├─ test_doctor.py
   ├─ test_embed.py
   ├─ test_eval.py
   ├─ test_files.py
   ├─ test_ical.py
   ├─ test_imagery.py
   ├─ test_ingest.py
   ├─ test_isolation.py
   ├─ test_judge.py
   ├─ test_querylog.py
   ├─ test_render.py
   ├─ test_retrieve.py
   ├─ test_review.py
   ├─ test_serve.py
   ├─ test_sources.py
   ├─ test_trends.py
   ├─ test_twitter_media.py
   └─ test_vision.py
```

