# Core pipeline
Ingest, embed, retrieve, answer, serve, and log, with every failure mode named.

## entrances
- main: a recall command from the owner at the terminal
  handler: main in cli.py
  trust: owner
- route POST /ask: a question from an HTTP client, answered with its sources
  handler: Handler in serve.py
  trust: network
- route POST /ask/stream: a question from an HTTP client, answered as a stream of events
  handler: Handler in serve.py
  trust: network
- route GET /health: a liveness answer, for anyone
  handler: Handler in serve.py
  trust: network

## invariants
- no hits forbids an answer: The prompt for an empty retrieval tells the model not to answer from its own knowledge.
  over: build_prompt called with a question and no hits
  via: test_no_hits_forbids_an_answer
  because: a model asked to answer from no sources answers from itself, and nothing downstream can tell that answer from recall
  refuted: build_prompt drops the instruction not to answer from the model's own knowledge -> the test test_no_hits_forbids_an_answer went red (2026-10-22)
  kinds: none
- attribution demanded: The system prompt forbids the model to report another person's experience as the user's own.
  over: the system prompt built with no user label
  via: test_it_demands_attribution
  because: a model once named the user as the person who did a half ironman, from a source where the user congratulated somebody else
  refuted: the system prompt loses the rule against reporting another person's experience as the user's own -> the test test_it_demands_attribution went red (2026-10-22)
  kinds: none
- a restarted server loses nothing: A batch that fails once against a server that comes back healthy is retried whole and keeps every item.
  over: a batch of eight items whose first embed call answers 502 while the health check answers ready
  via: test_a_restarted_server_loses_nothing
  because: a restarted embedding server and an oversized request both answer 502, and a client that shrinks on every failure drops good data
  refuted: embed_safe drops the batch after the health check answers ready instead of retrying it -> the test test_a_restarted_server_loses_nothing went red (2026-10-22)
  kinds: message
  checklist: destination-confinement dismissed: the destination is the embedding url in the owner's configuration, and the request carries only archive text the owner already holds
  checklist: message-authenticity dismissed: the embedding answer is unsigned and comes from the owner's own server
  checklist: input-validation declared as a wrong dimension is a setup fault
  checklist: retry-recognition dismissed: an embedding call has no effect that can land, so a retry repeats a pure computation
  checklist: duplicate-suppression dismissed: an embedding call has no deduplication identity
  checklist: keyed-ordering dismissed: vectors are matched to inputs by the index the server returns, not by transport order
  checklist: acknowledgment-barrier dismissed: no acknowledgment retires work here, and the ingest commits a batch only after its vectors return
  checklist: retry-classification declared as a restarted server loses nothing
- a dead server stops the run: A failed batch against a server whose health check never answers ready raises EmbeddingServerDown and drops nothing.
  over: a batch of eight items whose embed call answers 502 while the health check answers not ready
  via: test_a_dead_server_stops_the_run_instead_of_dropping_everything
  because: a stopped run keeps every stored batch and the next run resumes, while a drain into the drop list loses the corpus
  refuted: embed_safe no longer raises EmbeddingServerDown when the health check fails -> the test test_a_dead_server_stops_the_run_instead_of_dropping_everything went red (2026-10-22)
  kinds: message
  checklist: destination-confinement dismissed: the destination is the embedding url in the owner's configuration, and the request carries only archive text the owner already holds
  checklist: message-authenticity dismissed: the embedding answer is unsigned and comes from the owner's own server
  checklist: input-validation declared as a wrong dimension is a setup fault
  checklist: retry-recognition dismissed: an embedding call has no effect that can land, so a retry repeats a pure computation
  checklist: duplicate-suppression dismissed: an embedding call has no deduplication identity
  checklist: keyed-ordering dismissed: vectors are matched to inputs by the index the server returns, not by transport order
  checklist: acknowledgment-barrier dismissed: no acknowledgment retires work here, and the ingest commits a batch only after its vectors return
  checklist: retry-classification declared as a dead server stops the run
- health before bisecting: A failed batch asks the health check.
  over: a batch of one item whose embed call answers 502, then the health check asked after the failed batch
  via: test_it_checks_health_before_bisecting
  because: only the health check tells a restarted server from an oversized request, and a split against a dead server blames good data
  refuted: embed_safe skips the health check before it splits -> the test test_it_checks_health_before_bisecting went red (2026-10-22)
  kinds: message
  checklist: destination-confinement dismissed: the destination is the embedding url in the owner's configuration, and the request carries only archive text the owner already holds
  checklist: message-authenticity dismissed: the embedding answer is unsigned and comes from the owner's own server
  checklist: input-validation declared as a wrong dimension is a setup fault
  checklist: retry-recognition dismissed: an embedding call has no effect that can land, so a retry repeats a pure computation
  checklist: duplicate-suppression dismissed: an embedding call has no deduplication identity
  checklist: keyed-ordering dismissed: vectors are matched to inputs by the index the server returns, not by transport order
  checklist: acknowledgment-barrier dismissed: no acknowledgment retires work here, and the ingest commits a batch only after its vectors return
  checklist: retry-classification declared as a restarted server loses nothing
