# recall: map for agents

Read `CLAUDE.md` for the conventions, `docs/design.md` for the rationale by component and `docs/lessons.md` for the incidents behind each rule. The rules live in the coherence specs.

| Component | Folder | Spec |
| --- | --- | --- |
| recall | `.` | `recall.spec.md` |
| Core pipeline | `recall` | `recall/package.spec.md` |
| Source adapters | `recall/sources` | `recall/sources/sources.spec.md` |

`npx --no -- coherence spec --check` lists every invariant with its state. `npx --no -- coherence query status` lists the structural defects and the open requirements.
