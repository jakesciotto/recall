"""Counts from the structured store, so an aggregate is a source.

"How many texts have I sent to Ada over time" has no answer in eight
retrieved chunks, and a model that tries over-claims. The store holds one
row per message, this module runs a fixed catalogue of queries over it,
and the rendered table joins the prompt as a numbered source. The model
cites it or declines, like any other source.

Code owns every query and binds every argument. The planner, a model,
fills a small form from the catalogue; nothing it writes reaches SQL as
text. See docs/answering.md.
"""

import collections
import datetime as dt
import hashlib
import json
import re
import time
import unicodedata
import urllib.parse

from . import config, contacts, db, retrieve, trends

SOURCES = ("messages", "email", "twitter", "tweets")
# A source whose every event is the user's own, with no other side.
OWN = {"tweets": "tweets"}
# A five or six digit sender is a bank or a 2FA service, never a person.
SHORT_CODE = r"^[0-9]{1,6}$"
PERIODS = ("year", "month", "all")
DIRECTIONS = ("sent", "received", "all")
EXCLUDED = ("tapback", "system", "empty")
MAX_N = 25
MAX_CALLS = 3
MAX_PERSON = 200
MAX_CANDIDATES = 10
YEARS = (1990, 2100)
MIN_GAP_DAYS = 30          # a gap is listed for a person with this many active days
CONTACTS = "contacts"      # the alias source the ingest writes the vCard map under

CATALOGUE = {
    "count": (("source", SOURCES), ("with", "person"), ("by", PERIODS)),
    "top": (("source", SOURCES), ("year", "year"), ("direction", DIRECTIONS),
            ("n", "n")),
    "streaks": (("source", SOURCES), ("n", "n")),
}
DEFAULTS = {"source": "messages", "with": None, "by": "year", "year": None,
            "direction": "all", "n": 5}

Plan = collections.namedtuple("Plan", "calls raw ms error")


# ---- the form ---------------------------------------------------------------

def _year(value):
    if value is None or value == "":
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ValueError(value)
    if year != float(value) or not YEARS[0] <= year <= YEARS[1]:
        raise ValueError(value)
    return year


def _n(value):
    n = int(value)
    if n != float(value) or not 1 <= n <= MAX_N:
        raise ValueError(value)
    return n


