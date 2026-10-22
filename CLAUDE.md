# CLAUDE.md

Context for agents working in this repository. The coherence specs hold the rules. Read `recall.spec.md` and `docs/design.md` before you change code.

## Conventions

1. Run the tests with `.venv/bin/python -m pytest`. The suite blocks the database and the network for every test not marked live.
2. The coherence specs are the source of the rules. Run `npx --no -- coherence spec --check` and `npx --no -- coherence run` before you offer a branch.
3. Write prose in the STE100 style: short, active sentences. No em dashes, no emoji.
4. Run npm with Node 22 first on PATH. Use npm 11 to change the lockfile.

## Coherence

The project runs `@posthog/coherence` 1.x. The grammar is in `node_modules/@posthog/coherence/docs/spec.md` and the config keys are in `docs/config.md` beside it. Coherence reads Python through Pyright, which the package installs as an optional dependency.

1. Every spec bullet is one invariant. A totality oracle names a pytest class or function in `via:`. A chokepoint names the protected symbol in `protects:` and the one symbol every reference passes through in `chokepoint:`.
2. Append a whole bullet after another bullet's last line. A bullet inserted between a bullet and its `refuted:`, `kinds:` or `checklist:` lines takes them.
3. Witness a totality oracle with `npx --no -- coherence refute <folder>/<name> --broke "<what you changed>"` while the break is staged in the source. Then restore the code and run `npx --no -- coherence run`. A restore that leaves the file the same size within the same second can leave a stale `.pyc`, so touch the file after you restore it.
4. Record a choice with `npx --no -- coherence decide "<chose>" --over "<rejected>" --because "<why>" --session <id> --agent <name>`.
5. The rationale that the 0.37 specs carried in prose lives in `docs/design.md` under Rationale by component.
