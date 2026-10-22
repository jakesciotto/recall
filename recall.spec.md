# recall
Search your own archive on your own machine, and answer with citations or not at all.

## trust levels
- owner: the person who runs recall on their own machine, at the terminal
- network: an HTTP client that reaches the API, before its bearer verifies
- archive: the exported files under data/, whose content other people wrote
- store: the chunk rows and the query log in the local Postgres
- model: the local embedding server and the optional chat server

## invariants
- the test cut: Every test runs with the database and the network blocked.
  over: a database connect and an HTTP request to a local port, each made inside a test
  via: TestTheCutIsReal
  because: a green run on a fresh machine must prove that the tests are isolated, and a fixture that was never seen to fail cannot be trusted
  refuted: the conftest fixture stops replacing db.connect -> the test TestTheCutIsReal went red (2026-10-22)
  kinds: none
- every module imports: Every module in the package imports without error.
  over: every module that pkgutil walks under the recall package
  via: TestEveryModuleImports
  because: 120 tests passed while cli.py held a syntax error, because no test imported it
  refuted: a new module recall/unimported.py that no other file imports holds a syntax error -> the test TestEveryModuleImports went red (2026-10-22)
  kinds: none
