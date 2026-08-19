"""Loose documents: the catch-all adapter.

Two guards live here, both learned the hard way.

**Decide exclusions before ingesting, not after.** A personal archive holds
pirated ebooks, cached node_modules, and credential files. One filter written
against a path prefix leaked about 19,000 repository files and a credentials
file into a corpus, because the prefix stopped matching once the tree was
reorganised while the include rules kept matching. Match on path SEGMENTS and
on names, never on a rooted prefix.

**Sniff the content, do not trust the extension.** A Photoshop file named
.pdf becomes two million characters of noise if you believe the name.
"""

import re
import subprocess
import zipfile

from .base import Chunk, Source

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json"}
OFFICE_EXT = {".docx", ".pptx", ".xlsx"}

# Segment matches survive a reorganised tree; a rooted prefix does not.
SKIP_SEGMENTS = {"node_modules", ".git", "venv", ".venv", "__pycache__",
                 "site-packages", "Caches", "cache", "books"}
SKIP_HINTS = re.compile(r"(z-library|libgen|annas-archive|\(ebook\))", re.I)
MAX_BYTES = 25 * 1024 * 1024

# Overlap keeps a sentence that straddles a boundary findable from both sides.
OVERLAP = 400


def keep(path):
    parts = set(path.parts[:-1])
    if parts & SKIP_SEGMENTS:
        return False
    return not SKIP_HINTS.search(path.name)


def _is_binary(raw):
    return b"\x00" in raw[:4096]


def read_text(path):
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            r = subprocess.run(["pdftotext", "-q", str(path), "-"],
                               capture_output=True, timeout=180)
            return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
        if ext in OFFICE_EXT:
            with zipfile.ZipFile(path) as z:
                xml = " ".join(
                    z.read(n).decode("utf-8", "replace")
                    for n in z.namelist()
                    if n.endswith(".xml") and ("document" in n or "slide" in n
                                               or "sharedStrings" in n))
            return re.sub(r"<[^>]+>", " ", xml)
        if ext in TEXT_EXT or ext in (".html", ".htm"):
            raw = path.read_bytes()
            if _is_binary(raw):
                return ""
            text = raw.decode("utf-8", "replace")
            if ext in (".html", ".htm"):
                text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
                text = re.sub(r"<[^>]+>", " ", text)
            return text
    except Exception:
        return ""
    return ""


def split(text, budget):
    """Paragraph-aware split with overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > budget:
            out.append(buf)
            buf = (buf[-OVERLAP:] + "\n\n" + p) if len(buf) > OVERLAP else p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out


class Files(Source):
    name = "documents"

    def detect(self, root):
        docs = root / "documents"
        return [docs] if docs.is_dir() else []

    def _paths(self, path):
        for p in sorted(path.rglob("*")):
            if not p.is_file() or not keep(p):
                continue
            if p.stat().st_size > MAX_BYTES:
                continue
            if p.suffix.lower() in TEXT_EXT | OFFICE_EXT | {".pdf", ".html", ".htm"}:
                yield p

    def samples(self, path):
        texts = []
        for p in list(self._paths(path))[:200]:
            t = read_text(p)
            if t:
                texts.append(t[:20000])
        texts.sort(key=len, reverse=True)
        return texts[:8]

    def chunks(self, path, budget):
        import datetime as dt
        import hashlib
        for p in self._paths(path):
            text = read_text(p)
            if not text or not text.strip():
                continue
            rel = p.relative_to(path)
            digest = hashlib.sha256(str(rel).encode()).hexdigest()[:16]
            # mtime is a weak date, and it says so rather than pretending.
            when = dt.datetime.fromtimestamp(
                p.stat().st_mtime, dt.timezone.utc).isoformat().replace(
                    "+00:00", "Z")
            for i, piece in enumerate(split(text, budget)):
                yield Chunk(
                    ref=f"doc:{digest}:{i}",
                    text=f"[{rel}]\n{piece}",
                    source=self.name,
                    occurred_at=when,
                    date_confidence="mtime",
                    path=str(rel),
                )
