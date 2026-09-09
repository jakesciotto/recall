import io
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from recall import imagery


def png_bytes(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "JPEG")
    return buf.getvalue()


HEIC_HEAD = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40


class TestKindByMagicBytes(unittest.TestCase):
    """10,070 files carried a .pluginPayloadAttachment extension and 58
    percent of them were real photos. The extension says nothing."""

    def kind_of(self, name, head):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / name
            p.write_bytes(head + b"\x00" * 20)
            return imagery.kind(p)

    def test_a_jpeg_with_a_lying_extension_is_a_jpeg(self):
        self.assertEqual(self.kind_of("x.pluginPayloadAttachment",
                                      b"\xff\xd8\xff\xe0"), "jpeg")

    def test_text_with_a_jpg_extension_is_not_an_image(self):
        self.assertIsNone(self.kind_of("notes.jpg", b"hello there"))

    def test_heic_by_its_ftyp_brand(self):
        self.assertEqual(self.kind_of("p.heic", b"\x00\x00\x00\x18ftypheic"),
                         "heic")

    def test_png_gif_webp(self):
        self.assertEqual(self.kind_of("a", b"\x89PNG\r\n\x1a\n"), "png")
        self.assertEqual(self.kind_of("b", b"GIF89a"), "gif")
        self.assertEqual(self.kind_of("c", b"RIFF\x00\x00\x00\x00WEBP"), "webp")


@unittest.skipUnless(imagery.available(), "needs the captions extra (Pillow)")
class TestDecode(unittest.TestCase):
    def test_a_png_becomes_a_jpeg_with_its_original_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "a.png"
            p.write_bytes(png_bytes(300, 200))
            jpeg, size = imagery.decode(p)
        self.assertEqual(jpeg[:3], b"\xff\xd8\xff")
        self.assertEqual(size, (300, 200))

    def test_a_large_image_is_bounded_but_reports_its_true_size(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "a.png"
            p.write_bytes(png_bytes(3000, 100))
            jpeg, size = imagery.decode(p)
        self.assertEqual(size, (3000, 100))
        with Image.open(io.BytesIO(jpeg)) as im:
            self.assertLessEqual(max(im.size), imagery.MAX_EDGE)

    def test_a_file_pillow_cannot_read_goes_through_imagemagick(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[:2])
            if "identify" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=b"640 480",
                                                   stderr=b"")
            return subprocess.CompletedProcess(cmd, 0, stdout=jpeg_bytes(64, 48),
                                               stderr=b"")
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "a.heic"
            p.write_bytes(HEIC_HEAD)
            with mock.patch.object(imagery.subprocess, "run", fake_run):
                jpeg, size = imagery.decode(p)
        self.assertEqual(size, (640, 480))
        self.assertEqual(jpeg[:3], b"\xff\xd8\xff")
        self.assertEqual(len(calls), 2)

    def test_imagemagick_failing_is_a_decode_error(self):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout=b"",
                                               stderr=b"no decoder")
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "a.heic"
            p.write_bytes(HEIC_HEAD)
            with mock.patch.object(imagery.subprocess, "run", fake_run):
                with self.assertRaises(imagery.DecodeError):
                    imagery.decode(p)


class TestOcr(unittest.TestCase):
    def test_a_missing_tesseract_reads_as_no_text(self):
        with mock.patch.object(imagery.subprocess, "run",
                               side_effect=FileNotFoundError):
            self.assertEqual(imagery.ocr(b"jpeg"), "")

    def test_tesseract_output_is_stripped(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"  hello\n\n",
                                           stderr=b"")
        with mock.patch.object(imagery.subprocess, "run", return_value=done):
            self.assertEqual(imagery.ocr(b"jpeg"), "hello")
