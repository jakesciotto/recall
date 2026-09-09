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
