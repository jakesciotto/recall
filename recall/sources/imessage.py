"""Apple Messages, from a copy of chat.db.

Two findings from a real 675,967 message database shape this adapter.

**The text is not in `message.text`.** On modern macOS that column is NULL
for 99.7 percent of rows; the real body sits in `attributedBody`, an Apple
NSArchiver blob. An adapter that reads `text` captures a fraction of a
percent of the corpus and reports no error at all.

**Group chats have names, and they are worth more than they look.** 55 named
chats covered 15 percent of one corpus. Without the name, those chunks show
only phone numbers, so a question naming the group cannot match anything.
"""

import datetime as dt
import sqlite3
import struct

from .base import Chunk, Source

APPLE_EPOCH = 978307200
SESSION_GAP_S = 1800     # live chat: 30 minutes separates two conversations
MAX_TURNS = 20

_SQL = """
SELECT m.ROWID, c.guid, COALESCE(h.id, ''), m.date, m.is_from_me,
       m.text, m.attributedBody
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
"""


def decode_attributed_body(blob):
    """Pull the body string out of Apple's streamtyped archive.

    Find NSString, scan to the next 0x2b, read a variable-width length, then
    that many UTF-8 bytes. Validated against 1,795 rows that carried both
    columns: 1,795 exact matches.
    """
    if not blob:
        return ""
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    j = blob.find(b"+", i)
    if j < 0:
        return ""
    p = j + 1
    if p >= len(blob):
        return ""
    marker = blob[p]
    if marker == 0x81:
        n = struct.unpack("<H", blob[p + 1:p + 3])[0]; p += 3
    elif marker == 0x82:
        n = struct.unpack("<I", blob[p + 1:p + 5])[0]; p += 5
    elif marker == 0x83:
        n = struct.unpack("<Q", blob[p + 1:p + 9])[0]; p += 9
    else:
        n = marker; p += 1
    return blob[p:p + n].decode("utf-8", "replace")


def _unix(value):
    """Apple stores seconds or nanoseconds since 2001 depending on version."""
    if value is None:
        return 0
    if value > 100_000_000_000:
        value //= 1_000_000_000
    return value + APPLE_EPOCH


class IMessage(Source):
    name = "messages"

    def detect(self, root):
        return sorted(p for p in root.rglob("chat.db") if p.is_file())

    def _rows(self, path):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {g: n for g, n in con.execute(
                "SELECT guid, display_name FROM chat "
                "WHERE display_name IS NOT NULL AND display_name != ''")}
            out = []
            for rid, guid, handle, date, mine, text, blob in con.execute(_SQL):
                body = text or decode_attributed_body(blob)
                if not body or not body.strip():
                    continue
                out.append({
                    "rowid": rid, "thread": guid or "", "handle": handle,
                    "at": _unix(date), "mine": bool(mine),
                    "text": body.strip(),
                })
            out.sort(key=lambda r: (r["thread"], r["at"], r["rowid"]))
            return out, names
        finally:
            con.close()

    def samples(self, path):
        rows, _ = self._rows(path)
        rows.sort(key=lambda r: len(r["text"]), reverse=True)
        return [r["text"] for r in rows[:8]]

    def chunks(self, path, budget):
        from ..chunking import sessions
        rows, names = self._rows(path)
        for window in sessions(rows, SESSION_GAP_S, MAX_TURNS,
                               key=lambda r: r["thread"],
                               when=lambda r: r["at"]):
            first = window[0]
            who = sorted({r["handle"] for r in window if r["handle"]})
            when = dt.datetime.fromtimestamp(
                first["at"], dt.timezone.utc).isoformat().replace("+00:00", "Z")
            name = names.get(first["thread"])
            label = f'"{name}" with ' if name else "with "
            body = "\n".join(
                f"{'me' if r['mine'] else (r['handle'] or 'them')}: {r['text']}"
                for r in window)
            yield Chunk(
                ref=f"message:{first['rowid']}",
                text=f"[{when[:10]}, {label}{', '.join(who) or 'unknown'}]\n{body}",
                source=self.name,
                occurred_at=when,
                date_confidence="exact",
                participants=who,
                thread=first["thread"],
            )
