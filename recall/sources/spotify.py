"""Spotify extended streaming history.

The reason this adapter rolls up: one real export held 391,896 plays. As one
chunk each that is nearly twice an entire corpus, all of it listening noise,
and every unrelated query gets worse. Rolled to one chunk per month it is
about 126 chunks that answer "what was I listening to around then".

Two details that matter. A play under 30 seconds is a skip, and counting
skips makes a briefly sampled artist look like a favourite; 30 seconds is
Spotify's own definition of a stream. And the export carries thousands of
distinct IP addresses, which is a location trail that has no business in a
retrieval corpus, so nothing here ever writes one.
"""

import collections
import glob
import json
import os

from .base import Chunk, Source

MIN_PLAY_MS = 30_000


class Spotify(Source):
    name = "spotify"

    def detect(self, root):
        hits = set()
        for p in root.rglob("*Streaming_History_Audio*.json"):
            hits.add(p.parent)
        return sorted(hits)

    def samples(self, path):
        return []          # rollup chunks are bounded by construction

    def chunks(self, path, budget):
        plays = []
        for f in sorted(glob.glob(os.path.join(str(path), "**",
                                               "*Audio*.json"), recursive=True)):
            with open(f, encoding="utf-8") as fh:
                plays.extend(json.load(fh))

        months = collections.defaultdict(list)
        for p in plays:
            if (p.get("ms_played") or 0) < MIN_PLAY_MS:
                continue
            months[p["ts"][:7]].append(p)

        for month, group in sorted(months.items()):
            hours = sum(p.get("ms_played") or 0 for p in group) / 3.6e6
            lines = [f"[{month}, listening]",
                     f"{hours:.1f} hours across {len(group):,} plays."]
            for field, label, n in (
                    ("master_metadata_album_artist_name", "Top artists", 10),
                    ("master_metadata_track_name", "Top tracks", 5)):
                counts = collections.Counter(
                    p[field] for p in group if p.get(field))
                if counts:
                    lines.append(f"{label}: " + ", ".join(
                        f"{k} ({v})" for k, v in counts.most_common(n)))
            # Episode titles carry topic signal in a way track names do not.
            shows = sorted({p["episode_show_name"] for p in group
                            if p.get("episode_show_name")})
            if shows:
                lines.append("Podcasts: " + ", ".join(shows))
            yield Chunk(
                ref=f"spotify:{month}",
                text="\n".join(lines),
                source=self.name,
                occurred_at=f"{month}-01T00:00:00Z",
                date_confidence="period",
            )
