"""Apple Messages, from a copy of chat.db.

On modern macOS `message.text` is NULL for almost every row; the body lives
in `attributedBody`. Group chat names matter too. See docs/lessons.md.
"""

import bisect
import datetime as dt
import sqlite3
import struct

from .base import Chunk, Source, walk

APPLE_EPOCH = 978307200
SESSION_GAP_S = 1800     # live chat: 30 minutes separates two conversations
MAX_TURNS = 20
ATTACHMENT_PREFIX = "~/Library/Messages/Attachments/"
CONTEXT_SPAN_S = 1800
CONTEXT_TURNS = 3

_SQL = """
SELECT m.ROWID, c.guid, COALESCE(h.id, ''), m.date, m.is_from_me,
       m.text, m.attributedBody
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
"""


_ATTACHMENTS_SQL = """
SELECT a.filename, m.ROWID, c.guid, COALESCE(h.id, ''), m.date, m.is_from_me
FROM attachment a
JOIN message_attachment_join j ON j.attachment_id = a.ROWID
JOIN message m ON m.ROWID = j.message_id
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE a.filename IS NOT NULL
"""

_UNJOINED_SQL = """
SELECT a.filename, a.created_date
FROM attachment a
LEFT JOIN message_attachment_join j ON j.attachment_id = a.ROWID
WHERE a.filename IS NOT NULL AND j.message_id IS NULL
"""


