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
