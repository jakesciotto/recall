"""Email from an mbox export (Google Takeout, Thunderbird, offlineimap).

Filtering is the job, not parsing, and the filter is an allowlist: a receipt
carries both Category Purchases and Category Updates, so a denylist drops
real receipts. See docs/lessons.md.
"""

import email
import email.policy
import email.utils
import datetime as dt
import re
from email.header import decode_header, make_header

from .base import Chunk, Source, walk

KEEP_LABELS = {
    "Category Personal", "Category Purchases", "Category Travel",
    "Category Bills", "Category Forums", "Sent", "Chat", "Starred",
}

# Threads repeat themselves. Without stripping, a ten message thread embeds
# its first message ten times and the chunk is mostly an echo.
_QUOTE = re.compile(
    r"^\s*(On .{0,120}\bwrote:\s*$"
    r"|-{2,}\s*Original Message\s*-{2,}"
    r"|_{5,}\s*$"
    r"|From:\s.+\bSent:\s)", re.I | re.M)
_TAG = re.compile(r"<[^>]+>")
_ADDR = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# A message separator, matched by SHAPE. "From now on..." at the start of a
# body line is ordinary prose and must not split a message in half.
_SEP = re.compile(rb"^From \S+@\S* \w{3} \w{3} ")


def raw_messages(stream):
    """Yield each raw message as bytes. Streams: an mbox can be many GB."""
    cur = []
    for line in stream:
        if _SEP.match(line) and cur:
            yield b"".join(cur)
            cur = []
        cur.append(line)
    if cur:
        yield b"".join(cur)


def _strip_html(html):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = _TAG.sub(" ", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"')):
        text = text.replace(a, b)
    return re.sub(r"[ \t]{2,}", " ", text)


def body_text(raw):
    """Readable body, preferring text/plain. Never raises: one malformed
    message must not end a run."""
    try:
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    except Exception:
        return ""
    plain, html = [], []
    for part in msg.walk():
        # Text parts only. Everything else, inline images, PDFs, calendar
        # invites, octet-streams, is not readable and decodes to bytes full
        # of NUL. A quarter of one real mailbox's chunks carried them.
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        if "attachment" in str(part.get("Content-Disposition") or "").lower():
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", "replace")
        (plain if part.get_content_type() == "text/plain" else html).append(text)
    out = "\n".join(t for t in plain if t).strip()
    return out or _strip_html("\n".join(html)).strip()


def strip_quoted(text):
    if not text:
        return ""
    m = _QUOTE.search(text)
    if m:
        text = text[:m.start()]
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith(">")).strip()


def _header(msg, name):
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _iso(msg):
    raw = msg.get("Date")
    if raw:
        try:
            d = email.utils.parsedate_to_datetime(raw)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            pass
    return None


def _addr(sender):
    m = _ADDR.search(sender or "")
    return m.group(0).lower() if m else (sender or "").strip()


class Mbox(Source):
    name = "email"

    def detect(self, root):
        return sorted(p for p in walk(root)
                      if p.name.lower().endswith(".mbox") and p.is_file())

    def samples(self, path, limit=400):
        out = []
        with open(path, "rb") as fh:
            for raw in raw_messages(fh):
                body = strip_quoted(body_text(raw))
                if body:
                    out.append(body)
                if len(out) >= limit:
                    break
        out.sort(key=len, reverse=True)
        return out[:8]

    def _kept(self, path):
        """Messages that pass the allowlist, grouped by thread."""
        threads = {}
        with open(path, "rb") as fh:
            for n, raw in enumerate(raw_messages(fh)):
                try:
                    msg = email.message_from_bytes(
                        raw, policy=email.policy.compat32)
                except Exception:
                    continue
                labels = {x.strip() for x in
                          (msg.get("X-Gmail-Labels") or "").split(",") if x.strip()}
                # No labels at all means a plain mbox with no Gmail metadata.
                # Keep those: the user exported them deliberately.
                if labels and not (labels & KEEP_LABELS):
                    continue
                body = strip_quoted(body_text(raw))
                if not body:
                    continue
                tid = msg.get("X-GM-THRID") or msg.get("Message-ID") or f"n{n}"
                threads.setdefault(tid, []).append({
                    "subject": _header(msg, "Subject"),
                    "sender": _addr(_header(msg, "From")),
                    "at": _iso(msg),
                    "text": body,
                })
        return threads

    def chunks(self, path, budget, contacts=None):
        from ..chunking import pack
        from ..naming import label
        contacts = contacts or {}
        for tid, msgs in sorted(self._kept(path).items()):
            msgs.sort(key=lambda m: m["at"] or "")
            first = msgs[0]
            subject = (first["subject"] or "(no subject)").strip()
            day = (first["at"] or "")[:10]
            who = sorted({m["sender"] for m in msgs if m["sender"]})
            # The header rides on top of a body already packed to the
            # budget, so its length comes out of the room first. A subject
            # is capped so a runaway one cannot starve the body.
            header = f"[{day}, email thread: {subject[:200]}]"
            prefix = max(len(f"{label(m['sender'], contacts)} "
                             f"({(m['at'] or '')[:10]}): ") for m in msgs)
            room = max(budget - len(header) - 1 - prefix, budget // 2)
            # The suffix is gated on the NUMBER OF PARTS, not on the thread
            # holding more messages than a part. pack splits one over-budget
            # message into pieces, and a thread of one long message then
            # yields two parts of one item each; the old test read False for
            # both and the second overwrote the first on the unique ref.
            parts = list(pack(msgs, room))
            multi = len(parts) > 1
            for i, part in enumerate(parts, start=1):
                body = "\n\n".join(
                    f"{label(m['sender'], contacts)} "
                    f"({(m['at'] or '')[:10]}): {m['text']}"
                    for m in part)
                yield Chunk(
                    ref=f"email:{tid}" + (f"#{i}" if multi else ""),
                    text=f"{header}\n{body}",
                    source=self.name,
                    occurred_at=first["at"],
                    date_confidence="exact" if first["at"] else "low",
                    participants=who,
                    thread=str(tid),
                )