def _person(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(value)
    value = value.strip()
    if len(value) > MAX_PERSON:
        raise ValueError(value)
    return value or None


def validate(call):
    """The call with every value checked and every default filled, or None.

    A call outside the catalogue is None, whoever wrote it: an unknown
    metric, an unknown key, or a value outside its set. Nothing past
    this point trusts the input again.
    """
    if not isinstance(call, dict) or call.get("metric") not in CATALOGUE:
        return None
    shape = CATALOGUE[call["metric"]]
    if set(call) - {"metric"} - {key for key, _ in shape}:
        return None
    out = {"metric": call["metric"]}
    try:
        for key, kind in shape:
            value = call.get(key, DEFAULTS[key])
            if isinstance(kind, tuple):
                if value not in kind:
                    return None
            else:
                value = {"person": _person, "year": _year, "n": _n}[kind](value)
            out[key] = value
    except (TypeError, ValueError):
        return None
    return out


def ref(call, text, window=None):
    """stats:<metric>?<args>#<digest>. The digest is of the rendered text,
    so the judge reads what the answerer read, and an unchanged store
    writes no second row."""
    args = {k: v for k, v in call.items() if k != "metric" and v is not None}
    if window:
        args["since"], args["until"] = window
    query = urllib.parse.urlencode(sorted(args.items()))
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"stats:{call['metric']}?{query}#{digest}"


# ---- people -----------------------------------------------------------------

def labels(conn, source):
    """{identifier: label} for a source: the vCard map the ingest stored
    under CONTACTS, and the aliases the export itself carries."""
    rows = db.fetch(conn, "SELECT handle, label FROM alias "
                          "WHERE source IN (%s, %s)", [source, CONTACTS])
    return {r["handle"]: r["label"] for r in rows}


_APOSTROPHES = str.maketrans("", "", "'\u2019")


def fold(text):
    text = unicodedata.normalize("NFKD", text or "").translate(_APOSTROPHES)
    kept = "".join(ch if ch.isalnum() else " " for ch in text
                   if not unicodedata.combining(ch))
    return " ".join(kept.casefold().split())


def names(labels):
    folded = {}
    for handle, text in labels.items():
        key = fold(text)
        if key:
            shown, handles = folded.setdefault(key, (set(), set()))
            shown.add(text)
            handles.add(handle)
    return {key: (" / ".join(sorted(shown)), sorted(handles))
            for key, (shown, handles) in folded.items()}


def resolve(name, labels, source):
    """(handles, label, candidates) for a person named in a question.

    A name owns every identifier mapped to it. Names compare folded: case,
    accents, emoji and punctuation do not count. An exact folded name
    first, then a name with a word that starts with the question's name;
    several such names is a question back to the asker, never a guess. A
    raw identifier passes through: a phone number is normalised the way
    the store holds it, except for twitter, where a run of digits is an
    account id.
    """
    want = (name or "").strip()
    low = want.lower()
    named = names(labels)
    key = fold(want)
    if key in named:
        shown, handles = named[key]
        return handles, shown, []
    by_label = collections.defaultdict(list)
    shown = {}
    for handle, text in labels.items():
        by_label[text.lower()].append(handle)
        shown.setdefault(text.lower(), text)
    if low in by_label:
        return sorted(by_label[low]), shown[low], []
    if low in labels or low in {h.lower() for h in labels}:
        handle = next(h for h in labels if h.lower() == low)
        return [handle], labels[handle], []
    if "@" in low:
        return [low], labels.get(low, low), []
    if source == "twitter" and low.isdigit():
        return [low], labels.get(low, low), []
    phone = contacts.normalize_phone(want) if re.search(r"\d{7}", re.sub(r"\D", "", want)) else None
    if phone:
        return [phone], labels.get(phone, phone), []
    start = re.compile(r"(?:^| )" + re.escape(key)) if key else None
    hits = sorted(k for k in named if start and start.search(k))
    if len(hits) == 1:
        shown, handles = named[hits[0]]
        return handles, shown, []
    return [], want, [named[k][0] for k in hits]


# ---- folds ------------------------------------------------------------------

def longest_gap(days):
    """(silent days, the active day before, the active day after)."""
    ordered = sorted(days)
    best = (0, None, None)
    for a, b in zip(ordered, ordered[1:]):
        silent = (b - a).days - 1
        if silent > best[0]:
            best = (silent, a, b)
    return best


def _day(value):
    return dt.date.fromisoformat(str(value)[:10])


# ---- queries ----------------------------------------------------------------

_PERIOD = {
    "year": "to_char(e.occurred_at AT TIME ZONE %(tz)s, 'YYYY')",
    "month": "to_char(e.occurred_at AT TIME ZONE %(tz)s, 'YYYY-MM')",
    "all": "'all'",
}
_COUNTED = "e.kind <> ALL(%(excluded)s::text[])"
_RANK = {"sent": "sent", "received": "received", "all": "sent + received"}

_WITH = """
WITH t AS (
  SELECT thread, direct
  FROM thread WHERE source = %(source)s AND members && %(handles)s::text[]
)"""

_COUNT_WITH = _WITH + f"""
SELECT {{period}} AS period,
       count(*) FILTER (WHERE {_COUNTED} AND e.mine AND t.direct) AS sent_direct,
       count(*) FILTER (WHERE {_COUNTED} AND NOT e.mine AND t.direct) AS received_direct,
       count(*) FILTER (WHERE {_COUNTED} AND e.mine AND NOT t.direct) AS sent_group,
       count(*) FILTER (WHERE {_COUNTED} AND NOT e.mine AND NOT t.direct
                        AND e.sender = ANY(%(handles)s::text[])) AS received_group,
       count(*) FILTER (WHERE NOT {_COUNTED}) AS excluded
FROM event e JOIN t ON t.thread = e.thread
WHERE e.source = %(source)s{{bounds}}
GROUP BY 1 ORDER BY 1"""

_DAYS_WITH = _WITH + f"""
SELECT DISTINCT (e.occurred_at AT TIME ZONE %(tz)s)::date AS day
FROM event e JOIN t ON t.thread = e.thread
WHERE e.source = %(source)s AND {_COUNTED}
  AND (t.direct OR e.sender = ANY(%(handles)s::text[])){{bounds}}
ORDER BY 1"""

_COUNT_ALL = f"""
SELECT {{period}} AS period,
       count(*) FILTER (WHERE {_COUNTED} AND e.mine) AS sent,
       count(*) FILTER (WHERE {_COUNTED} AND NOT e.mine) AS received,
       count(*) FILTER (WHERE NOT {_COUNTED}) AS excluded
FROM event e
WHERE e.source = %(source)s{{bounds}}
GROUP BY 1 ORDER BY 1"""

_DAYS_ALL = f"""
SELECT DISTINCT (e.occurred_at AT TIME ZONE %(tz)s)::date AS day
FROM event e
WHERE e.source = %(source)s AND {_COUNTED}{{bounds}}
ORDER BY 1"""

# Members resolve to their label inside the query, so a person with a
# phone and an email in one group is one member, counted once. A sender
# resolves the same way, so received matches by person, not by handle.
_MEMBERS = """
WITH m AS (
  SELECT DISTINCT t.thread, coalesce(a.label, h) AS who, t.direct
  FROM thread t
  CROSS JOIN unnest(t.members) AS h
  LEFT JOIN alias a ON a.handle = h AND a.source IN (t.source, %(contacts)s)
  WHERE t.source = %(source)s AND h !~ %(short_code)s
),
s AS (
  SELECT e.*, coalesce(a.label, e.sender) AS who
  FROM event e
  LEFT JOIN alias a ON a.handle = e.sender AND a.source IN (e.source, %(contacts)s)
  WHERE e.source = %(source)s
)"""

# The rank is an expression over output columns, which ORDER BY accepts
# only on a subquery's columns, never on aliases of the same SELECT.
_TOP = _MEMBERS + f"""
SELECT * FROM (
  SELECT m.who,
         count(*) FILTER (WHERE e.mine AND m.direct) AS sent,
         count(*) FILTER (WHERE NOT e.mine AND e.who = m.who) AS received,
         count(*) FILTER (WHERE e.mine AND NOT m.direct) AS groups
  FROM s e JOIN m ON m.thread = e.thread
  WHERE {_COUNTED}{{bounds}}
  GROUP BY 1
) ranked ORDER BY {{rank}} DESC, who LIMIT %(limit)s"""

_BOUNDS = " AND e.occurred_at >= %(since)s AND e.occurred_at < %(until)s"

_CANDIDATES = f"""
WITH c AS (
  SELECT * FROM unnest(%(shown)s::text[], %(handles)s::text[]) AS c(label, handle)
),
t AS (
  SELECT DISTINCT c.label, th.thread
  FROM c JOIN thread th ON th.source = %(source)s AND th.members @> ARRAY[c.handle]
)
SELECT t.label, count(*) AS messages
FROM t JOIN event e ON e.source = %(source)s AND e.thread = t.thread
WHERE {_COUNTED}
GROUP BY 1"""

_END = ("SELECT (max(occurred_at) AT TIME ZONE %(tz)s)::date AS day "
        "FROM event WHERE source = %(source)s")

_STREAK_DAYS = _MEMBERS + f"""
SELECT m.who, (e.occurred_at AT TIME ZONE %(tz)s)::date AS day
FROM s e JOIN m ON m.thread = e.thread
WHERE {_COUNTED} AND (m.direct OR e.who = m.who)
GROUP BY 1, 2"""


def _zone_name(tz):
    return getattr(tz, "key", None) or str(tz)


def _base(call, labels):
    return {"call": call, "label": None, "handles": [], "candidates": [],
            "absent": [], "window": None}


def _window(dates):
    if dates is None or not dates.since:
        return None
    return dates.since, dates.until


def _local(day, tz):
    start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), tzinfo=tz)
    return start.astimezone(dt.timezone.utc).isoformat()


