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

    from . import db
    # Without the driver the database check can only repeat the same fault.
    if not have_driver:
        print("  skip  postgres check (needs psycopg)")
    else:
      try:
          with db.connect() as conn:
              db.apply_schema(conn)
              rows = db.counts(conn)
          total = sum(n for _, n in rows)
          _ok("postgres reachable", f"{total:,} chunks stored")
          for source, n in rows:
              print(f"          {source:14s} {n:,}")
      except Exception as e:
          _bad("postgres unreachable", f"{type(e).__name__}: {str(e)[:90]}")
          print("          try: docker compose up -d")
          problems += 1

    try:
        req = urllib.request.Request(
            config.EMBED_URL,
            json.dumps({"model": config.EMBED_MODEL,
                        "input": ["ping"]}).encode(),
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60):
            _ok("embedding server", config.EMBED_URL)
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
        from .sources import detect_all
        found = detect_all(data)
        if found:
            _ok("sources found", ", ".join(a.name for a, _ in found))
        else:
            print(f"  warn  no recognised sources under {data}")
            print("          drop an export in and re-run; see docs/sources.md")
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
    from . import answer, db, render, retrieve
    question = " ".join(args.question)
    with db.connect() as conn:
        hits, dates = retrieve.search(question, conn, _embedder(),
                                      k=args.k, source=args.source,
                                      today=dt.date.today())
    if dates.phrase:
        print(f"date filter: {dates.phrase} -> {dates.since} .. {dates.until}",
              file=sys.stderr)
    for i, h in enumerate(hits, start=1):
        print(f"[{i}] {answer.cite(h)}", file=sys.stderr)
    if args.sources_only or not config.CHAT_URL:
        if not args.sources_only:
            print("no generation endpoint set; showing sources only. "
                  "See docs/answering.md.", file=sys.stderr)
        return 0 if hits else 1

    print()
    prompt = answer.build_prompt(question, hits)
    if args.no_stream:
        print(answer.chat(prompt))
    else:
        for piece in answer.chat_stream(prompt):
            sys.stdout.write(piece)
            sys.stdout.flush()
        print()
    return 0 if hits else 1


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
