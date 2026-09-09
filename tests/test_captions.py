import hashlib
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from recall import captions

SHA = "ab" + "0" * 62


class TestRecords(unittest.TestCase):
    def test_a_record_lands_under_its_hash_prefix(self):
        with tempfile.TemporaryDirectory() as work:
            captions.write_record(work, {"sha256": SHA, "caption": "a dog"})
            p = captions.record_path(work, SHA)
            self.assertEqual(p, pathlib.Path(work) / "captions" / "ab"
                             / f"{SHA}.json")
            self.assertEqual(captions.read_record(work, SHA)["caption"],
                             "a dog")
            self.assertEqual(captions.count(work), 1)

    def test_a_missing_record_reads_as_none(self):
        with tempfile.TemporaryDirectory() as work:
            self.assertIsNone(captions.read_record(work, SHA))

    def test_no_temp_file_is_left_behind(self):
        with tempfile.TemporaryDirectory() as work:
            captions.write_record(work, {"sha256": SHA})
            names = os.listdir(pathlib.Path(work) / "captions" / "ab")
            self.assertEqual(names, [f"{SHA}.json"])


class TestHashCache(unittest.TestCase):
    def test_an_unknown_file_is_hashed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp) / "work"
            f = pathlib.Path(tmp) / "a.jpg"
            f.write_bytes(b"pixels")
            cache = captions.HashCache(work)
            self.assertIsNone(cache.known(f))
            self.assertEqual(cache.get(f),
                             hashlib.sha256(b"pixels").hexdigest())
            self.assertEqual(captions.HashCache(work).known(f), cache.get(f))

    def test_a_changed_file_re_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp) / "work"
            f = pathlib.Path(tmp) / "a.jpg"
            f.write_bytes(b"pixels")
            first = captions.HashCache(work).get(f)
            f.write_bytes(b"other pixels!")
            os.utime(f, (1, 1))
            cache = captions.HashCache(work)
            self.assertIsNone(cache.known(f))
            self.assertNotEqual(cache.get(f), first)

    def test_known_never_reads_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp) / "work"
            f = pathlib.Path(tmp) / "a.jpg"
            f.write_bytes(b"pixels")
            with mock.patch.object(captions, "sha256_of",
                                   side_effect=AssertionError("read")):
                self.assertIsNone(captions.HashCache(work).known(f))


from recall import config, imagery, vision  # noqa: E402
from recall.sources import base  # noqa: E402


def image_file(tmp, name="a.png", w=400, h=300):
    from PIL import Image
    p = pathlib.Path(tmp) / name
    Image.new("RGB", (w, h), (1, 2, 3)).save(p, "PNG")
    return p


@unittest.skipUnless(imagery.available(), "needs the captions extra (Pillow)")
class TestProcess(unittest.TestCase):
    def test_a_caption_is_written_with_the_model_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = image_file(tmp)
            with mock.patch.object(config, "VISION_MODEL", "llava"):
                state = captions.process(f, tmp, captioner=lambda j: "a dog",
                                         ocr=lambda j: "")
            rec = captions.read_record(tmp, captions.sha256_of(f))
        self.assertEqual(state, "ok")
        self.assertEqual(rec["caption"], "a dog")
        self.assertEqual(rec["caption_model"], "llava")
        self.assertEqual(rec["px"], [400, 300])

    def test_a_failed_caption_writes_nothing_so_the_next_run_retries(self):
        def down(jpeg):
            raise vision.CaptionError("502")
        with tempfile.TemporaryDirectory() as tmp:
            f = image_file(tmp)
            state = captions.process(f, tmp, captioner=down, ocr=lambda j: "")
            self.assertIsNone(captions.read_record(tmp, captions.sha256_of(f)))
        self.assertEqual(state, "failed")

    def test_a_tiny_image_is_gated_and_recorded_without_a_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = image_file(tmp, w=100, h=100)
            calls = []
            state = captions.process(
                f, tmp, captioner=lambda j: calls.append(j) or "x",
                ocr=lambda j: "")
            rec = captions.read_record(tmp, captions.sha256_of(f))
        self.assertEqual(state, "gated")
        self.assertEqual(calls, [])
        self.assertIn("below pixel gate", rec["skipped"])

    def test_a_held_record_is_done_without_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = image_file(tmp)
            captions.write_record(tmp, {"sha256": captions.sha256_of(f),
                                        "caption": "old"})
            boom = mock.Mock(side_effect=AssertionError("decoded"))
            state = captions.process(f, tmp, captioner=boom, decoder=boom,
                                     ocr=boom)
        self.assertEqual(state, "done")

    def test_a_non_image_is_skipped_before_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = pathlib.Path(tmp) / "note.jpg"
            f.write_bytes(b"plain text, not pixels")
            with mock.patch.object(captions, "sha256_of",
                                   side_effect=AssertionError("hashed")):
                state = captions.process(f, tmp, captioner=lambda j: "x",
                                         ocr=lambda j: "")
        self.assertEqual(state, "skipped")

    def test_ocr_text_is_kept_only_from_twenty_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = image_file(tmp)
            captions.process(f, tmp, captioner=lambda j: "a sign",
                             ocr=lambda j: "STOP")
            rec = captions.read_record(tmp, captions.sha256_of(f))
            self.assertIsNone(rec["ocr_text"])
            g = image_file(tmp, "b.png", 401, 300)
            captions.process(g, tmp, captioner=lambda j: "a sign",
                             ocr=lambda j: "x" * 20)
            rec = captions.read_record(tmp, captions.sha256_of(g))
            self.assertEqual(rec["ocr_chars"], 20)