def _store_end(conn, params):
    end = db.fetch(conn, _END, params)
    return _day(end[0]["day"]) if end and end[0].get("day") else None


def _clip(window, end):
    """The window stopped at the day after the store end when the question
    asked past it, with the end the question asked for; else the window
    and None. A table bounded to today over an export copied in August
    reads as an answer about today, and nothing in it says otherwise."""
    if not window or not end:
        return window, None
    stop = (end + dt.timedelta(days=1)).isoformat()
    if window[0] < stop < window[1]:
        return (window[0], stop), window[1]
    return window, None


def _narrow(conn, source, asked, candidates, labels):
    by_shown = {shown: handles for shown, handles in names(labels).values()}
    pairs = [(c, h) for c in candidates for h in by_shown.get(c, [])]
    rows = db.fetch(conn, _CANDIDATES,
                    {"source": source, "excluded": list(EXCLUDED),
                     "shown": [c for c, _ in pairs], "handles": [h for _, h in pairs]})
    counts = {r["label"]: int(r["messages"] or 0) for r in rows}
    live = sorted((c for c in candidates if counts.get(c)),
                  key=lambda c: (-counts[c], c))
    absent = [c for c in candidates if not counts.get(c)]
    if len(live) == 1:
        return by_shown[live[0]], live[0], [], absent
    return [], asked, [{"label": c, "messages": counts[c]} for c in live], absent


