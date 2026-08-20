"""Apple Health: workouts only, rolled up one chunk per month.

An export holds millions of sample records against a few thousand workouts.
The samples are telemetry and belong in a table, not in a retrieval corpus.
This adapter opens export.xml and nothing else, so it never reads the FHIR
clinical records that ship in the same export. See docs/lessons.md.
"""

import collections
import contextlib
import re
import xml.etree.ElementTree as ET
import zipfile

from ..chunking import PART_LABEL_SAMPLE, split_lines
from .base import Chunk, Source, walk

ZIP_MEMBER = "apple_health_export/export.xml"


def activity_name(raw):
    """"HKWorkoutActivityTypeTraditionalStrengthTraining" becomes
    "Traditional Strength Training". The stored form matches no question
    anyone asks."""
    bare = raw.replace("HKWorkoutActivityType", "")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", bare)


def workouts(stream):
    """Stream Workout elements out of export.xml.

    iterparse with clear() keeps memory flat against a file of hundreds of
    megabytes. Never clear a WorkoutStatistics: it is a child, its end event
    fires BEFORE its parent Workout, and clearing there empties every workout
    of its distance and calories.
    """
    context = ET.iterparse(stream, events=("start", "end"))
    _, root = next(context)
    for event, el in context:
        if event != "end":
            continue
        if el.tag not in ("Workout", "WorkoutStatistics"):
            el.clear()
            root.clear()
            continue
        if el.tag == "WorkoutStatistics":
            continue
        stats = {}
        for s in el.findall("WorkoutStatistics"):
            key = (s.get("type") or "").replace("HKQuantityTypeIdentifier", "")
            try:
                stats[key] = float(s.get("sum"))
            except (TypeError, ValueError):
                pass
        yield {
            "type": activity_name(el.get("workoutActivityType") or ""),
            "duration": float(el.get("duration") or 0),
            "unit": el.get("durationUnit") or "min",
            "start": el.get("startDate") or "",
            "source_name": el.get("sourceName") or "",
            "stats": stats,
        }
        el.clear()
        root.clear()


def line(w):
    bits = [f"{w['start'][:10]}  {w['type']}", f"{w['duration']:.0f} min"]
    dist = w["stats"].get("DistanceWalkingRunning") or 0
    if round(dist, 2) > 0:
        bits.append(f"{dist:.2f} mi")
    kcal = w["stats"].get("ActiveEnergyBurned") or 0
    if round(kcal) > 0:
        bits.append(f"{kcal:.0f} Cal")
    return "  ".join(bits)


def by_month(items):
    months = collections.defaultdict(list)
    for w in items:
        if w["start"]:
            months[w["start"][:7]].append(w)
    for month in sorted(months):
        yield month, sorted(months[month], key=lambda w: w["start"])


def _summary(month, items, part=None):
    counts = collections.Counter()
    minutes = collections.Counter()
    for w in items:
        counts[w["type"]] += 1
        minutes[w["type"]] += w["duration"]
    label = part or ""
    totals = "  ".join(f"{t}: {n} sessions, {minutes[t]:.0f} min"
                       for t, n in counts.most_common())
    plural = "" if len(items) == 1 else "s"
    return f"[{month}, workouts{label}]\n{len(items)} workout{plural}. {totals}"


def month_chunks(items, budget):
    """One chunk per month, listing the individual sessions inside it.

    A single workout is about 120 characters and embeds to noise. A bare
    monthly total cannot answer "when did I start jiu jitsu". Listing the
    sessions inside a monthly chunk answers both. A busy month splits on whole
    workouts, and each part counts its own sessions, so the part number has to
    appear in the text or the count reads as a month total.
    """
    for month, group in by_month(items):
        # A part lists a subset of the month with smaller totals, so the
        # month summary is an upper bound on any part summary.
        room = budget - len(_summary(month, group, PART_LABEL_SAMPLE)) - 1
        groups = list(split_lines([line(w) for w in group], max(room, 1)))
        single = len(groups) == 1
        seen = 0
        for i, part in enumerate(groups, start=1):
            here = group[seen:seen + len(part)]
            seen += len(part)
            yield Chunk(
                ref=f"health:{month}" if single else f"health:{month}#{i}",
                text=_summary(month, here, None if single else f", part {i}")
                     + "\n" + "\n".join(part),
                source="health",
                occurred_at=f"{month}-01T00:00:00Z",
                date_confidence="period",
            )


def _is_health_zip(path):
    try:
        with zipfile.ZipFile(path) as z:
            return ZIP_MEMBER in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


@contextlib.contextmanager
def open_export(path):
    """The phone hands you export.zip. Unpacking hundreds of megabytes first
    is a step that buys nothing."""
    if str(path).endswith(".zip"):
        with zipfile.ZipFile(path) as z, z.open(ZIP_MEMBER) as f:
            yield f
    else:
        with open(path, "rb") as f:
            yield f


class Health(Source):
    name = "health"

    def __init__(self):
        self._cache = {}

    def detect(self, root):
        found = []
        for p in walk(root):
            if p.name == "export.xml" and p.is_file():
                found.append(p)
            elif p.suffix.lower() == ".zip" and _is_health_zip(p):
                found.append(p)
        return sorted(found)

    def _workouts(self, path):
        key = str(path)
        if key not in self._cache:
            with open_export(path) as stream:
                self._cache[key] = list(workouts(stream))
        return self._cache[key]

    def samples(self, path):
        bodies = ["\n".join(line(w) for w in group)
                  for _, group in by_month(self._workouts(path))]
        bodies.sort(key=len, reverse=True)
        return bodies[:4]

    def chunks(self, path, budget, contacts=None):
        yield from month_chunks(self._workouts(path), budget)
