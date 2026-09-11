# Evaluating answers

Every question recall answers is logged: what retrieval offered, what the
model cited, and how long each stage took. That gives you the deterministic
numbers for free. Two more tools sit on top of the log, and they are built
to be compared against each other.

## Filling the log in one sitting

```bash
recall eval ~/my-questions.md
```

Numbered lines are questions. Headings, prose and bullets are not, so the
file can carry notes about what each group tests: exact facts, current
truth against superseded, multi-hop, and questions the archive must decline.
Every question is logged with `client = eval`, which keeps a batch separable
from what you ask by hand. Keep the file wherever your private notes live;
it is your data, not recall's.

An `expect:` line under a question is your reference: the answer, or the
source it should come from.

```markdown
7. Whom did I text most in 2019, and in 2021? Did the top contact change?
   expect: messages; the same person both years, so the answer is no
```

It travels with the question into the log as `expected`. The review shows
it before your keypress, and the judge grades the answer against it. Write
one for every question whose answer is a count over the archive. You cannot
label "whom did I text most" from memory, and a `GROUP BY` over the index
can tell you before you ask.

## The judge

```bash
RECALL_JUDGE_MODEL=some-other-model
recall judge --limit 50
recall judge --dry-run       # print verdicts, write nothing
```

A model reads each logged question, the sources the answer was given, and
the answer, and writes four estimates: was the answer grounded in what it
cited, did retrieval surface what the question needed, did the model hedge
while holding the evidence, and what kind of question was it. When the
question carries a reference, it writes a fifth, `judge_correct`: does the
answer agree with the reference. A row without a reference gets NULL there,
not `unknown`. NULL says there was nothing to grade against; `unknown` says
the model could not tell. The sources
keep the numbers the answer cited, because a citation indexes the prompt,
and renumbering them makes every grounding judgment wrong.

**Use a different model from the one that answers.** A model grading its
own output shows self-preference bias. `RECALL_JUDGE_MODEL` defaults to the
answering model only because recall does not choose your models for you.

**One rule runs before the model.** An answer that cites nothing and does
not decline is not grounded, and no model is asked: a claim with no
citation cannot trace to a source, whatever a model says. A decline is the
correct uncited answer, so it still goes to the model, which judges whether
the sources really held nothing. A rule verdict writes `rule` in
`judge_model`, so a query that measures one model never counts a row that
model did not see. On the first labelled batch of 20, every uncited answer
was a decline, so the rule fired on none of them.

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

**The reference shows before your keypress, on purpose.** It is not an
opinion about the answer. It is what you wrote down as the answer, or its
source, when you wrote the question. Hidden, a question like "whom did I
text most in 2021" asks you to recount the archive from memory, and a
label made that way is a guess with a name.

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

**Compare like with like.** On the first labelled batch of 20, the judge
agreed with the human on 10, and 6 of the 7 false passes were declines.
On a decline, `judge_grounded` is `yes` by definition, because a decline
makes no claim. The human `verdict` grades the system instead: a decline
over sources that held the answer is a hedge, and a decline over sources
that did not is a retrieval failure. Neither is a grounding failure. So
split the comparison. Cited answers measure the judge's grounding call.
Declines measure `judge_hedged` and `judge_retrieval`, and the judge was
inconsistent there: on one decline it wrote retrieval `yes` and hedged
`no`, which its own definition of hedged forbids.

Where a reference exists, compare `judge_correct` against `verdict` instead.
A decline against a reference that names an answer should be `no`, because
the answer misses what the reference names. The model did not apply that
on its own: on the first run with references, 11 of 40 labelled rows were
declines graded `correct=yes` because the answer "correctly identifies"
a gap in the sources. So the rule text now says it: grade the claim
against the reference, not the sources, and a decline is `no` unless the
reference itself says nothing exists. Measured again with that sentence:
the same 23 of 40 and the same 17 rows. The judge model ignored it. A code
rule was simulated instead, decline means `no` unless the reference starts
with `nothing`, and it was not adopted: it fixes the 11 and breaks 9,
because a decline phrase also appears inside partial answers that give
the fact, and a reference written as "no such email" does not start with
`nothing`. The same run also showed the other
direction: on five rows the judge read the reference and caught an error
the human label had missed, such as the answer naming the user as the
sender who filled their own inbox. A label made without the reference is
not the truth either.

```sql
-- Rows with a reference: the judge's correctness call against the human.
SELECT judge_correct, verdict, count(*)
FROM query_log
WHERE expected IS NOT NULL AND verdict IS NOT NULL AND judged_at IS NOT NULL
GROUP BY 1, 2 ORDER BY 1, 2;
```

```sql
-- Cited answers only: the judge's grounding call against the human verdict.
SELECT judge_grounded, verdict, count(*)
FROM query_log q
WHERE verdict IS NOT NULL AND judged_at IS NOT NULL
  AND EXISTS (SELECT 1 FROM query_candidate c
              WHERE c.query_id = q.id AND c.cited)
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
