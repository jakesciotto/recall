"""Yearly trend rollups: the aggregates no single chunk can hold.

"On which day of the week did I text the most in 2023" cannot be answered
from eight retrieved chunks, and a model that tries is over-claiming. The
data is there, so each source rolls it up once a year into one chunk, the
way Spotify and Health roll up months. Retrieval then finds a table instead
of guessing from a sample.

Years, weekdays and hours are LOCAL. Stored timestamps are UTC, and a
message at 23:30 on New Year's Eve in Denver is 06:30 on 1 January in UTC.
RECALL_TZ names the zone; every rollup names it in its text, so an answer
can say which clock it is reading.
"""

import collections
import datetime as dt
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .chunking import parts
from .sources.base import Chunk

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")
TOP = 5

# Addresses that are almost never a person. A heuristic, and named as one in
# the rollup text.
_SERVICE = re.compile(
    r"^(no-?reply|donotreply|notifications?|auto-?confirm|newsletters?|news|"
    r"info|support|alerts?|updates?|billing|orders?|hello|team|marketing|"
    r"mailer|bounce|digest|help|noreply\+.*)@|@(t|e|em|mail|email|news|"
    r"info|reply|mailer)\.", re.I)


def zone():
    """The zone RECALL_TZ names, else the machine's, else UTC."""
    name = os.environ.get("RECALL_TZ", "").strip()
    if not name:
        try:
            link = os.readlink("/etc/localtime")
            name = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else ""
        except OSError:
            name = ""
    try:
        return ZoneInfo(name) if name else dt.timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return dt.timezone.utc


def _local(when, tz):
    if isinstance(when, (int, float)):
        when = dt.datetime.fromtimestamp(when, dt.timezone.utc)
    elif isinstance(when, str):
        when = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(tz)


def by_year(items, when, tz):
    out = collections.defaultdict(list)
    for item in items:
        stamp = when(item)
        if stamp is None:
            continue
        out[_local(stamp, tz).year].append(item)
    return dict(sorted(out.items()))


def weekday_counts(items, when, tz):
    counts = collections.Counter(
        WEEKDAYS[_local(when(i), tz).weekday()] for i in items if when(i) is not None)
    return dict(counts)


def month_counts(items, when, tz):
    return collections.Counter(_local(when(i), tz).month for i in items
                               if when(i) is not None)


def longest_streak(days):
    """(length, first day, last day) of the longest run of consecutive days."""
    if not days:
        return 0, None, None
    ordered = sorted(days)
    best = (1, ordered[0], ordered[0])
    start = ordered[0]
    for prev, cur in zip(ordered, ordered[1:]):
        if (cur - prev).days != 1:
            start = cur
        length = (cur - start).days + 1
        if length > best[0]:
            best = (length, start, cur)
    return best


def describe_hours(items, when, tz):
    """"Most between 20:00 and 22:59 (5 of 6)": the top hours as a range."""
    hours = collections.Counter(_local(when(i), tz).hour for i in items
                                if when(i) is not None)
    if not hours:
        return "no timed items"
    top = [h for h, _ in hours.most_common(3)]
    lo, hi = min(top), max(top)
    share = sum(hours[h] for h in top)
    total = sum(hours.values())
    return (f"most between {lo:02d}:00 and {hi:02d}:59 ({share} of {total}); "
            f"peak hour {hours.most_common(1)[0][0]:02d}:00")


def describe_weekdays(counts):
    if not counts:
        return "no items"
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    busiest, n = ordered[0]
    total = sum(counts.values())
    table = ", ".join(f"{d} {counts.get(d, 0):,}" for d in WEEKDAYS)
    return f"busiest day of the week: {busiest} ({n:,} of {total:,}). By day: {table}"


def describe_months(counts):
    if not counts:
        return "no items"
    month, n = counts.most_common(1)[0]
    return f"busiest month: {dt.date(2000, month, 1).strftime('%B')} ({n:,})"


def looks_like_service(sender):
    return bool(_SERVICE.search(sender or ""))


def top(counter, n=TOP):
    return counter.most_common(n)


def chunks(source, year, lines, budget, tz, participants=()):
    """One trends chunk for a year, split only if the budget forces it."""
    zone_name = getattr(tz, "key", None) or str(tz)
    head = lambda part: (f"[{year}, {source} trends{part}, times in {zone_name}]\n"
                         f"Yearly rollup computed from every {source} record "
                         f"in {year}. Counts are exact; the person-or-service "
                         f"label is a heuristic.")
    for suffix, text in parts(lines, budget, head):
        yield Chunk(
            ref=f"{source}:trends:{year}{suffix}",
            text=text,
            source=source,
            occurred_at=f"{year}-01-01T00:00:00Z",
            date_confidence="period",
            participants=list(participants),
        )


def all_time_chunks(source, first_year, lines, budget, tz, participants=()):
    """One rollup across every year, for the questions that name no year."""
    zone_name = getattr(tz, "key", None) or str(tz)
    head = lambda part: (f"[all years, {source} trends{part}, times in {zone_name}]\n"
                         f"All-time rollup computed from every {source} record. "
                         f"Counts are exact.")
    for suffix, text in parts(lines, budget, head):
        yield Chunk(
            ref=f"{source}:trends:all{suffix}",
            text=text,
            source=source,
            occurred_at=f"{first_year}-01-01T00:00:00Z",
            date_confidence="period",
            participants=list(participants),
        )
