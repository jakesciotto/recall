"""The pipeline: detect, calibrate, chunk, embed, load, index.

The order matters and each step earns its place in docs/lessons.md. The
vector index is built last so the load does not pay index maintenance.
"""

import sys

from . import chunking, db, embed as embedding
from .sources import detect_all


def _row(chunk):
    d = chunk.as_dict() if hasattr(chunk, "as_dict") else dict(chunk)
    return (d["ref"], d["text"], d["source"], d.get("occurred_at"),
            d.get("date_confidence") or "low", d.get("participants") or [],
            d.get("thread"), d.get("path"), None)


def run(root, conn, log=print, batch_cap=64, reindex=True):
    """Ingest everything found under `root`. Returns a per-source summary."""
    db.apply_schema(conn)
    known = db.existing_refs(conn)
    log(f"{len(known):,} chunks already stored")

    found = detect_all(root)
    if not found:
        log(f"no recognised sources under {root}")
        return {}
    log("found: " + ", ".join(f"{a.name} ({p.name})" for a, p in found))

    summary = {}
    for adapter, path in found:
        budget = chunking.calibrate(adapter.samples(path))
        log(f"[{adapter.name}] budget {budget:,} chars per chunk")

        pending = [c for c in adapter.chunks(path, budget)
                   if (c.ref if hasattr(c, "ref") else c["ref"]) not in known]
        log(f"[{adapter.name}] {len(pending):,} new chunks")
        if not pending:
            summary[adapter.name] = {"new": 0, "dropped": 0}
            continue

        items = [{"text": c.text, "chunk": c} for c in pending]
        dropped = []
        loaded = 0

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
            if loaded % 2000 < len(rows):
                log(f"  {loaded:,} / {len(pending):,}")

        log(f"[{adapter.name}] loaded {loaded:,}, dropped {len(dropped):,}")
        summary[adapter.name] = {"new": loaded, "dropped": len(dropped)}

    if reindex and any(s["new"] for s in summary.values()):
        log("building the vector index (last, so the load did not pay for it)")
        db.build_vector_index(conn)
    return summary
