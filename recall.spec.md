# recall
Search your own archive on your own machine, and answer with citations or not at all.

## works when
- pyproject.toml exists at root
- docker-compose.yml exists at root
- passes test "TestTheCutIsReal"
- passes test "TestEveryModuleImports"

## why
Nothing leaves the box. Indexing, search, the query log, and the judge all run
against a local Postgres and a local embedding server. Prose answers are
optional and go to whatever OpenAI-compatible endpoint the user already runs.
No account, no upload, no API key, and no bundled chat model.

An answer about your own life that cannot cite its source is indistinguishable
from an invented one. Every answer cites, and an empty retrieval is forbidden
from answering at all.

Search is hybrid, always. Half of real questions are metadata questions in
disguise, and vector search alone answers those badly. The dense arm and the
sparse arm fuse by reciprocal rank.

Re-runs are cheap. Chunk identity is stable, so a second ingest writes only what
changed and a failed twelve hour run resumes instead of restarting.

A restarted model server is not bad data. Both answer HTTP 502. The client asks
the health endpoint before it reacts, waits out the restart, and retries the
same batch. Only a healthy server's refusal counts as an oversize signal.

The test suite blocks the database and the network for every test. A green run
on a fresh machine proves the tests are isolated. That cut is itself tested.

`recall doctor` is the first command to run when anything looks wrong. It
checks every dependency and names the command that fixes each one.
