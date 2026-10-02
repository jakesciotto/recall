# Core pipeline
Ingest, embed, retrieve, answer, serve, and log, with every failure mode named.

## invariants
- no hits forbids an answer
- a restarted server loses nothing
- a dead server stops the run
- a re-ingest writes only what changed
- the question is never interpolated into SQL
- a ref in both arms outranks a ref in one
- a logger that raises never costs the answer
- rows never carry NUL
- the chunk budget takes the worst measured ratio

## works when
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

## refutations
- no hits forbids an answer: build_prompt guard set to `if False` -> RED, "FAILED tests/test_answer.py::TestBuildPrompt::test_no_hits_forbids_an_answer | 1 failed"
- a restarted server loses nothing: deleted the same-batch retry in embed_safe -> GREEN first (the test counted items, and bisection also keeps 8 of 8); test now asserts call sizes [8, 8] -> RED, "FAILED tests/test_embed.py::TestEmbedSafe::test_a_restarted_server_loses_nothing | 1 failed"
- a dead server stops the run: deleted the `if not ready(): raise EmbeddingServerDown` guard in embed_safe -> RED, "FAILED tests/test_embed.py::TestEmbedSafe::test_a_dead_server_stops_the_run_instead_of_dropping_everything | 1 failed" (test_it_checks_health_before_bisecting also red)
- a re-ingest writes only what changed: `changed` set to `if True:` so every chunk is pending -> RED, "FAILED tests/test_ingest.py::TestChanged::test_an_unchanged_chunk_is_skipped | 1 failed". Skipping by ref alone (`elif False:`) leaves the named test green, which is the other direction of the same rule; three siblings in TestChanged go red, test_a_rewritten_chunk_reloads first.
- the question is never interpolated into SQL: where_clause wrote `source = '{source}'` as an f-string with no bound parameter -> RED, "FAILED tests/test_retrieve.py::TestWhereClause::test_the_question_is_never_interpolated | 1 failed"
- a ref in both arms outranks a ref in one: rrf took `max` of the two scores instead of the sum -> RED, "FAILED tests/test_retrieve.py::TestRRF::test_a_ref_in_both_lists_beats_a_ref_in_one | 1 failed"
- a logger that raises never costs the answer: deleted the try/except around `querylog.log` in `_log`, the helper handle_ask calls -> RED, "FAILED tests/test_serve.py::TestHandleAsk::test_a_logger_that_raises_never_costs_the_answer | 1 failed"
- rows never carry NUL: `clean` returned its input unchanged -> RED, "FAILED tests/test_ingest.py::TestRowsNeverCarryNul::test_nul_is_stripped_from_every_text_column | 1 failed". Dropping `clean` from one column of `_row` (source, then date_confidence) stayed GREEN: the test asserted five named columns. It now puts a NUL in every string field and reads the whole row; both breaks -> RED, "1 failed".
- the chunk budget takes the worst measured ratio: measure_density returned the average instead of the minimum -> RED, "FAILED tests/test_chunking.py::TestCalibrate::test_it_takes_the_worst_ratio_not_the_average | 1 failed"

## why
"No hits forbids an answer" is the product. The prompt builder refuses to ask
the model anything when retrieval returned nothing, because a model asked to
answer from no sources answers from itself.

"A restarted server loses nothing" and "a dead server stops the run" are the two
halves of one lesson. A restarted embedding server and an oversized request both
answer HTTP 502. A client that shrinks on every failure bisects a batch down to
single items against a dead server and blames each one. The chokepoint checks
health first, waits out a restart, retries the same batch, and stops the run
when the server never comes back, instead of dropping the corpus one item at a
time.

"A re-ingest writes only what changed" makes a twelve hour ingest resumable. The
digest of each chunk's text and ref decides pending against stored, and a chunk
the source stopped producing is left alone.

"The question is never interpolated into SQL". The where clause binds dates and
source as parameters, and the question reaches Postgres only through
`plainto_tsquery` and a bound vector literal.

"A ref in both arms outranks a ref in one" is what hybrid means in practice.
Reciprocal rank fusion returns every ref from either arm, ties are
deterministic, and agreement between the arms wins.

"A logger that raises never costs the answer". The query log is evidence, not a
dependency. It opens its own connection, rolls back on failure, and a broken
trace never raises into the request path.

"Rows never carry NUL". Postgres text columns reject NUL, and one export carried
it. The cleaner strips every text column before the digest sees it, so the
digest and the stored row agree.

"The chunk budget takes the worst measured ratio". Text runs from 1.4 to 4.4
characters per token depending on the source. A budget derived from the average
silently rejects a third of the mail. Calibration measures the corpus and takes
the densest sample.