class Media(base.Source):
    name = "media"

    def __init__(self, files):
        self.files = files

    def detect(self, root):
        return [root]

    def media(self, path):
        return self.files


@unittest.skipUnless(imagery.available(), "needs the captions extra (Pillow)")
class TestRun(unittest.TestCase):
    def test_it_captions_every_media_file_the_adapters_declare(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = [image_file(tmp, f"{i}.png", 400 + i, 300) for i in range(3)]
            lines = []
            with mock.patch.object(captions, "detect_all",
                                   lambda root: [(Media(files), root)]), \
                 mock.patch.object(config, "VISION_URL", "http://v"), \
                 mock.patch.object(vision, "caption", lambda j: "a thing"), \
                 mock.patch.object(imagery, "ocr", lambda j: ""):
                counts = captions.run(pathlib.Path(tmp), tmp, jobs=2,
                                      log=lines.append)
            self.assertEqual(counts, {"ok": 3})
            self.assertEqual(captions.count(tmp), 3)

    def test_limit_counts_work_not_files_already_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = [image_file(tmp, f"{i}.png", 400 + i, 300) for i in range(4)]
            captions.write_record(tmp, {"sha256": captions.sha256_of(files[0]),
                                        "caption": "x"})
            with mock.patch.object(captions, "detect_all",
                                   lambda root: [(Media(files), root)]), \
                 mock.patch.object(config, "VISION_URL", "http://v"), \
                 mock.patch.object(vision, "caption", lambda j: "a thing"), \
                 mock.patch.object(imagery, "ocr", lambda j: ""):
                counts = captions.run(pathlib.Path(tmp), tmp, jobs=1, limit=2,
                                      log=lambda *a: None)
            self.assertEqual(counts["ok"], 2)
            self.assertEqual(counts["done"], 1)

    def test_no_endpoint_stops_before_any_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "VISION_URL", ""):
                with self.assertRaises(SystemExit) as ctx:
                    captions.run(pathlib.Path(tmp), tmp)
            self.assertIn("RECALL_VISION_URL", str(ctx.exception))


@unittest.skipUnless(imagery.available(), "needs the captions extra (Pillow)")
class TestUncaptioned(unittest.TestCase):
    def test_it_counts_images_without_a_record_and_never_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = image_file(tmp, "a.png")
            b = image_file(tmp, "b.png", 401, 300)
            note = pathlib.Path(tmp) / "c.jpg"
            note.write_bytes(b"text")
            captions.write_record(tmp, {"sha256": captions.HashCache(tmp).get(a),
                                        "caption": "x"})
            with mock.patch.object(captions, "sha256_of",
                                   side_effect=AssertionError("hashed")):
                self.assertEqual(captions.uncaptioned([a, b, note], tmp), (1, 2))


class TestSourceMedia(unittest.TestCase):
    def test_the_default_declares_no_media(self):
        self.assertEqual(list(base.Source().media(pathlib.Path("."))), [])