def _key(day, by):
    return {"year": f"{day.year}", "month": f"{day.year}-{day.month:02d}",
            "all": "all"}[by]


def _count(conn, call, labels, tz, dates=None):
    out = _base(call, labels)
    params = {"source": call["source"], "tz": _zone_name(tz),
              "excluded": list(EXCLUDED)}
    if call["with"]:
        handles, shown, candidates = resolve(call["with"], labels, call["source"])
        absent = []
        if candidates:
            handles, shown, candidates, absent = _narrow(
                conn, call["source"], shown, candidates, labels)
        out.update(label=shown, handles=handles, candidates=candidates,
                   absent=absent)
        if not handles:
            return out
        params["handles"] = handles
        rows_sql, days_sql = _COUNT_WITH, _DAYS_WITH
    else:
        rows_sql, days_sql = _COUNT_ALL, _DAYS_ALL
    out["end"] = _store_end(conn, params)
    bounds = ""
    window, asked = _clip(_window(dates), out["end"])
    if window:
        params["since"], params["until"] = (_local(d, tz) for d in window)
        bounds = _BOUNDS
        out["window"] = window
        out["asked_until"] = asked
    out["rows"] = db.fetch(conn, rows_sql.format(period=_PERIOD[call["by"]],
                                                 bounds=bounds), params)
    days = [_day(r["day"]) for r in db.fetch(conn, days_sql.format(bounds=bounds),
                                             params)]
    out["first"] = days[0] if days else None
    out["last"] = days[-1] if days else None
    out["days"] = len(days)
    out["active"] = dict(collections.Counter(_key(d, call["by"]) for d in days))
    out["streak"] = trends.longest_streak(days)
    out["gap"] = longest_gap(days)
    out["excluded"] = sum(int(r.get("excluded") or 0) for r in out["rows"])
    return out


def _bounds(year, tz):
    since = dt.datetime(year, 1, 1, tzinfo=tz).astimezone(dt.timezone.utc)
    until = dt.datetime(year + 1, 1, 1, tzinfo=tz).astimezone(dt.timezone.utc)
    return since.isoformat(), until.isoformat()


def _top(conn, call, labels, tz, dates=None):
    out = _base(call, labels)
    params = {"source": call["source"], "excluded": list(EXCLUDED),
              "contacts": CONTACTS, "short_code": SHORT_CODE, "limit": call["n"],
              "tz": _zone_name(tz)}
    bounds = ""
    window = _window(dates)
    if call["year"]:
        params["since"], params["until"] = _bounds(call["year"], tz)
        bounds = _BOUNDS
    elif window:
        out["end"] = _store_end(conn, params)
        window, out["asked_until"] = _clip(window, out["end"])
        params["since"], params["until"] = (_local(d, tz) for d in window)
        bounds = _BOUNDS
        out["window"] = window
    rows = db.fetch(conn, _TOP.format(bounds=bounds, rank=_RANK[call["direction"]]),
                    params)
    out["rows"] = [{"label": r["who"], "sent": int(r["sent"] or 0),
                    "received": int(r["received"] or 0),
                    "groups": int(r["groups"] or 0)} for r in rows]
    return out


