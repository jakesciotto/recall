"""The pipeline: detect, calibrate, chunk, embed, load, index.

The order matters and each step earns its place in docs/lessons.md. The
vector index is built last so the load does not pay index maintenance.
"""

import collections
import hashlib
import sys

from . import chunking, contacts as contacts_mod, db, embed as embedding
from .sources import detect_all

Work = collections.namedtuple("Work", "pending new updated")


def digest(text):
    """The text fingerprint, matching Postgres md5(text) on a UTF-8 database.

    A database in another encoding fails in the safe direction: the run
    re-embeds work it already holds rather than skipping work it does not.
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def clean(value):
    """Text Postgres can hold. A text column can never carry 0x00, and one
    NUL byte in one email chunk once raised DataError inside upsert and
    ended the whole run before the next source started. The strip lives
    here, at the one point every row passes through, so no adapter has to
    remember it."""
    if not isinstance(value, str):
        return value
    return value.replace("\x00", "")


def _ref_text(chunk):
    if hasattr(chunk, "ref"):
        return clean(chunk.ref), clean(chunk.text)
    return clean(chunk["ref"]), clean(chunk["text"])


def changed(chunks, stored):
    """Split generated chunks into the work this run still has to do.

    Skipping on ref alone is what makes a re-run cheap, and it is also how a
    contact map added after the first ingest reached nothing: the ref is
    stable by design, so renamed text read as already held. Comparing the
    digest keeps the cheap re-run and still catches a rewrite.

    `stored` maps ref to the digest of the stored text. A ref it holds that
    no source produces any more stays where it is, because deleting a row is
    a separate decision.
    """
    pending, new, updated = [], 0, 0
    for chunk in chunks:
        ref, text = _ref_text(chunk)
        prior = stored.get(ref)
        if prior is None:
            pending.append(chunk)
            new += 1
        elif prior != digest(text):
            pending.append(chunk)
            updated += 1
    return Work(pending, new, updated)


def _row(chunk):
    """Every string column cleaned, ref included. The first NUL came in a
    message body; the second came in a Message-ID header, which becomes
    the ref. A strip that names columns misses the next one."""
    d = chunk.as_dict() if hasattr(chunk, "as_dict") else dict(chunk)
    return (clean(d["ref"]), clean(d["text"]), clean(d["source"]),
            clean(d.get("occurred_at")), clean(d.get("date_confidence")) or "low",
            [clean(p) for p in (d.get("participants") or [])],
            clean(d.get("thread")), clean(d.get("path")), None)


def run(root, conn, log=print, batch_cap=64, reindex=True):
    """Ingest everything found under `root`. Returns a per-source summary."""
    db.apply_schema(conn)
    stored = db.stored_digests(conn)
    log(f"{len(stored):,} chunks already stored")

    # Loaded once and handed to every adapter. Drop a .vcf under the data
    # directory and names appear; leave it out and nothing changes.
    contacts = contacts_mod.load_all(root)
    if contacts:
        log(f"{len(contacts):,} contacts loaded; participants will show names")

    found = detect_all(root)
    if not found:
        log(f"no recognised sources under {root}")
        return {}
    log("found: " + ", ".join(f"{a.name} ({p.name})" for a, p in found))

    summary = {}
    for adapter, path in found:
        budget = chunking.calibrate(adapter.samples(path))
        log(f"[{adapter.name}] budget {budget:,} chars per chunk")

        work = changed(adapter.chunks(path, budget, contacts), stored)
        log(f"[{adapter.name}] {work.new:,} new, {work.updated:,} rewritten")
        if not work.pending:
            summary[adapter.name] = {"new": 0, "updated": 0, "dropped": 0}
            continue

        # The text is cleaned HERE, before it is embedded and before the
        # write path copies it over the row. Cleaning it inside _row alone
        # let three runs die on the same NUL byte: _row's text was
        # overwritten with this item's text a few lines below.
        items = [{"text": clean(c.text), "chunk": c} for c in work.pending]
        dropped = []
        loaded = fresh = 0

        def on_drop(item, err):
            dropped.append(item["chunk"].ref)
            log(f"  dropped {item['chunk'].ref}: {str(err)[:90]}")

        def on_wait(n):
            log(f"  embedding server restarted, retrying {n} chunks")

        for group in embedding.batches(items, budget, cap=batch_cap):
            kept, vecs = embedding.embed_safe(group, on_drop, on_wait)
            if not kept:
                continue
            rows = []
            for item, vec in zip(kept, vecs):
                row = list(_row(item["chunk"]))
                row[1] = item["text"]
                row[8] = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
                rows.append(tuple(row))
            db.upsert(conn, rows)
            conn.commit()
            loaded += len(rows)
            # Counted at write time, so a drop lands on the right number
            # rather than being spread across both.
            fresh += sum(1 for i in kept if i["chunk"].ref not in stored)
            if loaded % 2000 < len(rows):
                log(f"  {loaded:,} / {len(work.pending):,}")

        log(f"[{adapter.name}] loaded {loaded:,}, dropped {len(dropped):,}")
        summary[adapter.name] = {"new": fresh, "updated": loaded - fresh,
                                 "dropped": len(dropped)}

    if reindex and any(s["new"] or s["updated"] for s in summary.values()):
        log("building the vector index (last, so the load did not pay for it)")
        db.build_vector_index(conn)
    return summary
