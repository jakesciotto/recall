"""Decode an attachment to a bounded JPEG, sniff its kind, gate on size, OCR.

Pillow reads the common formats. Anything it cannot read falls back to
ImageMagick, which needs a HEVC decoder plugin for HEIC. `magick -list
format` advertises HEIC even when no plugin is installed, so trust a real
decode and never that list. See docs/sources.md.
"""

import io
import subprocess

MIN_PIXELS = 50_000
MAX_EDGE = 1024
_QUALITY = 85


class DecodeError(Exception):
    pass


def available():
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def kind(path):
    """Image kind by the first bytes, never by extension."""
    with open(path, "rb") as f:
        head = f.read(16)
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "heic"
        if brand in (b"avif", b"avis"):
            return "avif"
    return None


def _to_jpeg(im):
    from PIL import Image
    im = im.convert("RGB")
    im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=_QUALITY)
    return buf.getvalue()


def _magick_size(path):
    # No frame selector here: "path[0]" makes identify copy to a temp file
    # without an extension, which libheif then refuses.
    r = subprocess.run(["magick", "identify", "-ping", "-format", "%w %h",
                        str(path)], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise DecodeError(r.stderr.decode("utf-8", "replace")[:200]
                          or "identify failed")
    try:
        w, h = r.stdout.decode().split()[:2]
        return int(w), int(h)
    except ValueError as e:
        raise DecodeError(f"identify gave no size: {r.stdout[:60]!r}") from e


def _magick(path):
    r = subprocess.run(
        ["magick", f"{path}[0]", "-auto-orient",
         "-resize", f"{MAX_EDGE}x{MAX_EDGE}>", "JPEG:-"],
        capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise DecodeError(r.stderr.decode("utf-8", "replace")[:200]
                          or "magick produced no output")
    return r.stdout


def decode(path):
    """(jpeg bytes, original size). Raises DecodeError.

    The size is the original on both paths. ImageMagick resizes while it
    converts, so that path reads the true size separately.
    """
    from PIL import Image
    try:
        with Image.open(path) as im:
            size = im.size
            return _to_jpeg(im), size
    except (OSError, ValueError):
        pass
    try:
        size = _magick_size(path)
        jpeg = _magick(path)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise DecodeError(str(e)[:200]) from e
    try:
        with Image.open(io.BytesIO(jpeg)) as im:
            im.size
    except (OSError, ValueError) as e:
        raise DecodeError(f"magick output unreadable: {e}") from e
    return jpeg, size


def ocr(jpeg, timeout=60):
    try:
        r = subprocess.run(["tesseract", "-", "-"], input=jpeg,
                           capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", "replace").strip()
