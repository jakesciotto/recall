"""Google Takeout My Activity: what you searched for, visited, and watched.

One file per product, HTML unless JSON was chosen at export time. Every
entry is a verb, a title, and a timestamp. Entries roll up to one chunk per
product per month with each day's entries listed inside, so "what was I
researching in March 2021" and "when did I first look up X" both work.
URLs and locations never reach a chunk: the titles carry the meaning, and
the export's map links are a location trail. See docs/sources.md.
"""

import collections
import datetime as dt
import html
import json
import re
from urllib.parse import urlsplit

from .. import chunking, trends
from .base import Chunk, Source, walk

FILES = {"MyActivity.html", "MyActivity.json",
         "watch-history.html", "watch-history.json",
         "search-history.html", "search-history.json"}

# The verb Takeout writes ahead of a title. HTML separates it from the link;
# JSON runs the two together, so the longest match is peeled off the front.
VERBS = sorted((
    "Searched for", "Visited", "Watched", "Viewed", "Used", "Defined",
    "Shared", "Liked", "Disliked", "Subscribed to", "Unsubscribed from",
    "Listened to", "Opened", "Clicked", "Ran", "Asked", "Played", "Saved",
    "Called", "Navigated to", "Explored", "Read", "Installed", "Updated",
    "Bought", "Rated", "Commented on", "Posted", "Answered", "Returned to",
), key=len, reverse=True)

# Takeout stamps every entry in the account's current zone by abbreviation.
# The common ones map to a fixed offset; anything else is read in the
# configured zone.
_OFFSETS = {"UTC": 0, "GMT": 0, "Z": 0,
            "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6,
            "PST": -8, "PDT": -7, "AKST": -9, "AKDT": -8, "HST": -10,
            "AST": -4, "ADT": -3, "BST": 1, "WET": 0, "WEST": 1, "CET": 1,
            "CEST": 2, "EET": 2, "EEST": 3, "MSK": 3, "JST": 9, "KST": 9,
            "AEST": 10, "AEDT": 11, "NZST": 12, "NZDT": 13}

_CELL = re.compile(r'<div class="content-cell mdl-cell mdl-cell--6-col '
                   r'mdl-typography--body-1">(.*?)</div>', re.S)
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r"<a [^>]*>(.*?)</a>", re.S)
_STAMP = re.compile(r"([A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2}:\d{2} [AP]M)"
                    r"\s*([A-Za-z]{1,5})?")


def _clean(fragment):
    text = html.unescape(_TAG.sub("", fragment))
    return text.replace("\xa0", " ").replace(" ", " ").strip()


def tidy_title(title):
    """A bare URL keeps its host and path. The query is tokens and noise."""
    if not re.match(r"https?://", title):
        return title
    parts = urlsplit(title)
    host = parts.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return (host + parts.path).rstrip("/")


def split_verb(title):
    for verb in VERBS:
        if title.startswith(verb + " "):
            return verb, title[len(verb) + 1:].strip()
    return "", title.strip()


def to_utc(stamp):
    """ISO 8601 UTC from "Feb 16, 2024, 9:50:17 PM MDT", or None."""
    m = _STAMP.search(stamp.replace(" ", " ").replace("\xa0", " "))
    if not m:
        return None
    when = dt.datetime.strptime(m.group(1), "%b %d, %Y, %I:%M:%S %p")
    zone = m.group(2)
    if zone in _OFFSETS:
        when = when - dt.timedelta(hours=_OFFSETS[zone])
    else:
        when = when.replace(tzinfo=trends.zone()).astimezone(
            dt.timezone.utc).replace(tzinfo=None)
    return when.isoformat() + "Z"


def parse_html(text):
    """[{verb, title, extra, at}] in file order. Only the entry cell is
    read; the caption cell beside it holds products and map links."""
    out = []
    for body in _CELL.findall(text):
        raw = [s for s in body.split("<br>") if _clean(s)]
        stamped = [(i, to_utc(_clean(s))) for i, s in enumerate(raw)]
        stamped = [(i, at) for i, at in stamped if at and i > 0]
        if not stamped:
            continue
        when, at = stamped[-1]
        first = raw[0]
        link = _LINK.search(first)
        if link:
            verb = _clean(first[:link.start()])
            title = _clean(link.group(1))
        else:
            verb, title = split_verb(_clean(first))
        title = tidy_title(title)
        details = [_clean(s) for i, s in enumerate(raw) if i not in (0, when)]
        extra = " ".join(d for d in details if not d.endswith(title))
        out.append({"verb": verb, "title": title, "extra": extra, "at": at})
    return out


