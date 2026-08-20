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

from ..chunking import parts, sessions
from .base import Chunk, Source, walk

# iMessage splits at 30 minutes. Twitter DMs are asynchronous, and at 30
# minutes 39 percent of sessions came out as one short message. A day puts the
# median DM chunk at 462 characters, matching the message chunks beside it.
DM_SESSION_GAP_S = 86_400
MAX_TURNS = 20

DM_FILES = ("direct-messages.js", "direct-messages-group.js")


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
        tweets, convos, me = self._export(path)
        yield from self._tweet_chunks(tweets, budget)
        yield from self._dm_chunks(convos, handle_map(tweets), me, budget)

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