def _iso(unix):
    return dt.datetime.fromtimestamp(
        unix, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def local_path(filename, root):
    """The on-disk file for an attachment row. chat.db records the Mac's
    path; the export keeps the same tree in Attachments/ beside chat.db."""
    if not filename or not filename.startswith(ATTACHMENT_PREFIX):
        return None
    return root / filename[len(ATTACHMENT_PREFIX):]


def _cached(hashes, file, seen):
    """(sha, record) for a captioned file the cache knows and this run has
    not yielded yet. The sha is marked seen before the record is read, so
    a second file with the same bytes never yields twice."""
    from .. import captions, config
    sha = hashes.known(file)
    if sha is None or sha in seen:
        return None
    seen.add(sha)
    rec = captions.read_record(config.WORK_DIR, sha)
    if not rec or rec.get("skipped") or not rec.get("caption"):
        return None
    return sha, rec


def _ocr_line(rec):
    from .. import captions
    ocr = rec.get("ocr_text") or ""
    return f"Text in image: {ocr}" if len(ocr) >= captions.OCR_MIN_CHARS else ""


def context_window(rows, ats, at, contacts, span_s=CONTEXT_SPAN_S,
                   turns=CONTEXT_TURNS):
    """Up to `turns` texted lines either side of `at`, within `span_s`, in
    one thread. `ats` is the sorted time of each row, for bisect."""
    from ..naming import label
    i = bisect.bisect_left(ats, at)
    before = [r for r in rows[max(0, i - turns):i] if at - r["at"] <= span_s]
    after = [r for r in rows[i:i + turns] if r["at"] - at <= span_s]
    return "\n".join(
        f"{'me' if r['mine'] else label(r['handle'] or 'them', contacts)}: "
        f"{r['text']}"
        for r in before + after)


def decode_attributed_body(blob):
    """Body string from Apple's streamtyped archive: find NSString, scan to
    the next 0x2b, read a variable-width length, then that many UTF-8 bytes."""
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
        return sorted(p for p in walk(root)
                      if p.name == "chat.db" and p.is_file())

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
            # A message joined to two chats is one event: it keeps the
            # first chat by guid, or two windows would share one ref.
            seen, once = set(), []
            for r in out:
                if r["rowid"] not in seen:
                    seen.add(r["rowid"])
                    once.append(r)
            return once, names
        finally:
            con.close()

    def _attachments(self, path):
        """[(file, message)] for every attachment row whose file exists.
        The message is carried even when it has no text: most photos are
        sent with none, and the link is what dates the image."""
        root = path.parent / "Attachments"
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(_ATTACHMENTS_SQL).fetchall()
        finally:
            con.close()
        out = []
        for filename, rid, guid, handle, date, mine in rows:
            p = local_path(filename, root)
            if p is not None and p.is_file():
                out.append((p, {"rowid": rid, "thread": guid or "",
                                "handle": handle, "at": _unix(date),
                                "mine": bool(mine)}))
        # Earliest message first, so an image sent twice always dates by
        # its first sending. The query's own order changed between runs.
        out.sort(key=lambda pm: (pm[1]["at"], pm[1]["rowid"]))
        return out

    def _orphans(self, path, linked):
        """Files under Attachments/ that no attachment row names: the
        message was deleted and the file stayed. A `.pvt` directory is a
        Live Photo bundle whose still repeats the HEIC beside it, so its
        contents are skipped."""
        root = path.parent / "Attachments"
        return sorted(
            p for p in walk(root)
            if p not in linked
            and not any(part.endswith(".pvt")
                        for part in p.relative_to(root).parts))

    def _orphan_facts(self, path):
        """Every chat guid, and the creation time of each attachment row
        that has no message: a deleted message leaves its row behind. An
        export old enough to lack `created_date` dates nothing."""
        root = path.parent / "Attachments"
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            chats = {guid for (guid,) in con.execute("SELECT guid FROM chat")}
            created = {}
            try:
                rows = con.execute(_UNJOINED_SQL).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for filename, date in rows:
                p = local_path(filename, root)
                if p is not None and date:
                    created[p] = _unix(date)
        finally:
            con.close()
        return chats, created

    def media(self, path):
        linked = {p for p, _ in self._attachments(path)}
        return sorted(linked) + self._orphans(path, linked)

    def samples(self, path):
        rows, _ = self._rows(path)
        rows.sort(key=lambda r: len(r["text"]), reverse=True)
        return [r["text"] for r in rows[:8]]

    def chunks(self, path, budget, contacts=None):
        from .. import trends
        rows, names = self._rows(path)
        yield from self._windows(rows, names, contacts, budget)
        yield from trend_chunks(rows, contacts, budget, trends.zone())
        yield from self._attachment_chunks(path, rows, names, contacts, budget)

    def _attachment_chunks(self, path, rows, names, contacts, budget):
        """One chunk per captioned image, from the cache `recall caption`
        fills. Nothing here reads image bytes: a file the cache has not
        hashed yet is simply not ready."""
        from .. import captions, config
        from ..chunking import fit
        from ..naming import header
        contacts = contacts or {}
        by_thread = {}
        for r in rows:
            by_thread.setdefault(r["thread"], []).append(r)
        ats = {t: [r["at"] for r in rs] for t, rs in by_thread.items()}
        handles = {t: {r["handle"] for r in rs if r["handle"]}
                   for t, rs in by_thread.items()}
        hashes = captions.HashCache(config.WORK_DIR)
        seen = set()
        floor = len("Said around it: ") + captions.OCR_MIN_CHARS
        linked = self._attachments(path)
        for file, msg in linked:
            hit = _cached(hashes, file, seen)
            if hit is None:
                continue
            sha, rec = hit
            thread = msg["thread"]
            who = sorted(handles.get(thread, set())
                         | ({msg["handle"]} if msg["handle"] else set()))
            when = _iso(msg["at"])
            name = names.get(thread)
            group = f'"{name}" with ' if name else "with "
            lines = [f"[{when[:10]}, {group}{header(who, contacts)}]",
                     f"Image: {rec['caption']}"]
            around = context_window(by_thread.get(thread, []),
                                    ats.get(thread, []), msg["at"], contacts)
            text = "\n".join(lines[:2])
            extra = [_ocr_line(rec),
                     f"Said around it: {around}" if around else ""]
            for line in fit(budget - len(text), extra, floor):
                text += "\n" + line
            yield Chunk(ref=f"attachment:{sha}", text=text, source=self.name,
                        occurred_at=when, date_confidence="exact",
                        participants=who, thread=thread)
        chats, created = self._orphan_facts(path)
        for file in self._orphans(path, {p for p, _ in linked}):
            hit = _cached(hashes, file, seen)
            if hit is None:
                continue
            sha, rec = hit
            when = _iso(created[file]) if file in created else None
            thread = file.parent.name if file.parent.name in chats else None
            who = sorted(handles.get(thread, set())) if thread else []
            if thread:
                name = names.get(thread)
                kind = (f'chat photo of "{name}" with ' if name
                        else "chat photo with") + header(who, contacts)
            else:
                kind = "attachment with no message"
            text = (f"[{when[:10] if when else 'undated'}, {kind}]\n"
                    f"Image: {rec['caption']}")
            for line in fit(budget - len(text), [_ocr_line(rec)], floor):
                text += "\n" + line
            yield Chunk(ref=f"attachment:{sha}", text=text, source=self.name,
                        occurred_at=when,
                        date_confidence="metadata" if when else "low",
                        participants=who, thread=thread)

    def _windows(self, rows, names, contacts, budget):
        from ..chunking import parts, sessions
        from ..naming import header, label
        contacts = contacts or {}
        for window in sessions(rows, SESSION_GAP_S, MAX_TURNS,
                               key=lambda r: r["thread"],
                               when=lambda r: r["at"]):
            first = window[0]
            who = sorted({r["handle"] for r in window if r["handle"]})
            when = dt.datetime.fromtimestamp(
                first["at"], dt.timezone.utc).isoformat().replace("+00:00", "Z")
            name = names.get(first["thread"])
            group = f'"{name}" with ' if name else "with "
            head = f"[{when[:10]}, {group}{header(who, contacts)}"
            lines = [
                f"{'me' if r['mine'] else label(r['handle'] or 'them', contacts)}"
                f": {r['text']}"
                for r in window]
            for suffix, text in parts(lines, budget,
                                      lambda l: f"{head}{l}]"):
                yield Chunk(
                    ref=f"message:{first['rowid']}{suffix}",
                    text=text,
                    source=self.name,
                    occurred_at=when,
                    date_confidence="exact",
                    participants=who,
                    thread=first["thread"],
                )


def trend_chunks(rows, contacts, budget, tz):
    """One rollup per year: volume, busiest weekday and month, top contacts,
    and the longest daily streak per contact. See trends.py."""
    import collections
    import datetime as dt
    from .. import trends
    from ..naming import label
    contacts = contacts or {}
    at = lambda r: r["at"]

    def streak_lines(items):
        days = collections.defaultdict(set)
        for r in items:
            if r["handle"]:
                days[r["handle"]].add(trends._local(r["at"], tz).date())
        best = sorted(((trends.longest_streak(d), h) for h, d in days.items()),
                      key=lambda x: -x[0][0])[:3]
        return ", ".join(f"{label(h, contacts)} {n} days ({a} to {b})"
                         for (n, a, b), h in best if n)

    def top_line(items):
        by_handle = collections.Counter(r["handle"] for r in items if r["handle"])
        return by_handle, "Most messaged contacts: " + ", ".join(
            f"{label(h, contacts)} ({n:,})" for h, n in trends.top(by_handle))

    years = trends.by_year(rows, at, tz)
    for year, items in years.items():
        sent = sum(1 for r in items if r["mine"])
        by_handle, top = top_line(items)
        lines = [
            f"{len(items):,} messages: {sent:,} sent, {len(items) - sent:,} received.",
            trends.describe_weekdays(trends.weekday_counts(items, at, tz)),
            "Sent only, " + trends.describe_weekdays(
                trends.weekday_counts([r for r in items if r["mine"]], at, tz)),
            trends.describe_months(trends.month_counts(items, at, tz)),
            top,
            "Longest daily messaging streaks within the year: " + streak_lines(items),
        ]
        yield from trends.chunks("messages", year, lines, budget, tz,
                                 participants=sorted(by_handle))

    # All time, because a streak that crosses New Year is cut by a per-year
    # table and "which contact shows the longest streak" names no year.
    if years:
        dated = [r for r in rows if r.get("at") is not None]
        by_handle, top = top_line(dated)
        lines = [
            f"{len(dated):,} messages across {min(years)} to {max(years)}.",
            top,
            "Longest daily messaging streaks across all years: " + streak_lines(dated),
        ]
        yield from trends.all_time_chunks("messages", min(years), lines, budget, tz,
                                          participants=sorted(by_handle))