def _streaks(conn, call, labels, tz, dates=None):
    out = _base(call, labels)
    params = {"source": call["source"], "tz": _zone_name(tz),
              "excluded": list(EXCLUDED), "contacts": CONTACTS,
              "short_code": SHORT_CODE}
    days = collections.defaultdict(set)
    for r in db.fetch(conn, _STREAK_DAYS, params):
        days[r["who"]].add(_day(r["day"]))
    people = [{"label": who, "days": len(d), "streak": trends.longest_streak(d),
               "gap": longest_gap(d)} for who, d in days.items()]
    out["streaks"] = sorted(people, key=lambda p: (-p["streak"][0], p["label"]))[:call["n"]]
    out["gaps"] = sorted((p for p in people if p["days"] >= MIN_GAP_DAYS),
                         key=lambda p: (-p["gap"][0], p["label"]))[:call["n"]]
    return out


_RUN = {"count": _count, "top": _top, "streaks": _streaks}


def run(conn, call, labels, tz, dates=None):
    """The result of one call. Raises on a call outside the catalogue
    before any query runs. The question's dates bound a count, and a top
    call that names no year."""
    clean = validate(call)
    if clean is None:
        raise ValueError(f"not a call in the catalogue: {call!r}")
    return _RUN[clean["metric"]](conn, clean, labels, tz, dates=dates)


def ask_back(result):
    call = result["call"]
    if call["metric"] != "count" or not call.get("with") or result["handles"]:
        return None
    asked, source = call["with"], call["source"]
    candidates = result.get("candidates") or []
    if candidates:
        shown = candidates[:MAX_CANDIDATES]
        more = len(candidates) - len(shown)
        listed = ", ".join(f"{c['label']} ({c['messages']:,} messages)" for c in shown)
        return (f'"{asked}" matches several contacts in {source}: {listed}'
                + (f", and {more} more" if more > 0 else "")
                + ". Each number counts the messages in threads that include "
                "that contact. Ask again with one of these names.")
    if result.get("absent"):
        return (f'"{asked}" matches {", ".join(result["absent"])}, and none of '
                f"them has a message in {source}.")
    return (f'No contact or identifier matches "{asked}" in {source}. Ask again '
            "with a full name, a phone number or an email address.")


# ---- rendering --------------------------------------------------------------

def _span(triple, unit):
    n, a, b = triple
    if not n:
        return f"0 {unit}s"
    return f"{n:,} {unit}{'s' if n != 1 else ''} ({a} to {b})"


def _table(columns, rows):
    lines = [" | ".join(columns)]
    for row in rows:
        lines.append(" | ".join(f"{row[c]:,}" if isinstance(row[c], int)
                                else str(row[c]) for c in columns))
    return "\n".join(lines)