- an oversized item is dropped alone: A healthy server that still refuses a batch has the oversized item split out and dropped while the rest are kept.
  over: a small, an oversized and a small item against a healthy server that refuses any text over 50 characters
  via: test_a_genuinely_oversized_item_is_isolated_and_dropped
  because: only a refusal from a healthy server means the input really is too large, and only then does a split make sense
  refuted: embed_safe keeps a single refused item instead of dropping it -> the test test_a_genuinely_oversized_item_is_isolated_and_dropped went red (2026-10-22)
  kinds: message
  checklist: destination-confinement dismissed: the destination is the embedding url in the owner's configuration, and the request carries only archive text the owner already holds
  checklist: message-authenticity dismissed: the embedding answer is unsigned and comes from the owner's own server
  checklist: input-validation declared as a wrong dimension is a setup fault
  checklist: retry-recognition dismissed: an embedding call has no effect that can land, so a retry repeats a pure computation
  checklist: duplicate-suppression dismissed: an embedding call has no deduplication identity
  checklist: keyed-ordering dismissed: vectors are matched to inputs by the index the server returns, not by transport order
  checklist: acknowledgment-barrier dismissed: no acknowledgment retires work here, and the ingest commits a batch only after its vectors return
  checklist: retry-classification declared as an oversized item is dropped alone
- a wrong dimension is a setup fault: An embedding whose length differs from RECALL_EMBED_DIMS raises EmbeddingMisconfigured.
  over: one input answered with a 768 dimension vector against the configured dimension
  via: test_a_wrong_dimension_is_a_setup_fault
  because: EmbeddingMisconfigured is not a RuntimeError, so embed_safe does not catch it, and a setup fault never reaches the split that would drop every chunk
  refuted: embed stops comparing the vector length with RECALL_EMBED_DIMS -> the test test_a_wrong_dimension_is_a_setup_fault went red (2026-10-22)
  kinds: message
  checklist: destination-confinement dismissed: the destination is the embedding url in the owner's configuration, and the request carries only archive text the owner already holds
  checklist: message-authenticity dismissed: the embedding answer is unsigned and comes from the owner's own server
  checklist: input-validation declared as a wrong dimension is a setup fault
  checklist: retry-recognition dismissed: an embedding call has no effect that can land, so a retry repeats a pure computation
  checklist: duplicate-suppression dismissed: an embedding call has no deduplication identity
  checklist: keyed-ordering dismissed: vectors are matched to inputs by the index the server returns, not by transport order
  checklist: acknowledgment-barrier dismissed: no acknowledgment retires work here, and the ingest commits a batch only after its vectors return
  checklist: retry-classification declared as a wrong dimension is a setup fault
- batches keep every item once: Batching yields every item exactly once and in its original order.
  over: 120 items of 50 characters batched under a 1000 character budget
  via: test_every_item_appears_exactly_once_and_in_order
  because: an item that batching loses or repeats is a chunk that is never stored or is embedded twice
  refuted: batches never yields the last batch -> the test test_every_item_appears_exactly_once_and_in_order went red (2026-10-22)
  kinds: none
- a re-ingest writes only what changed: A chunk whose stored digest equals the digest of its text is not pending.
  over: one chunk whose ref is stored with the digest of the same text
  via: test_an_unchanged_chunk_is_skipped
  because: without the skip every run embeds the whole corpus again, and a twelve hour ingest must resume
  refuted: changed marks a stored chunk pending whatever its digest -> the test test_an_unchanged_chunk_is_skipped went red (2026-10-22)
  kinds: encoding
  checklist: semantic-preservation dismissed: the digest is a comparison key and changes no stored text
  checklist: canonical-encoding dismissed: the digest compares text byte for byte
  checklist: key-rotation-compatibility dismissed: nothing is encrypted
- the second run writes nothing: A second ingest of a source whose chunks carried NUL writes no row.
  over: two runs of the ingest over one source whose ref and text carry NUL, against a store kept between the runs
  via: test_the_second_run_writes_nothing
  because: the digest and the stored text must agree after the clean, or the chunk is embedded again on every run
  refuted: run embeds and stores the raw item text instead of the cleaned text -> the test test_the_second_run_writes_nothing went red (2026-10-22)
  kinds: encoding
  checklist: semantic-preservation dismissed: the clean removes NUL on purpose, and NUL carries no meaning in text
  checklist: canonical-encoding declared as the second run writes nothing
  checklist: key-rotation-compatibility dismissed: nothing is encrypted
