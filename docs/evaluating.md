# Evaluating answers

Every question recall answers is logged: what retrieval offered, what the
model cited, and how long each stage took. That gives you the deterministic
numbers for free. Two more tools sit on top of the log, and they are built
to be compared against each other.

## The judge

```bash
RECALL_JUDGE_MODEL=some-other-model
recall judge --limit 50
recall judge --dry-run       # print verdicts, write nothing
```

A model reads each logged question, the sources the answer was given, and
the answer, and writes four estimates: was the answer grounded in what it
cited, did retrieval surface what the question needed, did the model hedge
while holding the evidence, and what kind of question was it. The sources
keep the numbers the answer cited, because a citation indexes the prompt,
and renumbering them makes every grounding judgment wrong.

**Use a different model from the one that answers.** A model grading its
own output shows self-preference bias. `RECALL_JUDGE_MODEL` defaults to the
answering model only because recall does not choose your models for you.

The judge writes only the `judge_*` columns. It cannot write `verdict` or
`note`, even by accident. Its columns carry no CHECK constraint: a model
populates them, a CHECK would fail a whole batch on one unexpected word,
and the code normalises to a known set and writes `unknown` instead. One
dead request becomes one `unknown` row, never a dead batch.

## The review

```bash
recall review
recall review --limit 5
recall review --redo         # revisit rows you already labelled
```

You read the question, the answer, and the sources, and press one key:
`g` good, `b` bad, `s` skip, `q` quit. Anything else is not an action, which
is what stops a stray Enter from labelling a row. Rows come newest first,
because recall decays: an old row invites a guess, and a guessed label is
worse than a missing one.

**The judge's opinion stays hidden until after your keypress.** Showing it
first anchors you to it, the two then agree more often than they should,
and the measurement quietly becomes worthless.

The review writes only `verdict` and `note`. It cannot write a `judge_*`
column. Neither side may overwrite the other, or comparing them is circular.

## Why both, and what the comparison showed

`judge_grounded` is a screen, not a verdict. The human `verdict` column is
the truth the judge is measured against. This is not caution for its own
sake. On the first attribution case measured, the comparison mattered, and
it cut both ways.

An answer said the user had raced, citing a message where the user wrote
"great first race man". Read in isolation that line parses either way: the
user narrating, or the user congratulating somebody else. The next turn,
the other person replying "thanks!", settles it. The answer also attributed
that person's bike crash to the user. The judge rated the answer grounded.
So did a second, larger model, in a near-identical sentence. Both had the
reply turn in the prompt and neither used it.

Then a neutral question, "who did the race", surfaced a source the first
check never looked at: the user had in fact completed such a race that
year, in a different month. So the top-line claim was true and miscited.
The crash was still wrong. The judge had been closer to right than the
person checking it, and the person had verified a claim against the one
source the answer cited rather than against the whole source set.

Three things follow.

1. **Verify a claim against the whole source set, never against the one
   source the answer happened to cite.** An answer can be true and
   miscited, and those two failures need separate names.
2. **Agreement between two models is not evidence when they share a blind
   spot.** The pragmatic inference across conversational turns, "who is
   this line addressed to", is exactly the kind of thing two models can
   both miss the same way.
3. **A prompt reduces this and does not fix it.** The attribution
   instruction in the system prompt removed the crash misattribution and
   left a misattributed quote in place. `RECALL_USER_LABEL` helps more,
   because the `me:` label itself binds the subject to the user; see
   [answering.md](answering.md). A structural fix, marking the addressee
   at chunk build time, is not attempted.

So run both. Label a few dozen rows by hand, then compare `verdict` against
`judge_grounded` before you trust the judge on anything.

```sql
SELECT judge_grounded, verdict, count(*)
FROM query_log
WHERE verdict IS NOT NULL AND judged_at IS NOT NULL
GROUP BY 1, 2 ORDER BY 1, 2;
```

## The question the log exists to answer

```sql
-- Of what retrieval offered, what did the model cite?
SELECT final_rank, count(*) AS offered,
       count(*) FILTER (WHERE cited) AS cited
FROM query_candidate
WHERE final_rank IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

If the model never cites past rank 3, then `k=8` spends context for nothing.
If it often cites rank 7 and 8, `k` is too small. Nothing else in the system
can tell you that.
