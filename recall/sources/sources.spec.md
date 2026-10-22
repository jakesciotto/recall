# Source adapters
Each adapter answers two questions for itself: is my data in here, and what chunks does it produce.

## invariants
- activity detection: The activity adapter finds every product file of a Takeout export and finds nothing when the export is absent.
  over: a tree with Search, Chrome and YouTube watch history files, and a tree with only a loose note
  via: TestDetection
  because: recall ingest needs no configuration because detection lives in the adapter, so a file dropped under data/ is the whole setup
  refuted: the activity adapter detects only the first product file -> the test TestDetection went red (2026-10-22)
  kinds: none
- activity before the catch-all: The activity adapter is registered before the documents adapter.
  over: the order of the ADAPTERS list
  via: TestRegistration
  because: the documents adapter is the catch-all for loose files, and a specific adapter must claim its own export first
  refuted: ADAPTERS registers the activity adapter after the documents adapter -> the test TestRegistration went red (2026-10-22)
  kinds: none
- mbox refs are unique: Every chunk that the mbox adapter produces carries a distinct ref, and an unsplit thread keeps its bare ref.
  over: one long message split under a 3000 budget, a short thread, a long thread of short messages, and threads of three messages swept across six word sizes
  via: TestMboxRefsAreUnique
  because: refs are identity, 729 colliding refs once cost 1,387 rows, and a part suffix on an unsplit chunk changes every ref already stored
  refuted: the mbox adapter drops the part suffix from split threads -> the test TestMboxRefsAreUnique went red (2026-10-22)
  kinds: identity
  checklist: capability-authorization dismissed: a ref authorizes nothing
  checklist: canonical-encoding dismissed: a ref is built from the thread id and a part number, with no normalization
  checklist: identity-continuity dismissed: a ref is not an identity chain, and the stable-ref case is covered by the bare-ref test
- mbox takes text parts only: The mbox body holds text parts only, and images, binary parts and declared attachments never reach it.
  over: a plain part with an inline image, a pdf with no disposition, an html part alone, an html part with an inline image, and a plain part with a text attachment
  via: TestMboxTakesTextPartsOnly
  because: inline images decoded as text put NUL bytes in a quarter of one mailbox's chunks, and three ingests died on the same error
  refuted: body_text decodes every non text part into the body -> the test TestMboxTakesTextPartsOnly went red (2026-10-22)
  kinds: message
  checklist: destination-confinement dismissed: the adapter sends nothing anywhere
  checklist: message-authenticity dismissed: a message in an export is read as the owner's record, and no signature is checked
  checklist: input-validation declared as mbox takes text parts only
  checklist: retry-recognition dismissed: a parse has no effect that can land
  checklist: duplicate-suppression dismissed: the body parse does not deduplicate, and the ref identifies a thread
  checklist: keyed-ordering dismissed: no transport carries the parts
  checklist: acknowledgment-barrier dismissed: nothing is acknowledged
  checklist: retry-classification dismissed: a parse is never retried
- mbox keeps by allowlist: The keep allowlist holds the personal and purchase labels and never the noise labels.
  over: KEEP_LABELS checked against personal mail, a pure promotion, a receipt tagged both Purchases and Updates, and the Updates and Inbox labels
  via: TestMboxFilter
  because: a receipt carries both Category Purchases and Category Updates, so a denylist on Updates throws real receipts away
  refuted: KEEP_LABELS loses Category Purchases -> the test TestMboxFilter went red (2026-10-22)
  kinds: read
  checklist: scoped-reads dismissed: one owner, and no scope beyond the optional source filter
  checklist: redaction dismissed: a kept message is stored whole for its owner
