"""Loose documents: the catch-all adapter.

Four rules from the first real archive, each in docs/lessons.md. The header
is read before the extension is trusted. The date comes from the path, then
from embedded metadata, and from the mtime last. The ref follows the content,
so a moved file is not re-embedded and two copies are one document. And the
user's own exclusions live in documents/.recallignore, matched on segments,
never on a rooted prefix.
"""

import datetime as dt
import fnmatch
import hashlib
import re
import shutil
import subprocess
import sys
import zipfile

from .base import Chunk, Source, walk

# Resolved once. PDFs go through poppler's pdftotext, and read_text swallows
# every error per file, so a missing binary would turn a shelf of PDFs into
# nothing with a clean-looking summary. The warning below fires once.
# pdfinfo and antiword are optional: without them PDFs date by path or mtime
# and legacy .doc files are skipped.
PDFTOTEXT = shutil.which("pdftotext")
PDFINFO = shutil.which("pdfinfo")
ANTIWORD = shutil.which("antiword")
_warned_pdftotext = False

TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json"}
OFFICE_EXT = {".docx", ".pptx", ".xlsx"}
OTHER_EXT = {".pdf", ".html", ".htm", ".doc", ".rtf"}
CANDIDATE_EXT = TEXT_EXT | OFFICE_EXT | OTHER_EXT

# Segment matches survive a reorganised tree; a rooted prefix does not.
# clinical-records holds the FHIR medical records that ship inside an
# Apple Health export. Indexing those must be a deliberate choice.
SKIP_SEGMENTS = {"node_modules", ".git", "venv", ".venv", "__pycache__",
                 "site-packages", "Caches", "cache", "books",
                 "clinical-records"}
SKIP_HINTS = re.compile(r"(z-library|libgen|annas-archive|\(ebook\))", re.I)
MAX_BYTES = 25 * 1024 * 1024
# Thirty-one CSV exports held 64 percent of one archive's characters. The
# cap keeps a file's header and first rows and leaves the rest.
MAX_CHARS = 2_000_000

# Binary guard: a NUL in the first block, or too few printable bytes once
# strict UTF-8 has failed. What file(1) and git do, plus the UTF-8 step so
# a note in Japanese is not thrown away for having no ASCII in it.
PROBE = 8192
PRINTABLE_RATIO = 0.85

# Overlap keeps a sentence that straddles a boundary findable from both sides.
OVERLAP = 400

_BOMS = ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
         (b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"),
         (b"\xfe\xff", "utf-16"))

_YEAR = re.compile(r"(?<!\d)(19[89]\d|20[0-3]\d)(?!\d)")
_ISO = re.compile(r"(?<!\d)(19[89]\d|20[0-3]\d)[-_]?(0[1-9]|1[0-2])"
                  r"(?:[-_]?(0[1-9]|[12]\d|3[01]))?(?!\d)")
_MONTHS = ("january february march april may june july august september "
           "october november december").split()
_SEASONS = {"spring": 4, "summer": 7, "fall": 10, "autumn": 10, "winter": 1}


class Ignore:
    """The user's exclusions, one pattern per line in documents/.recallignore.

    A pattern without a slash matches a file name or a directory name at any
    depth. A pattern with a slash matches the path relative to documents/.
    Matching ignores case, because the exports come from filesystems that do.
    """

    FILE = ".recallignore"

    def __init__(self, patterns):
        self.patterns = [p.lower() for p in patterns]

    @classmethod
    def load(cls, root):
        f = root / cls.FILE
        if not f.is_file():
            return cls([])
        lines = (l.strip() for l in f.read_text().splitlines())
        return cls([l for l in lines if l and not l.startswith("#")])

    def matches(self, rel):
        whole = rel.as_posix().lower()
        parts = [p.lower() for p in rel.parts]
        for pat in self.patterns:
            if "/" in pat:
                if fnmatch.fnmatchcase(whole, pat):
                    return True
            elif any(fnmatch.fnmatchcase(p, pat) for p in parts):
                return True
        return False


def keep(path, ignore=None):
    """`path` is relative to the documents root."""
    name = path.name
    if name.startswith("._") or name == ".DS_Store":
        return False
    if set(path.parts[:-1]) & SKIP_SEGMENTS:
        return False
    if SKIP_HINTS.search(name):
        return False
    return not (ignore and ignore.matches(path))


def _bom_encoding(raw):
    for mark, enc in _BOMS:
        if raw.startswith(mark):
            return enc
    return None


