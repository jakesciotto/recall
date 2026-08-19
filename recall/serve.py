"""HTTP API. Retrieval and generation stay local; only the answer travels.

Requires a bearer token and fails closed: an unset token accepts nothing.
"""

import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import answer, config, db, render, retrieve

MAX_K = 25


class BadRequest(Exception):
    pass


def authorized(header, token):
    """Constant-time bearer check. A plain == leaks the token by timing."""
    if not token or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):], token)


def sse(event, data):
    """One Server-Sent Event frame. SSE is newline delimited, so a raw
    newline in the payload would end the event early; json.dumps escapes it."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _search(body, conn):
    question = (body.get("question") or "").strip()
    if not question:
        raise BadRequest("question is required")
    try:
        k = max(1, min(int(body.get("k") or 8), MAX_K))
    except (TypeError, ValueError):
        k = 8
    from . import embed
    hits, dates = retrieve.search(
        question, conn, lambda t: embed.embed([t])[0],
        k=k, source=body.get("source"))
    return question, hits, dates


def _payload(dates):
    return ({"phrase": dates.phrase, "since": dates.since,
             "until": dates.until} if dates and dates.phrase else None)


def handle_ask(body):
    with db.connect() as conn:
        question, hits, dates = _search(body, conn)
    text = None
    if not body.get("sources_only") and config.CHAT_URL:
        text = answer.chat(answer.build_prompt(question, hits))
    return {"question": question, "answer": text,
            "answer_blocks": render.blocks(text),
            "sources": hits, "date_filter": _payload(dates)}


def stream_ask(body):
    """Sources first, then tokens, then the parsed answer.

    Retrieval is fast and generation is slow, so sources go out immediately.
    Blocks need the whole answer, so they arrive last.
    """
    with db.connect() as conn:
        question, hits, dates = _search(body, conn)
    yield sse("sources", {"sources": hits, "date_filter": _payload(dates)})
    if not config.CHAT_URL:
        yield sse("done", {"answer": None, "answer_blocks": []})
        return
    parts = []
    for piece in answer.chat_stream(answer.build_prompt(question, hits)):
        parts.append(piece)
        yield sse("token", {"t": piece})
    text = "".join(parts)
    yield sse("done", {"answer": text, "answer_blocks": render.blocks(text)})


class Handler(BaseHTTPRequestHandler):
    token = ""

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if path not in ("/ask", "/ask/stream"):
            self._send(404, {"error": "not found"})
            return
        if not authorized(self.headers.get("Authorization"), self.token):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            self._send(400, {"error": "invalid json"})
            return

        if path == "/ask":
            try:
                self._send(200, handle_ask(body))
            except BadRequest as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"[:200]})
            return

        # Every frame is flushed. Without the flush the OS buffer holds
        # tokens until it fills, which recreates the stall streaming removes.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")   # ask nginx not to buffer
        self.end_headers()
        try:
            for frame in stream_ask(body):
                self.wfile.write(frame.encode())
                self.wfile.flush()
        except BrokenPipeError:
            return
        except Exception as e:
            self.wfile.write(sse("error", {"error": str(e)[:200]}).encode())
            self.wfile.flush()

    def log_message(self, *args):
        pass          # never log the question


def main(bind, port):
    if not config.API_TOKEN:
        raise SystemExit("RECALL_API_TOKEN is not set, refusing to start")
    Handler.token = config.API_TOKEN
    print(f"serving on {bind}:{port}", flush=True)
    HTTPServer((bind, port), Handler).serve_forever()
