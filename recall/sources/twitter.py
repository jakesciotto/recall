"""Twitter/X archive: tweets rolled up by day, direct messages by session.

One export holds two content shapes that need different handling. Tweets are
tiny, so they group by day. DMs are ordinary conversation, so they window like
any other chat. The export names DM senders by numeric id and ships no name
table. See docs/lessons.md.
"""

import collections
import datetime as dt
import json
import os
import pathlib
import re

from ..chunking import fit, parts, sessions
from .base import Chunk, Source, walk

# iMessage splits at 30 minutes. Twitter DMs are asynchronous, and at 30
# minutes 39 percent of sessions came out as one short message. A day puts the
# median DM chunk at 462 characters, matching the message chunks beside it.
DM_SESSION_GAP_S = 86_400
MAX_TURNS = 20

DM_FILES = ("direct-messages.js", "direct-messages-group.js")
MEDIA_DIRS = ("tweets_media", "direct_messages_media",
              "direct_messages_group_media")

# "<digits>-<hash>.<ext>". The hash itself can start with a dash, so anchor
# on the leading digit run rather than splitting on the separator.
_MEDIA_ID = re.compile(r"^(\d+)-")


def media_id(name):
    """The tweet or DM id a media file name carries, or None."""
    m = _MEDIA_ID.match(os.path.basename(name))
    return m.group(1) if m else None