def render(result, today=None, tz=None):
    """The text the model reads. Every number travels with its definition,
    because a count is only as honest as the sentence that says what it
    counted."""
    call = result["call"]
    today = today or dt.date.today()
    zone = _zone_name(tz or trends.zone())
    source = call["source"]
    who = result.get("label")
    window = result.get("window")
    span = f", from {window[0]} until {window[1]}" if window else ""
    if call["metric"] == "count":
        head = (f"count, {source}" + (f" with {who}" if call.get("with") else "")
                + f", by {call['by']}{span}")
    elif call["metric"] == "top":
        head = (f"top {call['n']}, {source}"
                + (f", {call['year']}" if call.get("year") else span or ", all years")
                + f", by {call['direction']}")
    else:
        head = f"streaks, {source}"
    lines = [f"[stats: {head}, times in {zone}]",
             f"Computed on {today} from the {source} store, which holds one row "
             f"per {OWN.get(source, 'message')[:-1] if source in OWN else 'message'} "
             f"in the export. Counts are exact. Tapbacks, system rows and empty "
             f"rows are not counted."]
    if window:
        lines.append(f"The question's dates bound this table: from {window[0]} up to, "
                     f"not including, {window[1]}, in local days.")
        if result.get("asked_until"):
            lines.append(f"The question asked until {result['asked_until']}, and the "
                         f"{source} store ends on {result['end']}: the export holds "
                         f"nothing later, so the range stops there. Days after "
                         f"{result['end']} are missing from the export, not silent.")
    if call["metric"] == "count" and call.get("with") and not result["handles"]:
        lines.append(ask_back(result) + " This call counted nothing.")
        return "\n".join(lines)
    if result.get("absent"):
        lines.append(f'"{call["with"]}" also matches {", ".join(result["absent"])}, '
                     f"with no message in {source}.")
    if call["metric"] == "count" and call.get("with") and not result["rows"]:
        lines.append(f"No messages with {who} in {source}: the store holds no "
                     f"message in any thread that includes {who}. A message in "
                     f"another source is not a {source} message.")
        return "\n".join(lines)
    if call["metric"] == "count":
        rows = result["rows"]
        active = result.get("active") or {}
        if call.get("with"):
            lines.append(f"With means a thread that includes {who}. Direct is the "
                         f"one-to-one thread; group is any thread with more members. "
                         f"Received in a group counts {who}'s own messages there.")
            sent = sum(r["sent_direct"] + r["sent_group"] for r in rows)
            received = sum(r["received_direct"] + r["received_group"] for r in rows)
            columns = ("period", "sent direct", "received direct", "sent group",
                       "received group", "active days")
            keyed = [{"period": r["period"], "sent direct": r["sent_direct"],
                      "received direct": r["received_direct"],
                      "sent group": r["sent_group"],
                      "received group": r["received_group"],
                      "active days": active.get(r["period"], 0)} for r in rows]
            day_rule = (f"A day counts when either of you wrote in the direct "
                        f"thread or {who} wrote in a group.")
        elif source in OWN:
            noun = OWN[source]
            sent = sum(r["sent"] for r in rows)
            received = 0
            columns = ("period", noun, "active days")
            keyed = [{"period": r["period"], noun: r["sent"],
                      "active days": active.get(r["period"], 0)} for r in rows]
            day_rule = f"A day counts when you posted. Retweets count as {noun}."
        else:
            sent = sum(r["sent"] for r in rows)
            received = sum(r["received"] for r in rows)
            columns = ("period", "sent", "received", "active days")
            keyed = [{"period": r["period"], "sent": r["sent"],
                      "received": r["received"],
                      "active days": active.get(r["period"], 0)} for r in rows]
            day_rule = "A day counts when any message was sent or received."
        total = (f"Total {sent:,} {OWN[source]}" if source in OWN else
                 f"Total {sent + received:,}: sent {sent:,}, received {received:,}")
        # One fact per line. A 26B model declined a question whose answer
        # sat mid-sentence in a run-on line of five facts.
        lines.append(f"{total}; excluded {result['excluded']:,}.")
        lines.append(f"First {result['first']}, last {result['last']}."
                     if result["first"] else "No dated messages.")
        if result.get("end"):
            lines.append(f"The {source} store ends on {result['end']}: the export "
                         f"holds nothing later.")
        days = result.get("days") or 0
        if result["first"]:
            calendar = (result["last"] - result["first"]).days + 1
            lines.append(f"Active days {days:,} of the {calendar:,} days from first "
                         f"to last. {day_rule}")
        else:
            lines.append(f"Active days 0. {day_rule}")
        lines.append(f"Longest daily streak {_span(result['streak'], 'day')}.")
        lines.append(f"Longest gap {_span(result['gap'], 'silent day')}.")
        # One period is two sentences, not a one-row table: a model once
        # crossed the columns of such a table.
        if call["by"] == "all" and len(rows) == 1:
            r = rows[0]
            if call.get("with"):
                lines.append(f"Direct thread: sent {r['sent_direct']:,}, received "
                             f"{r['received_direct']:,}. Group threads: sent "
                             f"{r['sent_group']:,}, received {r['received_group']:,}.")
            elif source in OWN:
                lines.append(f"{r['sent']:,} {OWN[source]}.")
            else:
                lines.append(f"Sent {r['sent']:,}, received {r['received']:,}.")
        else:
            lines.append(_table(columns, keyed) if keyed else "No rows.")
    elif call["metric"] == "top":
        lines.append("Sent is the user's messages in the one-to-one thread with "
                     "the person; received is the person's messages anywhere; "
                     "groups is the user's messages in group threads the person "
                     "is in. The rank follows the chosen direction, sent plus "
                     "received for all.")
        lines.append(_table(("person", "sent", "received", "groups"),
                            [{"person": r["label"], "sent": r["sent"],
                              "received": r["received"], "groups": r["groups"]}
                             for r in result["rows"]])
                     if result["rows"] else "No rows.")
    else:
        lines.append("A streak is consecutive local days with a message in the "
                     "one-to-one thread or from the person in any thread. A gap "
                     "is the silent days between two such days, listed for a "
                     f"person with {MIN_GAP_DAYS} active days or more.")
        lines.append("Longest streaks:\n" + "\n".join(
            f"{p['label']} | {_span(p['streak'], 'day')} | {p['days']:,} active days"
            for p in result["streaks"]) if result["streaks"] else "No streaks.")
        lines.append("Longest gaps:\n" + "\n".join(
            f"{p['label']} | {_span(p['gap'], 'silent day')} | {p['days']:,} active days"
            for p in result["gaps"]) if result["gaps"] else "No gaps to list.")
    return "\n".join(lines)


