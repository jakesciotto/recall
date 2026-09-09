"""The caption cache, and the loop that fills it. See docs/sources.md.

One record per image, keyed by the file's sha256, at
<work>/captions/<sha[:2]>/<sha>.json. A record existing means that hash is
done. It is written on success or on a deliberate gate, never on failure,
so a restarting server cannot mark images done that it never captioned.
"""

import hashlib
import json
import os
import pathlib
import threading

from . import imagery

OCR_MIN_CHARS = 20
IMAGE_KINDS = {"jpeg", "png", "heic", "gif", "bmp", "tiff", "webp", "avif"}


def record_path(work, sha):
    return pathlib.Path(work) / "captions" / sha[:2] / f"{sha}.json"


def read_record(work, sha):
    try:
        return json.loads(record_path(work, sha).read_text(encoding="utf-8"))
    except OSError:
        return None


def write_record(work, record):
    p = record_path(work, record["sha256"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, p)


def count(work):
    root = pathlib.Path(work) / "captions"
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.glob("*/*.json"))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class HashCache:
    """sha256 per file, keyed by path, size and mtime, so a re-run never
    re-reads an unchanged tree. Append-only JSONL; the last line wins."""

    def __init__(self, work):
        self.path = pathlib.Path(work) / "hashes.jsonl"
        self._lock = threading.Lock()
        self._map = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    r = json.loads(line)
                    self._map[r["path"]] = (r["size"], r["mtime"], r["sha256"])
        except OSError:
            pass

    def known(self, path):
        st = os.stat(path)
        hit = self._map.get(str(path))
        if hit and hit[0] == st.st_size and hit[1] == int(st.st_mtime):
            return hit[2]
        return None

    def get(self, path):
        sha = self.known(path)
        if sha:
            return sha
        st = os.stat(path)
        sha = sha256_of(path)
        row = {"path": str(path), "size": st.st_size,
               "mtime": int(st.st_mtime), "sha256": sha}
        with self._lock:
            self._map[str(path)] = (row["size"], row["mtime"], sha)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        return sha


from collections import Counter  # noqa: E402
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait  # noqa: E402

from . import config, vision  # noqa: E402
from .sources import detect_all  # noqa: E402


def process(path, work, captioner=None, decoder=None, ocr=None, hashes=None):
    """One file. Nothing is written on a failure, so the next run retries it."""
    captioner = captioner or vision.caption
    decoder = decoder or imagery.decode
    ocr = ocr or imagery.ocr
    hashes = hashes or HashCache(work)
    try:
        kind = imagery.kind(path)
        if kind not in IMAGE_KINDS:
            return "skipped"
        sha = hashes.get(path)
        if read_record(work, sha) is not None:
            return "done"
        try:
            jpeg, size = decoder(path)
        except imagery.DecodeError:
            return "failed"
        record = {"sha256": sha, "kind": kind, "px": list(size),
                  "caption": None, "caption_model": None,
                  "ocr_text": None, "ocr_chars": 0, "skipped": None}
        if size[0] * size[1] < imagery.MIN_PIXELS:
            record["skipped"] = f"below pixel gate ({size[0]}x{size[1]})"
            write_record(work, record)
            return "gated"
        text = ocr(jpeg)
        if len(text) >= OCR_MIN_CHARS:
            record["ocr_text"] = text
            record["ocr_chars"] = len(text)
        try:
            record["caption"] = captioner(jpeg)
        except vision.CaptionError:
            return "failed"
        record["caption_model"] = config.VISION_MODEL
        write_record(work, record)
        return "ok"
    except Exception:
        # A day-long run must not die because one file surprised us.
        return "failed"


def uncaptioned(paths, work):
    """(images without a record, images). Never hashes: an ingest must stay
    cheap, and only `recall caption` reads image bytes."""
    hashes = HashCache(work)
    missing = images = 0
    for p in paths:
        try:
            if imagery.kind(p) not in IMAGE_KINDS:
                continue
            sha = hashes.known(p)
        except OSError:
            continue
        images += 1
        if sha is None or read_record(work, sha) is None:
            missing += 1
    return missing, images


def run(root, work, jobs=3, limit=None, log=print):
    """Caption every media file the adapters declare. `limit` bounds new
    work, not files already done, so a smoke test always does something."""
    if not config.VISION_URL:
        raise SystemExit("RECALL_VISION_URL is not set; see docs/sources.md")
    if not imagery.available():
        raise SystemExit("Pillow is not installed. Run: "
                         "pip install 'recall[captions]'")
    found = detect_all(root)
    files = [p for adapter, path in found for p in adapter.media(path)]
    log(f"{len(files):,} media files from {len(found)} sources; "
        f"{count(work):,} records held")
    hashes = HashCache(work)
    counts = Counter()
    todo = iter(files)
    seen = 0
    with ThreadPoolExecutor(jobs) as ex:
        pending = set()

        def fill():
            while len(pending) < jobs * 2:
                p = next(todo, None)
                if p is None:
                    return
                pending.add(ex.submit(process, p, work, hashes=hashes))
        fill()
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in finished:
                counts[f.result()] += 1
                seen += 1
                if seen % 500 == 0:
                    log(f"  {seen:,} / {len(files):,}  {dict(counts)}")
            worked = counts["ok"] + counts["gated"] + counts["failed"]
            if limit and worked >= limit:
                for f in pending:
                    f.cancel()
                pending = set()
                break
            fill()
    log(f"done: {dict(counts)}")
    return dict(counts)