def _iso(ts):
    return dt.datetime.fromtimestamp(
        ts, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_js(path):
    """Parse one export file.

    Twitter wraps valid JSON in a JavaScript assignment, so the file is not
    JSON. Split on the FIRST "=" only: a tweet containing "x = y" is truncated
    by a split on any later one.
    """
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    return json.loads(raw[raw.index("=") + 1:])


def _tweet_at(t):
    return dt.datetime.strptime(t["tweet"]["created_at"],
                                "%a %b %d %H:%M:%S %z %Y")


def handle_map(tweets):
    """Numeric account id to screen name, mined from your own tweets.

    The tweets sort by time first, so a renamed account resolves to the handle
    it used most recently and the result does not depend on file order.
    """
    out = {}
    for t in sorted(tweets, key=_tweet_at):
        for u in t["tweet"].get("entities", {}).get("user_mentions", []):
            if u.get("id_str") and u.get("screen_name"):
                out[u["id_str"]] = u["screen_name"]
    return out


def tweets_by_day(tweets):
    days = collections.defaultdict(list)
    for t in sorted(tweets, key=_tweet_at):
        days[_tweet_at(t).date().isoformat()].append(t)
    return sorted(days.items())


def dm_records(conversations, handles, me):
    """Flatten DM conversations into the row shape chunking.sessions expects.

    An entry without a messageCreate is a membership event and holds no text.
    An unresolved sender keeps its numeric id: about a third resolve, and one
    shared bucket would merge separate people into one apparent speaker.
    """
    out = []
    for c in conversations:
        conv = c["dmConversation"]
        for m in conv["messages"]:
            if "messageCreate" not in m:
                continue
            e = m["messageCreate"]
            sender = e["senderId"]
            at = dt.datetime.strptime(
                e["createdAt"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=dt.timezone.utc)
            out.append({
                "rowid": e["id"],
                "thread": conv["conversationId"],
                "sender": sender,
                "handle": handles.get(sender, sender),
                "at": at.timestamp(),
                "mine": sender == me,
                "text": e["text"],
            })
    out.sort(key=lambda r: (r["thread"], r["at"], str(r["rowid"])))
    return out


class Twitter(Source):
    name = "twitter"

    def __init__(self):
        self._cache = {}

    def detect(self, root):
        return sorted({p.parent for p in walk(root)
                       if p.name == "tweets.js" and p.is_file()})

    def _export(self, path):
        key = str(path)
        if key not in self._cache:
            tweets = load_js(os.path.join(key, "tweets.js"))
            convos = []
            for name in DM_FILES:
                f = os.path.join(key, name)
                if os.path.exists(f):
                    convos.extend(load_js(f))
            me = None
            account = os.path.join(key, "account.js")
            if os.path.exists(account):
                me = load_js(account)[0]["account"]["accountId"]
            self._cache[key] = (tweets, convos, me)
        return self._cache[key]

    def samples(self, path):
        tweets, convos, me = self._export(path)
        bodies = ["\n".join(t["tweet"]["full_text"] for t in group)
                  for _, group in tweets_by_day(tweets)]
        bodies += ["\n".join(r["text"] for r in w)
                   for w in sessions(dm_records(convos, {}, me),
                                     DM_SESSION_GAP_S, MAX_TURNS)]
        bodies.sort(key=len, reverse=True)
        return bodies[:8]

    def chunks(self, path, budget, contacts=None):
        from .. import trends
        tweets, convos, me = self._export(path)
        yield from self._tweet_chunks(tweets, budget)
        yield from self._dm_chunks(convos, handle_map(tweets), me, budget)
        yield from trend_chunks(tweets, convos, handle_map(tweets), me, budget,
                                trends.zone())
        yield from self._media_chunks(path, tweets, convos, me, budget)

    def media(self, path):
        out = []
        for name in MEDIA_DIRS:
            folder = pathlib.Path(path) / name
            if folder.is_dir():
                out.extend(sorted(p for p in folder.iterdir()
                                  if p.is_file() and not p.name.startswith(".")))
        return out

    def _parents(self, tweets, convos, handles, me):
        """Tweet or DM id to what carried the media: its words, time, and
        for a DM the thread and the sender."""
        idx = {}
        for t in tweets:
            tid = t["tweet"].get("id_str")
            if tid:
                idx[tid] = {"kind": "tweet", "text": t["tweet"]["full_text"],
                            "at": _tweet_at(t).timestamp(), "thread": None,
                            "sender": None, "who": None, "mine": True}
        for r in dm_records(convos, handles, me):
            idx[str(r["rowid"])] = {"kind": "dm", "text": r["text"],
                                    "at": r["at"], "thread": r["thread"],
                                    "sender": r["sender"], "who": r["handle"],
                                    "mine": r["mine"]}
        return idx

    def _media_chunks(self, path, tweets, convos, me, budget):
        """One chunk per captioned media file, from the cache `recall
        caption` fills, linked to its tweet or DM by the file name prefix.
        An orphan keeps its chunk undated: dropping it would lose the
        picture entirely."""
        from .. import captions, config
        parents = self._parents(tweets, convos, handle_map(tweets), me)
        hashes = captions.HashCache(config.WORK_DIR)
        seen = set()
        for file in self.media(path):
            sha = hashes.known(file)
            if sha is None or sha in seen:
                continue
            seen.add(sha)
            rec = captions.read_record(config.WORK_DIR, sha)
            if not rec or rec.get("skipped") or not rec.get("caption"):
                continue
            parent = parents.get(media_id(file.name) or "")
            is_dm = file.parent.name != "tweets_media"
            who = parent["who"] if parent and is_dm and not parent["mine"] else None
            head = (f"[{_iso(parent['at'])[:10] if parent else 'undated'}, "
                    f"{'DM media' if is_dm else 'tweet media'}"
                    f"{' with ' + who if who else ''}]")
            noun = "Video" if rec.get("kind") == "video" else "Image"
            text = f"{head}\n{noun}: {rec['caption']}"
            with_line = ""
            if parent and parent["text"].strip():
                if parent["kind"] == "tweet":
                    with_line = f"Posted with: {parent['text']}"
                else:
                    speaker = "me" if parent["mine"] else parent["who"]
                    with_line = f"Said with: {speaker}: {parent['text']}"
            ocr = rec.get("ocr_text") or ""
            extra = [f"Text in image: {ocr}"
                     if len(ocr) >= captions.OCR_MIN_CHARS else "", with_line]
            floor = len("Said around it: ") + captions.OCR_MIN_CHARS
            for line in fit(budget - len(text), extra, floor):
                text += "\n" + line
            yield Chunk(
                ref=f"twitter-media:{sha}", text=text, source=self.name,
                occurred_at=_iso(parent["at"]) if parent else None,
                date_confidence="exact" if parent else "low",
                participants=[parent["sender"]]
                if parent and parent["sender"] and not parent["mine"] else [],
                thread=parent["thread"] if parent else None)

    def _tweet_chunks(self, tweets, budget):
        """A retweet arrives as "RT @someone: ..." and keeps that prefix. It
        is the only marker separating another person's words from your own."""
        for day, group in tweets_by_day(tweets):
            lines = [t["tweet"]["full_text"] for t in group]
            for suffix, text in parts(lines, budget,
                                      lambda l: f"[{day}, tweets{l}]"):
                yield Chunk(
                    ref=f"tweet:{day}{suffix}",
                    text=text,
                    source=self.name,
                    occurred_at=f"{day}T00:00:00Z",
                    date_confidence="period",
                )

    def _dm_chunks(self, conversations, handles, me, budget):
        for window in sessions(dm_records(conversations, handles, me),
                               DM_SESSION_GAP_S, MAX_TURNS):
            first = window[0]
            senders = sorted({r["sender"] for r in window if not r["mine"]})
            who = ", ".join(sorted({r["handle"] for r in window
                                    if not r["mine"]})) or "unknown"
            when = dt.datetime.fromtimestamp(
                first["at"], dt.timezone.utc).isoformat().replace("+00:00", "Z")
            lines = [f"{'me' if r['mine'] else r['handle']}: {r['text']}"
                     for r in window]
            for suffix, text in parts(
                    lines, budget,
                    lambda l: f"[{when[:10]}, DM with {who}{l}]"):
                yield Chunk(
                    ref=f"dm:{first['rowid']}{suffix}",
                    text=text,
                    source=self.name,
                    occurred_at=when,
                    date_confidence="exact",
                    participants=senders,
                    thread=first["thread"],
                )


def trend_chunks(tweets, conversations, handles, me, budget, tz):
    """One rollup per year: tweet volume and retweet share, the hours and
    weekdays you post, and DM volume by handle. See trends.py."""
    import collections
    from .. import trends
    at = lambda t: _tweet_at(t)
    dms = dm_records(conversations, handles, me)
    dm_years = trends.by_year(dms, lambda r: r["at"], tz)
    for year, items in trends.by_year(tweets, at, tz).items():
        rts = sum(1 for t in items if t["tweet"]["full_text"].startswith("RT @"))
        year_dms = dm_years.get(year, [])
        by_handle = collections.Counter(r["handle"] for r in year_dms if not r["mine"])
        lines = [
            f"{len(items):,} tweets, {rts:,} retweets, {len(items) - rts:,} original.",
            "When you tweet: " + trends.describe_hours(items, at, tz) + ".",
            trends.describe_weekdays(trends.weekday_counts(items, at, tz)),
            trends.describe_months(trends.month_counts(items, at, tz)),
            f"Direct messages: {len(year_dms):,}" + (
                "; most with " + ", ".join(f"{h} ({n})" for h, n in trends.top(by_handle))
                if by_handle else ""),
        ]
        yield from trends.chunks("twitter", year, lines, budget, tz)