# ---- the planner ------------------------------------------------------------

PLANNER = """You turn a question about a personal archive into calls on its structured store, or decline.

The store counts messages (Apple Messages: texts and iMessages), email, twitter (direct messages), and tweets (the user's own tweets, with no other person). Each call is one JSON object:
- {"metric": "count", "source": "messages|email|twitter|tweets", "with": "<person's name as written in the question, or null>", "by": "year|month|all"}: how many were sent and received, per period, in threads with the person, or in every thread when with is null, with the number of active days. Use it for how many, how often, on how many days, over time, did I ever message, when did we start or stop, first or last message with someone. Code applies any dates in the question to the count, so never leave out a count because the question names dates.
- {"metric": "top", "source": "messages|email|twitter", "year": <four digit year, or null for all years>, "direction": "sent|received|all", "n": <1 to 25>}: the people with the most messages. Use it for who did I message most, top contacts, who filled my inbox, who sent me the most.
- {"metric": "streaks", "source": "messages|email|twitter", "n": <1 to 25>}: the longest daily streaks and the longest gaps per person. Use it for longest streak, longest unbroken run, longest gap or silence.

Rules:
1. Answer with a JSON array of at most 3 calls, or the word null. No prose, no code fence.
2. texts, iMessages or messages mean messages. mail or email means email. DMs or twitter means twitter. tweets or tweeting means tweets, and a tweets call never names a person. When the question names no channel, use messages.
3. over time means by year. A question that compares years makes one call per year.
4. Any question about how many, how often, on how many days, when first or last, which share in groups against one-to-one, or whether any message exists with a named person is a count call with that person. The count answers all of those at once.
5. A question about what was said, what a message or a document contains, or anything the store cannot count, is null.
6. A question that mixes a count with something the store cannot answer still gets the count call. Retrieval covers the rest.

Examples:
Question: how many texts have I sent to Ada over time
[{"metric": "count", "source": "messages", "with": "Ada", "by": "year"}]
Question: when did I first and last message Bo, and how many messages did we exchange?
[{"metric": "count", "source": "messages", "with": "Bo", "by": "all"}]
Question: how many messages have I exchanged with Bo in group chats compared with one-to-one?
[{"metric": "count", "source": "messages", "with": "Bo", "by": "all"}]
Question: did I ever message Ada?
[{"metric": "count", "source": "messages", "with": "Ada", "by": "all"}]
Question: since January 2019, on how many days did I text Bo?
[{"metric": "count", "source": "messages", "with": "Bo", "by": "all"}]
Question: whom did I text most in 2019, and whom in 2021?
[{"metric": "top", "source": "messages", "year": 2019, "direction": "all", "n": 3}, {"metric": "top", "source": "messages", "year": 2021, "direction": "all", "n": 3}]
Question: who sent me the most email in 2021?
[{"metric": "top", "source": "email", "year": 2021, "direction": "received", "n": 5}]
Question: which contact shows the longest unbroken messaging streak?
[{"metric": "streaks", "source": "messages", "n": 3}]
Question: when did I stop tweeting regularly, and what was my last tweet before the gap?
[{"metric": "count", "source": "tweets", "with": null, "by": "year"}]
Question: who filled my inbox the most in 2021, and what did they usually write about?
[{"metric": "top", "source": "email", "year": 2021, "direction": "received", "n": 5}]
Question: what did Ada say about the trip in 2018?
null
Question: show the email where Bo and I planned a dinner
null
"""

