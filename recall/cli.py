"""recall: ask questions about your own archive, entirely on your machine.

  recall doctor            check the setup and say what is missing
  recall ingest            find sources under ./data and index them
  recall ask "question"    answer from the archive, with citations
  recall serve             HTTP API for a browser or another app
"""

import argparse
import datetime as dt
import json
import pathlib
import sys
import urllib.request

from . import config


def _ok(label, detail=""):
    print(f"  ok    {label}{'  ' + detail if detail else ''}")


def _bad(label, detail=""):
    print(f"  FAIL  {label}{'  ' + detail if detail else ''}")


def cmd_doctor(args):
    """Check every dependency and say exactly what to do about each one.

    This exists so a first run fails with an instruction rather than a stack
    trace three minutes into an ingest.
    """
    print("recall doctor")
    problems = 0

    have_driver = True
    try:
        import psycopg  # noqa: F401
        _ok("psycopg installed")
    except ImportError:
        _bad("psycopg missing", "pip install 'psycopg[binary]'")
        have_driver = False
        problems += 1

    # A system binary, not a pip package, so pip install cannot bring it.
    # Without it every PDF silently indexes as nothing.
    import shutil
    if shutil.which("pdftotext"):
        _ok("pdftotext installed", "PDFs will be read")
    else:
        _bad("pdftotext missing", "PDFs are skipped. Install poppler: "
             "apt/dnf poppler-utils, brew poppler")
        problems += 1

    from . import db
    # Without the driver the database check can only repeat the same fault.
    if not have_driver:
        print("  skip  postgres check (needs psycopg)")
    else:
      try:
          with db.connect() as conn:
              db.apply_schema(conn)
              rows = db.counts(conn)
              logged = db.query_log_count(conn)
          total = sum(n for _, n in rows)
          _ok("postgres reachable", f"{total:,} chunks stored")
          for source, n in rows:
              print(f"          {source:14s} {n:,}")
          # Read the count, never the table's existence. A log that exists
          # and records nothing looks exactly like a working one.
          from . import querylog
          if querylog.enabled():
              _ok("query log on", f"{logged:,} questions recorded")
          else:
              print("  off   query log (RECALL_QUERY_LOG=0)")
      except Exception as e:
          _bad("postgres unreachable", f"{type(e).__name__}: {str(e)[:90]}")
          print("          try: docker compose up -d")
          problems += 1

    # Embed one string rather than reach the port. A 200 carrying the wrong
    # vector length is the fault that drains a corpus, and it answers a bare
    # reachability check perfectly.
    from . import embed as embedding
    try:
        vec = embedding.embed(["ping"], timeout=60)[0]
        _ok("embedding server", f"{config.EMBED_URL}  {len(vec)} dims")
    except embedding.EmbeddingMisconfigured as e:
        _bad("embedding server", str(e))
        problems += 1
    except Exception as e:
        _bad("embedding server", f"{config.EMBED_URL}  {type(e).__name__}")
        print("          try: docker compose up -d")
        problems += 1

    # Generation is optional. Indexing and search never use it, so a missing
    # endpoint is a note, not a fault.
    if not config.CHAT_URL:
        print("  note  no generation endpoint set; `recall ask` will return "
              "sources only")
        print("          set RECALL_CHAT_URL to write prose answers, "
              "see docs/answering.md")
    else:
        try:
            req = urllib.request.Request(
                config.CHAT_URL,
                json.dumps({"model": config.CHAT_MODEL, "max_tokens": 1,
                            "messages": [{"role": "user",
                                          "content": "hi"}]}).encode(),
                {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60):
                _ok("generation endpoint", config.CHAT_URL)
        except Exception as e:
            _bad("generation endpoint",
                 f"{config.CHAT_URL}  {type(e).__name__}")
            problems += 1

    from . import chunking
    ratio = chunking.measure_density(["the quick brown fox " * 40])
    if ratio:
        _ok("tokenizer", f"{ratio:.2f} chars/token on a sample")
    else:
        print("  warn  no tokenizer endpoint; budgets fall back to a "
              "pessimistic ratio")

    data = pathlib.Path(args.data or config.DATA_DIR)
    if data.is_dir():
        from . import contacts as contacts_mod
        from .sources import detect_all
        found = detect_all(data)
        if found:
            _ok("sources found", ", ".join(a.name for a, _ in found))
        else:
            print(f"  warn  no recognised sources under {data}")
            print("          drop an export in and re-run; see docs/sources.md")
        names = contacts_mod.load_all(data)
        if names:
            _ok("contacts", f"{len(names):,} names; participants will show them")
        else:
            print("  note  no .vcf found; participants stay as phone numbers")
            print("          export your address book as vCard into the data "
                  "directory")
    else:
        _bad("data directory missing", str(data))
        problems += 1

    if problems:
        print(f"\n{problems} problem(s) above. Fix those and run doctor again.")
        return 1
    print("\nready. next:  recall ingest")
    return 0


def cmd_ingest(args):
    from . import db, ingest
    root = pathlib.Path(args.data or config.DATA_DIR)
    with db.connect() as conn:
        summary = ingest.run(root, conn, reindex=not args.no_index)
    print(json.dumps(summary, indent=2))
    return 0


def _embedder():
    from . import embed
    return lambda text: embed.embed([text])[0]


def cmd_ask(args):
    import time
    from . import answer, db, querylog, retrieve
    started = time.monotonic()
    question = " ".join(args.question)
    with db.connect() as conn:
        hits, dates, trace = retrieve.search_traced(
            question, conn, _embedder(), k=args.k, source=args.source,
            today=dt.date.today())
    if dates.phrase:
        print(f"date filter: {dates.phrase} -> {dates.since} .. {dates.until}",
              file=sys.stderr)
    for i, h in enumerate(hits, start=1):
        print(f"[{i}] {answer.cite(h)}", file=sys.stderr)

    prompt = answer.build_prompt(question, hits)
    text = None
    meta = {}
    first_token_ms = None
    generate = not args.sources_only and config.CHAT_URL
    if generate:
        print()
        if args.no_stream:
            text = answer.chat(prompt, meta=meta)
            print(text)
        else:
            parts = []
            for piece in answer.chat_stream(prompt):
                if first_token_ms is None:
                    first_token_ms = int((time.monotonic() - started) * 1000)
                parts.append(piece)
                sys.stdout.write(piece)
                sys.stdout.flush()
            print()
            text = "".join(parts)
    elif not args.sources_only:
        print("no generation endpoint set; showing sources only. "
              "See docs/answering.md.", file=sys.stderr)

    # Written whether or not a model ran: every retrieval decision happened.
    # Guarded here as well as inside, so no logging change can ever cost the
    # user an answer.
    try:
        querylog.log(client="cli", question=question, k=args.k,
                     pool=retrieve.POOL, source=args.source, dates=dates,
                     trace=trace, hits=hits, answer=text, meta=meta,
                     model_requested=config.CHAT_MODEL,
                     prompt_chars=len(prompt),
                     streamed=bool(generate and not args.no_stream),
                     first_token_ms=first_token_ms,
                     total_ms=int((time.monotonic() - started) * 1000))
    except Exception:
        pass
    return 0 if hits else 1


def cmd_judge(args):
    from . import db, judge
    with db.connect() as conn:
        db.apply_schema(conn)
        judge.run(conn, limit=args.limit, redo=args.redo, dry_run=args.dry_run)
    return 0


def cmd_review(args):
    from . import db, review
    with db.connect() as conn:
        db.apply_schema(conn)
        review.run(conn, limit=args.limit, redo=args.redo)
    return 0


def cmd_serve(args):
    from . import serve
    serve.main(args.bind or config.API_BIND, args.port or config.API_PORT)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="recall", description=__doc__.splitlines()[0])
    p.add_argument("--debug", action="store_true",
                   help="show the traceback instead of a short message")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the setup")
    d.add_argument("--data")
    d.set_defaults(fn=cmd_doctor)

    i = sub.add_parser("ingest", help="index everything under the data dir")
    i.add_argument("--data")
    i.add_argument("--no-index", action="store_true",
                   help="skip the vector index; use when more loads follow")
    i.set_defaults(fn=cmd_ingest)

    a = sub.add_parser("ask", help="answer a question from the archive")
    a.add_argument("question", nargs="+")
    a.add_argument("-k", type=int, default=8)
    a.add_argument("--source")
    a.add_argument("--sources-only", action="store_true")
    a.add_argument("--no-stream", action="store_true")
    a.set_defaults(fn=cmd_ask)

    j = sub.add_parser("judge", help="grade logged answers with a model")
    j.add_argument("--limit", type=int, default=50, help="rows per batch")
    j.add_argument("--redo", action="store_true",
                   help="judge rows that already carry a judgment")
    j.add_argument("--dry-run", action="store_true",
                   help="print judgments, write nothing")
    j.set_defaults(fn=cmd_judge)

    r = sub.add_parser("review", help="label logged answers by hand")
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--redo", action="store_true",
                   help="revisit rows you already labelled")
    r.set_defaults(fn=cmd_review)

    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--bind")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted. Progress is stored; re-run to resume.",
              file=sys.stderr)
        return 130
    except ImportError as e:
        print(f"missing dependency: {e}", file=sys.stderr)
        print("run:  pip install -e .", file=sys.stderr)
        return 1
    except Exception as e:
        # A setup fault should point at the command that diagnoses it, not
        # at a stack trace. Use --debug when you want the traceback.
        if args.debug:
            raise
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        print("run:  recall doctor", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
