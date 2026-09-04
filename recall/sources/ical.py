"""iCalendar exports, as month rollups plus a chunk per described event.

Event titles are a life record rather than noise: a race, a flight, a
class, a doctor. A calendar is also often the only temporal spine in an
archive. Two chunk kinds come out of one file.

Events roll up to one chunk per month, the same shape Spotify and Health
use. One chunk per event makes thousands of chunks whose median title is
sixteen characters, and "[2019-06-04] Dentist" retrieves badly while
diluting everything around it.

An event with a real description ALSO gets its own chunk. Descriptions hold
meeting agendas and forwarded mail, the highest-signal text in the file. An
event therefore appears in both places on purpose: the month gives context,
the event gives detail.

This module is `ical`, not `calendar`, because the standard library owns
that name. See docs/lessons.md for the parsing traps.
"""

import collections
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..chunking import parts, split_to_budget
from ..naming import label
from .base import Chunk, Source, walk

# Below this a description is a conference link or a one-line note, and it
# adds nothing the month rollup does not already carry.
MIN_DESCRIPTION_CHARS = 120

# Names shown per event in a month rollup. One real meeting carries eleven
# addresses, which crowds out the rest of the month. The event chunk still
# lists everybody.
MONTH_LINE_NAMES = 4

SAMPLE_COUNT = 20

_FREQ = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly",
         "YEARLY": "yearly"}


def unfold(text):
    """Join RFC 5545 continuation lines.

    A line beginning with a space or a tab continues the line before it.
    Read the file without this and every long value truncates at the fold,
    with no error: that is how hundreds of real descriptions measure as
    empty.
    """
    return re.sub(r"\r?\n[ \t]", "", text).replace("\r\n", "\n")