def parse_json(text):
    out = []
    for item in json.loads(text):
        stamp = item.get("time")
        if not stamp:
            continue
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if when.tzinfo:
            when = when.astimezone(dt.timezone.utc).replace(tzinfo=None)
        verb, title = split_verb(item.get("title", ""))
        title = tidy_title(title)
        extra = " ".join(s.get("name", "") for s in item.get("subtitles", [])
                         if s.get("name"))
        out.append({"verb": verb, "title": title, "extra": extra,
                    "at": when.replace(microsecond=0).isoformat() + "Z"})
    return out


def product_of(path):
    """The product a file records: the folder name for My Activity, and
    the history kind for the YouTube export's own files."""
    if path.name.startswith("watch-history"):
        return "YouTube watch"
    if path.name.startswith("search-history"):
        return "YouTube search"
    return path.parent.name


def _slug(product):
    return re.sub(r"[^a-z0-9]+", "-", product.lower()).strip("-")


def _local(at, tz):
    return dt.datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(tz)


def _line(verb, title, extra, hhmm, n):
    text = " ".join(p for p in (hhmm, verb, title) if p)
    if extra:
        text += f" [{extra}]"
    if n > 1:
        text += f" (x{n})"
    return "  " + text


class Activity(Source):
    name = "activity"

    def detect(self, root):
        return [p for p in walk(root) if p.name in FILES]

    def samples(self, path):
        return []          # rollup chunks are split to the budget

    def chunks(self, path, budget, contacts=None):
        product = product_of(path)
        slug = _slug(product)
        text = path.read_text(encoding="utf-8", errors="replace")
        records = parse_json(text) if path.suffix == ".json" else parse_html(text)
        tz = trends.zone()
        zone_name = getattr(tz, "key", None) or str(tz)
        at = lambda r: r["at"]

        months = chunking.rollup(
            records, period=lambda r: _local(r["at"], tz).strftime("%Y-%m"))
        for month, group in months:
            days = collections.defaultdict(dict)
            for r in sorted(group, key=at):
                local = _local(r["at"], tz)
                entry = days[local.strftime("%Y-%m-%d")].setdefault(
                    (r["verb"], r["title"], r["extra"]),
                    [local.strftime("%H:%M"), 0])
                entry[1] += 1
            lines = [f"{len(group):,} events on {len(days)} days."]
            for day, entries in sorted(days.items()):
                lines.append(day)
                lines.extend(_line(*key, *val) for key, val in entries.items())
            head = lambda part: (f"[{month}, {product} activity{part}, "
                                 f"times in {zone_name}]")
            for suffix, body in chunking.parts(lines, budget, head):
                yield Chunk(
                    ref=f"activity:{slug}:{month}{suffix}",
                    text=body,
                    source=self.name,
                    occurred_at=f"{month}-01T00:00:00Z",
                    date_confidence="period",
                )

        years = trends.by_year(records, at, tz)
        for year, items in years.items():
            yield from self._trends(slug, year, items, at, budget, tz)
        if years:
            yield from self._trends(slug, None, records, at, budget, tz,
                                    first_year=min(years))

    def _trends(self, slug, year, items, at, budget, tz, first_year=None):
        titles = collections.Counter(
            " ".join(p for p in (r["verb"], r["title"]) if p) for r in items)
        lines = [
            f"{len(items):,} events.",
            "When: " + trends.describe_hours(items, at, tz) + ".",
            trends.describe_weekdays(trends.weekday_counts(items, at, tz)),
            trends.describe_months(trends.month_counts(items, at, tz)),
            "Most repeated: " + ", ".join(
                f"{t} ({n})" for t, n in trends.top(titles)) + ".",
        ]
        source = f"activity:{slug}"
        made = (trends.chunks(source, year, lines, budget, tz) if year
                else trends.all_time_chunks(source, first_year, lines, budget, tz))
        for c in made:
            c.source = self.name
            yield c