- a contact added later renames the stored chunk: A vCard added after the first ingest rewrites the stored text of the chunk it names.
  over: two runs of the ingest over one source, the second with a vCard that names the participant
  via: test_a_vcard_dropped_in_later_renames_the_stored_chunk
  because: the ref is stable by design, so a skip on the ref alone reads renamed text as already held
  refuted: changed skips every stored ref without a digest comparison -> the test test_a_vcard_dropped_in_later_renames_the_stored_chunk went red (2026-10-22)
  kinds: encoding
  checklist: semantic-preservation dismissed: the contact name replaces the raw identifier in the text on purpose, and participants keep the identifier
  checklist: canonical-encoding dismissed: the digest compares text byte for byte
  checklist: key-rotation-compatibility dismissed: nothing is encrypted
- filters are bound, never interpolated: The where clause binds the source filter as a parameter.
  over: a source filter that holds a single quote
  via: test_the_question_is_never_interpolated
  because: the source filter arrives in the HTTP body, and SQL built from it by concatenation would run what the client wrote
  crossing: owner -> store
  entrances: none
  refuted: where_clause writes the source filter into the SQL text instead of binding it -> the test test_the_question_is_never_interpolated went red (2026-10-22)
  kinds: read
  checklist: scoped-reads dismissed: one owner, and no scope beyond the optional source filter
  checklist: redaction dismissed: the bearer holder is the owner of the archive, and recall shows the owner their own archive
- a ref in both arms outranks a ref in one: Reciprocal rank fusion ranks a ref that both arms found above a ref that one arm found.
  over: a dense list a, b, c and a sparse list c, d
  via: test_a_ref_in_both_lists_beats_a_ref_in_one
  because: agreement between the dense arm and the sparse arm is what hybrid search means in practice
  refuted: rrf keeps the best single arm score instead of the sum -> the test test_a_ref_in_both_lists_beats_a_ref_in_one went red (2026-10-22)
  kinds: none
- a logger that raises never costs the answer: An ask returns its answer when the query log raises.
  over: one ask through handle_ask with a logger that raises
  via: test_a_logger_that_raises_never_costs_the_answer
  because: the query log is evidence, not a dependency, and a broken trace must never raise into the request path
  refuted: _log in serve.py lets a logger exception raise -> the test test_a_logger_that_raises_never_costs_the_answer went red (2026-10-22)
  kinds: none
- a log failure rolls back: A failed candidate write in the query log rolls the connection back.
  over: one log_query whose candidate insert fails
  via: test_a_failure_rolls_back_so_the_connection_stays_usable
  because: psycopg leaves a connection in an aborted transaction after an error, and every later statement fails until a rollback
  refuted: log_query stops rolling back after a failed candidate write -> the test test_a_failure_rolls_back_so_the_connection_stays_usable went red (2026-10-22)
  kinds: none
- a citation past the prompt is kept as invalid: A citation number above the source count is recorded as invalid, not dropped.
  over: an answer that cites 2 and 12 against 8 sources
  via: test_a_number_past_the_prompt_is_invalid_not_dropped
  because: a model that cites a source it was never given tells the log something worth keeping
  refuted: cited_numbers drops a number past the source count -> the test test_a_number_past_the_prompt_is_invalid_not_dropped went red (2026-10-22)
  kinds: none
- rows never carry NUL: Every text column of a chunk row is stripped of NUL, the ref included.
  over: a chunk whose ref, text, thread, path and participants carry NUL
  via: test_nul_is_stripped_from_every_text_column
  because: Postgres text rejects NUL, and one NUL in one email ended a whole run before the next source started
  crossing: archive -> store
  entrances: none
  refuted: clean returns the value with its NUL bytes -> the test test_nul_is_stripped_from_every_text_column went red (2026-10-22)
  kinds: encoding
  checklist: semantic-preservation dismissed: the clean removes NUL on purpose, and NUL carries no meaning in text
  checklist: canonical-encoding declared as the second run writes nothing
  checklist: key-rotation-compatibility dismissed: nothing is encrypted
- the chunk budget takes the worst measured ratio: Calibration sizes the chunk budget from the densest sample, not the average.
  over: two samples of 4.0 and 1.0 characters per token under a 1000 token ceiling
  via: test_it_takes_the_worst_ratio_not_the_average
  because: text runs from 1.4 to 4.4 characters per token by source, and a budget from the average lets the dense tail through to be refused
  refuted: measure_density takes the average ratio instead of the lowest -> the test test_it_takes_the_worst_ratio_not_the_average went red (2026-10-22)
  kinds: none
