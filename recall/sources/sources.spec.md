# Source adapters
Each adapter answers two questions for itself: is my data in here, and what chunks does it produce.

## works when
- base.py exists at this node
- passes test "TestDetection"
- passes test "TestRegistration"
- passes test "TestMboxRefsAreUnique"
- passes test "TestMboxTakesTextPartsOnly"
- passes test "TestMboxFilter"

## why
`recall ingest` needs no configuration because detection lives in the adapter,
not in a config file. Dropping a file under `data/` is the whole setup. Adding a
source means one small class that registers itself.

Refs are identity. Every chunk ref must be unique and stable across runs, or the
changed-only ingest reloads everything and the citations point at the wrong
document. A long message that splits carries a part suffix.

Mail is filtered on purpose. Promotions and noise labels are dropped, receipts
survive, binary parts never reach the body, and the Sent label marks a message
as the user's own.

Medical data stays a deliberate choice. The health adapter opens `export.xml`
only and the FHIR clinical records in the same export are never read.