def unescape(value):
    """Decode the TEXT escapes: backslash, comma, semicolon, and newline.

    One scan, left to right, rather than a chain of replaces. A chain turns
    an escaped backslash into a real one and then reads the FOLLOWING
    character as part of a new escape, so an escaped backslash before an n
    wrongly becomes a newline.
    """
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _prop(line):
    """('NAME', {param: value}, 'value') for one unfolded content line."""
    name_params, _, value = line.partition(":")
    pieces = name_params.split(";")
    params = {}
    for p in pieces[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return pieces[0].upper(), params, value


def parse_events(text):
    """Every usable VEVENT, as dicts.

    Only VEVENT blocks are read. A VTIMEZONE block carries its own DTSTART
    for the daylight saving rule, always dated 1970, so a whole-file scan
    invents events that never existed.
    """
    body = unfold(text)
    out = []
    for block in re.findall(r"^BEGIN:VEVENT$(.*?)^END:VEVENT$",
                            body, re.S | re.M):
        ev = {"uid": "", "summary": "", "description": "", "location": "",
              "rrule": "", "recurrence_id": "", "status": "",
              "dtstart_params": {}, "dtstart_value": "", "created": "",
              "attendees": [], "cn": {}}
        for line in block.strip().split("\n"):
            if ":" not in line:
                continue
            name, params, value = _prop(line)
            if name == "UID":
                ev["uid"] = value.strip()
            elif name == "SUMMARY":
                ev["summary"] = unescape(value).strip()
            elif name == "DESCRIPTION":
                ev["description"] = unescape(value).strip()
            elif name == "LOCATION":
                ev["location"] = unescape(value).strip()
            elif name == "STATUS":
                ev["status"] = value.strip().upper()
            elif name == "RRULE":
                ev["rrule"] = value.strip()
            elif name == "RECURRENCE-ID":
                ev["recurrence_id"] = value.strip()
            elif name == "DTSTART":
                ev["dtstart_params"] = params
                ev["dtstart_value"] = value.strip()
            elif name == "CREATED":
                ev["created"] = value.strip()
            elif name == "ATTENDEE":
                email = re.sub(r"^mailto:", "", value.strip(), flags=re.I)
                if not email:
                    continue
                ev["attendees"].append(email)
                cn = params.get("CN")
                if cn:
                    ev["cn"][email.lower()] = unescape(cn).strip()
        # A cancelled event is a plan that did not happen, and an event with
        # no start cannot be placed in a month.
        if ev["status"] == "CANCELLED" or not ev["dtstart_value"]:
            continue
        out.append(ev)
    return out


def event_time(ev):
    """(aware UTC datetime, all_day) for one event, or (None, False).

    Exports use three DTSTART forms: UTC, an all-day date, and a time in a
    named zone. Reading only the UTC form misplaces the other two by up to
    seven hours.
    """
    raw = ev["dtstart_value"]
    params = ev["dtstart_params"]
    if params.get("VALUE") == "DATE" or (len(raw) == 8 and raw.isdigit()):
        try:
            day = dt.datetime.strptime(raw[:8], "%Y%m%d")
        except ValueError:
            return None, False
        return day.replace(tzinfo=dt.timezone.utc), True
    try:
        naive = dt.datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    if raw.endswith("Z"):
        return naive.replace(tzinfo=dt.timezone.utc), False
    tzid = params.get("TZID")
    if tzid:
        try:
            local = naive.replace(tzinfo=ZoneInfo(tzid))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            # A zone this machine does not carry must not end the run. UTC
            # keeps the event on the right day, which is what a month
            # rollup needs.
            return naive.replace(tzinfo=dt.timezone.utc), False
        return local.astimezone(dt.timezone.utc), False
    # A floating time has no zone by definition. Treat it as UTC.
    return naive.replace(tzinfo=dt.timezone.utc), False


def event_ref(ev):
    """The stable ref for one event.

    RECURRENCE-ID marks a single moved or edited occurrence, and it reuses
    the parent UID. Without the suffix the two collide on the UNIQUE ref and
    one of them is silently lost.
    """
    base = f"calendar:{ev['uid']}"
    return f"{base}@{ev['recurrence_id']}" if ev["recurrence_id"] else base


def _iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _names(ev, contacts):
    """Display names for the attendees, most trustworthy source first.

    The contacts map wins because it is the one source a later re-map can
    update. The CN in the file is the fallback, and the bare address is the
    last resort.
    """
    out = set()
    for email in ev["attendees"]:
        key = email.strip().lower()
        named = label(key, contacts)
        out.add(ev["cn"].get(key, named) if named == key else named)
    return sorted(out)


def _repeat(ev):
    m = re.search(r"FREQ=([A-Z]+)", ev["rrule"] or "")
    if not m:
        return ""
    return _FREQ.get(m.group(1), m.group(1).lower())


def _oneline(value):
    """Collapse every run of whitespace to a single space.

    A LOCATION or a SUMMARY can carry its own newline: a venue and its
    street address on separate lines. The month rollup promises one line
    per event, so an embedded newline leaves an address floating with no
    event attached.
    """
    return re.sub(r"\s+", " ", value or "").strip()


def event_line(ev, contacts):
    """One line for the month rollup.

    A recurring event is listed once, in its starting month, with its rule
    named. Expanding a weekly class across three years writes hundreds of
    identical lines and buries every other event in every month it touches.
    """
    when, all_day = event_time(ev)
    stamp = when.strftime("%d %a")
    clock = "all day" if all_day else when.strftime("%H:%M")
    bits = [f"{stamp} {clock}", _oneline(ev["summary"]) or "(no title)"]
    repeat = _repeat(ev)
    if repeat:
        bits.append(f"(repeats {repeat})")
    if ev["location"]:
        bits.append(f"at {_oneline(ev['location'])}")
    names = _names(ev, contacts)
    if names:
        shown = names[:MONTH_LINE_NAMES]
        extra = len(names) - len(shown)
        text = ", ".join(shown)
        if extra:
            text += f" and {extra} more"
        bits.append("with " + text)
    return "  ".join(bits)


def month_chunks(month, events, budget, contacts, path):
    lines = [event_line(ev, contacts) for ev in events]
    seen, participants = set(), []
    for ev in events:
        for email in ev["attendees"]:
            if email not in seen:
                seen.add(email)
                participants.append(email)
    head = lambda part: f"[{month}, calendar{part}]\n{len(events)} events."
    for suffix, text in parts(lines, budget, head):
        yield Chunk(
            ref=f"calendar:{month}{suffix}",
            text=text,
            source="calendar",
            occurred_at=f"{month}-01T00:00:00Z",
            date_confidence="period",
            participants=sorted(participants),
            path=path,
        )


def event_chunks(ev, budget, contacts, path):
    """One described event as one chunk, or several if the text is long.

    The header rides on top of a body already packed to the budget, so its
    length comes out of the room before the body is split. A header that
    would take more than half the budget is cut, because an attendee list
    must never starve the description it exists to introduce.
    """
    when, all_day = event_time(ev)
    stamp = when.strftime("%Y-%m-%d")
    clock = "all day" if all_day else when.strftime("%H:%M")
    head = [f"[{stamp} {clock}, calendar]",
            _oneline(ev["summary"]) or "(no title)"]
    if ev["location"]:
        head.append(f"Location: {_oneline(ev['location'])}")
    names = _names(ev, contacts)
    if names:
        head.append("Attendees: " + ", ".join(names))
    repeat = _repeat(ev)
    if repeat:
        head.append(f"Repeats {repeat}.")
    header = "\n".join(head)[:budget // 2]

    room = max(budget - len(header) - 2, 1)
    pieces = split_to_budget(ev["description"], room)
    base = event_ref(ev)
    single = len(pieces) == 1
    for i, piece in enumerate(pieces, start=1):
        yield Chunk(
            ref=base if single else f"{base}#{i}",
            text=f"{header}\n\n{piece}",
            source="calendar",
            occurred_at=_iso(when),
            date_confidence="period" if all_day else "exact",
            participants=list(ev["attendees"]),
            path=path,
        )


def build(events, budget, contacts=None, path=None):
    """Month rollups, then a chunk for every substantially described event."""
    contacts = contacts or {}
    months = collections.defaultdict(list)
    dated = []
    for ev in events:
        when, _ = event_time(ev)
        if when is None:
            continue
        dated.append((when, ev))
        months[when.strftime("%Y-%m")].append((when, ev))

    for month in sorted(months):
        group = [ev for _, ev in sorted(months[month],
                                        key=lambda p: (p[0], p[1]["uid"]))]
        yield from month_chunks(month, group, budget, contacts, path)

    for _, ev in sorted(dated, key=lambda p: (p[0], p[1]["uid"])):
        if len(ev["description"]) < MIN_DESCRIPTION_CHARS:
            continue
        yield from event_chunks(ev, budget, contacts, path)


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


class ICal(Source):
    name = "calendar"

    def detect(self, root):
        return sorted(p for p in walk(root) if p.suffix.lower() == ".ics")

    def samples(self, path):
        texts = [ev["description"] for ev in parse_events(_read(path))]
        return sorted(texts, key=len, reverse=True)[:SAMPLE_COUNT]

    def chunks(self, path, budget, contacts=None):
        from .. import trends
        events = parse_events(_read(path))
        yield from build(events, budget, contacts, str(path))
        yield from trend_chunks(events, budget, trends.zone())


def created_time(ev):
    raw = ev.get("created") or ""
    try:
        return dt.datetime.strptime(raw[:15], "%Y%m%dT%H%M%S").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def trend_chunks(events, budget, tz):
    """One rollup per year: volume, all-day share, recurring events, busiest
    month, and how far ahead events were created. See trends.py."""
    import statistics
    from .. import trends
    timed = [(event_time(ev), ev) for ev in events]
    timed = [(w, a, ev) for (w, a), ev in timed if w is not None]
    at = lambda item: item[0]
    for year, items in trends.by_year(timed, at, tz).items():
        all_day = sum(1 for _, a, _ in items if a)
        recurring = sorted({f"{_oneline(ev['summary'])} ({_repeat(ev)})"
                            for _, _, ev in items if _repeat(ev)})
        leads = []
        for when, _, ev in items:
            made = created_time(ev)
            if made:
                leads.append(max((when - made).days, 0))
        lines = [
            f"{len(items):,} events, {all_day:,} all-day, {len(items) - all_day:,} timed.",
            trends.describe_months(trends.month_counts(items, at, tz)),
            "Recurring: " + (", ".join(recurring) if recurring else "none"),
        ]
        if leads:
            same = sum(1 for d in leads if d == 0)
            week = sum(1 for d in leads if 1 <= d <= 7)
            more = len(leads) - same - week
            lines.append(
                f"How far ahead events were created: median lead time "
                f"{statistics.median(leads):.0f} days; same day: {same}, "
                f"within a week: {week}, more than a week: {more}.")
        yield from trends.chunks("calendar", year, lines, budget, tz)