- a split loses nothing: A split of text with no paragraph break keeps every character.
  over: 5000 characters with no paragraph break split under a 700 character budget
  via: test_nothing_is_lost
  because: a single record of 188,218 characters once arrived, and a split that drops text loses it without a word
  refuted: split_to_budget skips one character at each cut -> the test test_nothing_is_lost went red (2026-10-22)
  kinds: encoding
  checklist: semantic-preservation declared as a split loses nothing
  checklist: canonical-encoding dismissed: a split makes parts, not an identity
  checklist: key-rotation-compatibility dismissed: nothing is encrypted
- answers only behind the bearer: The answer path is reached only through the HTTP handler that checks the bearer.
  protects: handle_ask
  chokepoint: Handler
  from: anywhere
  because: Handler.do_POST refuses a request whose bearer does not match the configured token before it calls handle_ask, and an unset token accepts nothing
  crossing: network -> owner
  kinds: credential
  checklist: capability-authorization declared as answers only behind the bearer
  checklist: revalidated-permission dismissed: every request checks the bearer again
  checklist: message-authenticity dismissed: the bearer is compared in constant time, and no signed message is accepted
  checklist: encrypted-storage dismissed: the token lives in the environment, and recall stores no credential
  checklist: key-rotation-compatibility dismissed: nothing encrypted is retained
  checklist: separation-of-duties dismissed: one owner holds the one token, and no approval policy exists
- streamed answers only behind the bearer: The streamed answer path is reached only through the HTTP handler that checks the bearer.
  protects: stream_ask
  chokepoint: Handler
  from: anywhere
  because: Handler.do_POST refuses a request whose bearer does not match the configured token before it starts the stream, and an unset token accepts nothing
  crossing: network -> owner
  kinds: credential
  checklist: capability-authorization declared as streamed answers only behind the bearer
  checklist: revalidated-permission dismissed: every request checks the bearer again
  checklist: message-authenticity dismissed: the bearer is compared in constant time, and no signed message is accepted
  checklist: encrypted-storage dismissed: the token lives in the environment, and recall stores no credential
  checklist: key-rotation-compatibility dismissed: nothing encrypted is retained
  checklist: separation-of-duties dismissed: one owner holds the one token, and no approval policy exists
- a whole decline is graded by the reference, not by the model: A reply that declines the whole question is graded from the eval author's decline marker, whatever the model graded.
  over: one reply graded yes by the model and read as a whole decline, against a reference marked as expecting no decline
  via: test_a_whole_decline_against_a_named_answer_is_no
  because: Eleven of forty labelled rows were whole declines graded correct because the answer identified a gap in the sources, and the rule as a prompt sentence changed none of them. The model reports whether the answer declined the whole question; the eval author states whether a decline is the correct answer, as a marker under the reference; code grades a whole decline from the two. A part decline keeps the model's grade.
  refuted: grade_decline returns its input unchanged -> the test test_a_whole_decline_against_a_named_answer_is_no went red (2026-10-22)
  kinds: none
- the judge labels only a row nobody labelled: The label of a row is the later of the human and the agent, the one that spoke when one did, and the judge's word only when neither spoke.
  over: six rows: a human verdict alone, an agent verdict alone, an agent after a human, a human after an agent, a judge word alone, and no label at all
  via: test_the_judge_labels_only_an_unlabelled_row
  because: the human's labels are the calibration the judge is measured against, and a judge that outranks a human or an agent grades itself
  kinds: none
- an agent label never touches a human or judge column: save_agent updates the four agent columns of one row and no other column.
  over: one agent label saved through save_agent with a verdict, a name and a note
  via: test_it_writes_only_the_agent_columns
  because: the judge is measured against the human and the agent against both, and a write to verdict or a judge column makes that comparison circular
  kinds: storage
  checklist: scoped-reads dismissed: save_agent reads nothing, and the archive has one owner
  checklist: encrypted-storage dismissed: a label is a word and a note in the owner's own Postgres, not a secret
  checklist: key-rotation-compatibility dismissed: nothing is encrypted
  checklist: input-validation dismissed: save_agent refuses a verdict outside good and bad, an empty name and an empty note, and tests cover each, but no invariant names it yet
  checklist: revision-preservation declared as an agent label never touches a human or judge column
  checklist: commit-ordered-effects dismissed: one UPDATE then one commit, and nothing follows the commit
  checklist: durable-dispatch-intent dismissed: the write dispatches nothing
  checklist: declared-target-coverage dismissed: the UPDATE names one row by id
  checklist: completion-evidence dismissed: the commit is the completion, and the test counts it