def is_binary(raw):
    if _bom_encoding(raw):
        return False
    head = raw[:PROBE]
    if not head:
        return False
    if b"\x00" in head:
        return True
    try:
        head.decode("utf-8")
        return False
    except UnicodeDecodeError as e:
        # A multibyte character cut by the probe boundary is not evidence.
        if e.start >= len(head) - 3:
            return False
    printable = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    return printable / len(head) < PRINTABLE_RATIO


def decode_bytes(raw):
    """A declared byte order mark, then UTF-8, then what Windows wrote."""
    enc = _bom_encoding(raw)
    if enc:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def sniff(path):
    """Kind from the header, falling back to the extension.

    Extensions lie in both directions on an old archive: a Photoshop file
    named .pdf, a PDF named .txt. The header does not lie.
    """
    with open(path, "rb") as f:
        head = f.read(PROBE)
    # poppler accepts leading whitespace before the header; so do we.
    if head.lstrip().startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"{\\rt"):
        return "rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(path)
    if is_binary(head):
        return "binary"
    ext = path.suffix.lower()
    if ext in (".html", ".htm"):
        return "html"
    if ext == ".rtf":
        return "rtf"
    return "text"


def _zip_kind(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (zipfile.BadZipFile, OSError):
        return "binary"
    if "word/document.xml" in names:
        return "docx"
    if any(n.startswith("ppt/slides/slide") for n in names):
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return "binary"


def _warn_pdftotext():
    global _warned_pdftotext
    if _warned_pdftotext:
        return
    _warned_pdftotext = True
    print("pdftotext not found: every PDF is skipped. Install poppler "
          "(apt: poppler-utils, dnf: poppler-utils, brew: poppler).",
          file=sys.stderr)


def _pdf(path):
    if not PDFTOTEXT:
        _warn_pdftotext()
        return ""
    r = subprocess.run([PDFTOTEXT, "-q", str(path), "-"],
                       capture_output=True, timeout=180)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def _office(path):
    with zipfile.ZipFile(path) as z:
        xml = " ".join(
            z.read(n).decode("utf-8", "replace")
            for n in z.namelist()
            if n.endswith(".xml") and ("document" in n or "slide" in n
                                       or "sharedStrings" in n))
    return re.sub(r"<[^>]+>", " ", xml)


def _ole(path):
    if not ANTIWORD:
        return ""
    r = subprocess.run([ANTIWORD, "-w", "0", str(path)],
                       capture_output=True, timeout=180)
    return decode_bytes(r.stdout) if r.returncode == 0 else ""


def _rtf(text):
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\{\\\*.*?\}", "", text, flags=re.S)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    return re.sub(r"<[^>]+>", " ", text)


def read_text(path):
    try:
        return _read(path)[:MAX_CHARS]
    except Exception:
        return ""


def _read(path):
    kind = sniff(path)
    if kind == "binary":
        return ""
    if kind == "pdf":
        return _pdf(path)
    if kind in ("docx", "pptx", "xlsx"):
        return _office(path)
    if kind == "ole":
        return _ole(path)
    text = decode_bytes(path.read_bytes())
    if kind == "rtf":
        return _rtf(text)
    if kind == "html":
        return _html(text)
    return text


def path_date(rel):
    """ISO 8601 from the path, or None. A folder named 2019 is the user's
    own claim about when a document belongs."""
    m = _ISO.search(rel)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        try:
            return _iso(dt.datetime(y, mo, d))
        except ValueError:
            pass
    years = [int(y) for y in _YEAR.findall(rel)]
    if not years:
        return None
    low = rel.lower()
    month = 1
    for name, num in zip(_MONTHS, range(1, 13)):
        if name in low:
            month = num
            break
    else:
        for name, num in _SEASONS.items():
            if name in low:
                month = num
                break
    return _iso(dt.datetime(max(years), month, 1))


def occurred_at(rel, mtime, meta=None):
    """(iso8601, confidence). Path beats metadata beats mtime.

    Metadata is what the writing application stamped, so it beats the mtime
    a sync rewrote, and loses to a folder the user named.
    """
    hit = path_date(rel)
    if hit:
        return hit, "path"
    if meta:
        return meta, "metadata"
    return mtime, "mtime"


def metadata_date(path):
    """Embedded creation date as ISO 8601, or None."""
    try:
        kind = sniff(path)
        if kind == "pdf":
            return _pdf_date(path)
        if kind in ("docx", "pptx", "xlsx"):
            return _office_date(path)
    except Exception:
        pass
    return None


def _pdf_date(path):
    if not PDFINFO:
        return None
    # -isodates keeps the offset. The default output is local time with the
    # zone as a name, which parses to the wrong hour on every box outside UTC.
    r = subprocess.run([PDFINFO, "-isodates", str(path)],
                       capture_output=True, timeout=60)
    m = re.search(rb"^CreationDate:\s+(\S+)", r.stdout, re.M)
    if not m:
        return None
    raw = m.group(1).decode("utf-8", "replace").replace("Z", "+00:00")
    try:
        when = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo:
        when = when.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return _sane(when)


def _office_date(path):
    with zipfile.ZipFile(path) as z:
        if "docProps/core.xml" not in z.namelist():
            return None
        raw = z.read("docProps/core.xml")
    m = re.search(rb"<dcterms:created[^>]*>([^<]+)<", raw)
    if not m:
        return None
    try:
        return _sane(dt.datetime.strptime(m.group(1).decode()[:19],
                                          "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _sane(when):
    """Reject the epoch defaults and impossible futures broken writers stamp.
    A wrong date is worse than none: the mtime is at least honest."""
    if not 1990 < when.year <= dt.date.today().year:
        return None
    return _iso(when)


def _iso(when):
    return when.replace(tzinfo=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _letter_share(text):
    return sum(ch.isalpha() for ch in text) / max(len(text), 1)


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def split(text, budget):
    """Paragraph-aware split with overlap, never over budget.

    A paragraph longer than the budget is cut at a line end when one falls
    in the second half of the window, else hard. The overlap carried into
    the next chunk shrinks to whatever room the next paragraph leaves.
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf = [], ""
    for p in paras:
        while len(p) > budget:
            if buf:
                out.append(buf)
                buf = ""
            cut = p.rfind("\n", budget // 2, budget)
            cut = cut + 1 if cut > 0 else budget
            out.append(p[:cut].rstrip())
            p = p[cut:].lstrip()
        if not p:
            continue
        if buf and len(buf) + len(p) + 2 > budget:
            out.append(buf)
            room = budget - len(p) - 2
            tail = buf[-min(OVERLAP, room):] if room > 0 else ""
            buf = f"{tail}\n\n{p}" if tail else p
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

    def _paths(self, path, ignore=None):
        for p in walk(path):
            rel = p.relative_to(path)
            if not keep(rel, ignore) or p.suffix.lower() not in CANDIDATE_EXT:
                continue
            if not p.is_file() or p.stat().st_size > MAX_BYTES:
                continue
            yield p, rel

    def samples(self, path):
        """The longest texts and the densest.

        The budget calibrates from these and the loader trusts it. Length
        alone missed an 18 KB price table on one archive: it ran at one
        token per character against 1.5 for the longest texts, so its
        chunks sat under the character budget and over the token ceiling.
        The share of letters in the first block is the proxy for density,
        read on text files only; a numeric table inside a PDF is covered by
        the safety margin alone.
        """
        paths = list(self._paths(path, Ignore.load(path)))
        largest = sorted(paths, key=lambda pr: -pr[0].stat().st_size)[:100]
        long_texts = [t for t in (read_text(p)[:20000] for p, _ in largest) if t]
        heads = []
        for p, rel in paths:
            if rel.suffix.lower() not in TEXT_EXT:
                continue
            with open(p, "rb") as f:
                raw = f.read(PROBE)
            if raw and not is_binary(raw):
                heads.append((_letter_share(decode_bytes(raw)), p))
        heads.sort(key=lambda h: h[0])
        dense_texts = [t for t in (read_text(p)[:20000] for _, p in heads[:30]) if t]
        return (sorted(long_texts, key=len, reverse=True)[:8]
                + sorted(dense_texts, key=_letter_share)[:8])

    def chunks(self, path, budget, contacts=None):
        ignore = Ignore.load(path)
        seen = set()
        for p, rel in self._paths(path, ignore):
            digest = _digest(p)
            if digest in seen:
                continue
            seen.add(digest)
            text = read_text(p)
            if not text.strip():
                continue
            mtime = _iso(dt.datetime.fromtimestamp(p.stat().st_mtime,
                                                   dt.timezone.utc))
            rel_s = rel.as_posix()
            # pdfinfo is a process per file; only spend it when the path
            # said nothing.
            meta = None if path_date(rel_s) else metadata_date(p)
            when, confidence = occurred_at(rel_s, mtime, meta)
            header = f"[{rel_s}]\n"
            for i, piece in enumerate(split(text, budget - len(header))):
                yield Chunk(
                    ref=f"doc:{digest[:16]}:{i}",
                    text=header + piece,
                    source=self.name,
                    occurred_at=when,
                    date_confidence=confidence,
                    path=rel_s,
                )