PLAN_TOKENS = 2000


def parse_plan(text):
    """The valid calls in a model's answer, at most MAX_CALLS. Raises
    ValueError on an answer that is not JSON and not null."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body[body.find("\n") + 1:] if "\n" in body else body
        body = body.strip().rstrip("`").strip()
    if body.lower() == "null" or not body:
        return []
    data = json.loads(body)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError(f"a plan is a list, got {type(data).__name__}")
    calls = [c for c in (validate(item) for item in data) if c]
    return calls[:MAX_CALLS]


def plan(question, chat=None):
    """Ask the model which calls answer the question. Never raises past
    the parse: an unusable answer is a Plan with an error, and the ask
    goes on without a table."""
    if chat is None:
        if not config.CHAT_URL:
            return Plan([], None, 0, None)
        from . import answer
        chat = answer.chat
    started = time.monotonic()
    raw = chat(f"Question: {question}", system=PLANNER, max_tokens=PLAN_TOKENS)
    ms = int((time.monotonic() - started) * 1000)
    try:
        return Plan(parse_plan(raw), raw, ms, None)
    except ValueError as e:
        return Plan([], raw, ms, f"ValueError: {e}")


def hit(conn, call, tz=None, today=None, dates=None):
    """One source: the call run, rendered and kept under its ref. A count
    whose person did not resolve also carries the ask-back under "ask",
    which gather takes off before the source reaches anyone."""
    tz = tz or trends.zone()
    result = run(conn, call, labels(conn, call["source"]), tz, dates=dates)
    text = render(result, today=today, tz=tz)
    key = ref(call, text, result.get("window"))
    db.save_computed(conn, key, text, call["source"])
    source = {"ref": key, "text": text, "occurred_at": None,
              "source": call["source"], "path": None, "score": 1.0}
    ask = ask_back(result)
    if ask:
        source["ask"] = ask
    return source


def gather(question, conn, embedder, k=retrieve.TOP_K, source=None, today=None,
           planner=None, tz=None):
    """Hybrid retrieval with any planned tables in front.

    Returns (hits, dates, trace) in the shape of retrieve.search_traced.
    A table takes one of the k slots, and the trace carries the plan and
    its time, so the query log can say what was asked of the store. A
    planner that raises, or a call that fails, costs nothing: the error
    goes into the trace and retrieval runs as it always did.
    """
    planner = planner or plan
    today = today or dt.date.today()
    dates = retrieve.parse_dates(question, today)
    try:
        asked = planner(question)
    except Exception as e:
        asked = Plan([], None, 0, f"{type(e).__name__}: {e}")
    errors = [asked.error] if asked.error else []
    extras, asks = [], []
    for call in asked.calls:
        try:
            table = hit(conn, call, tz=tz, today=today, dates=dates)
            ask = table.pop("ask", None)
            if ask:
                asks.append(f"{ask} [{len(extras) + 1}]")
            extras.append(table)
        except Exception as e:
            # A failed statement leaves the connection in an aborted
            # transaction, and every later statement would fail with it.
            try:
                conn.rollback()
            except Exception:
                pass
            errors.append(f"{type(e).__name__}: {e}")
    hits, dates, trace = retrieve.search_traced(
        question, conn, embedder, k=max(1, k - len(extras)), source=source,
        today=today, dates=dates)
    note = {"calls": asked.calls, "raw": asked.raw}
    if errors:
        note["error"] = "; ".join(errors)
    trace["ask_back"] = " ".join(asks) if asks else None
    if asks:
        note["ask_back"] = trace["ask_back"]
    trace["plan"] = json.dumps(note, default=str) if asked.calls or errors else None
    trace["plan_ms"] = asked.ms
    trace["fused"] = [(h["ref"], 1.0) for h in extras] + list(trace["fused"])
    trace["fused_n"] = len(trace["fused"])
    return extras + hits, dates, trace
