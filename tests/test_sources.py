import io
import pathlib
import sqlite3
import struct
import tempfile
import unittest

from recall.sources import files, imessage, mbox


class TestMboxSeparator(unittest.TestCase):
    """A new message starts at a line beginning "From " followed by an
    address and a date. "From now on..." at the start of a body line is
    ordinary prose, and splitting on it tears a message in half."""

    def test_it_splits_real_messages(self):
        raw = (b"From 1@x Sun Aug 16 23:54:20 +0000 2026\nSubject: a\n\nbody\n"
               b"From 2@x Mon Aug 17 10:00:00 +0000 2026\nSubject: b\n\nbody\n")
        self.assertEqual(len(list(mbox.raw_messages(io.BytesIO(raw)))), 2)

    def test_prose_beginning_with_from_does_not_split(self):
        raw = (b"From 1@x Sun Aug 16 23:54:20 +0000 2026\nSubject: a\n\n"
               b"From now on I will write daily.\n")
        self.assertEqual(len(list(mbox.raw_messages(io.BytesIO(raw)))), 1)


class TestMboxFilter(unittest.TestCase):
    """An allowlist, not a denylist. A receipt carries BOTH Category
    Purchases and Category Updates, so "skip anything tagged Updates" throws
    away real receipts."""

    def keep(self, labels):
        return bool({x.strip() for x in labels.split(",")} & mbox.KEEP_LABELS)

    def test_personal_mail_is_kept(self):
        self.assertTrue(self.keep("Category Personal,Inbox"))

    def test_a_pure_promotion_is_dropped(self):
        self.assertFalse(self.keep("Category Promotions,Inbox,Opened"))

    def test_a_receipt_survives_being_tagged_updates_too(self):
        self.assertTrue(self.keep("Category Purchases,Category Updates,Inbox"))

    def test_the_keep_set_never_contains_the_noise_labels(self):
        self.assertNotIn("Category Updates", mbox.KEEP_LABELS)
        self.assertNotIn("Inbox", mbox.KEEP_LABELS)


class TestMboxQuoting(unittest.TestCase):
    def test_a_quoted_reply_is_removed(self):
        got = mbox.strip_quoted(
            "Yes.\n\nOn Sun, Aug 16 Ann wrote:\n> original question")
        self.assertIn("Yes.", got)
        self.assertNotIn("original question", got)

    def test_angle_quoted_lines_go(self):
        self.assertNotIn(">", mbox.strip_quoted("new\n> old"))

    def test_unquoted_text_survives_intact(self):
        self.assertEqual(mbox.strip_quoted("Just a note."), "Just a note.")


def streamtyped(body):
    """Minimal Apple archive carrying one string, for the decoder test."""
    payload = body.encode()
    return (b"\x04\x0bstreamtyped\x81\xe8\x03NSString\x01\x94\x84\x01+"
            + bytes([len(payload)]) + payload)


class TestAttributedBody(unittest.TestCase):
    """On modern macOS `message.text` is NULL for 99.7 percent of rows. An
    adapter that reads it captures a fraction of a percent of the corpus and
    reports no error at all."""

    def test_it_decodes_the_body(self):
        self.assertEqual(
            imessage.decode_attributed_body(streamtyped("hello there")),
            "hello there")

    def test_an_empty_blob_returns_empty(self):
        self.assertEqual(imessage.decode_attributed_body(b""), "")
        self.assertEqual(imessage.decode_attributed_body(None), "")

    def test_a_blob_without_nsstring_returns_empty(self):
        self.assertEqual(imessage.decode_attributed_body(b"nonsense"), "")

    def test_a_two_byte_length_is_read(self):
        body = "y" * 400
        blob = (b"\x04\x0bstreamtyped\x81\xe8\x03NSString\x01\x94\x84\x01+"
                + b"\x81" + struct.pack("<H", len(body)) + body.encode())
        self.assertEqual(imessage.decode_attributed_body(blob), body)


class TestAppleTimestamps(unittest.TestCase):
    """Apple stores seconds or nanoseconds since 2001 depending on version.
    Reading nanoseconds as seconds dates a message to the year 4000."""

    def test_seconds_convert(self):
        self.assertEqual(imessage._unix(0), imessage.APPLE_EPOCH)

    def test_nanoseconds_convert(self):
        self.assertEqual(imessage._unix(1_000_000_000_000),
                         1000 + imessage.APPLE_EPOCH)


class TestFileExclusions(unittest.TestCase):
    """A filter anchored to a rooted path fails OPEN once the tree moves: it
    stops matching while the include rules keep matching. Match on segments."""

    def p(self, s):
        return pathlib.Path(s)

    def test_a_normal_document_is_kept(self):
        self.assertTrue(files.keep(self.p("notes/trip.md")))

    def test_a_vendored_directory_is_skipped_at_any_depth(self):
        self.assertFalse(files.keep(self.p("a/b/node_modules/c/index.js")))
        self.assertFalse(files.keep(self.p("node_modules/x.js")))

    def test_a_cache_directory_is_skipped(self):
        self.assertFalse(files.keep(self.p("deep/Caches/thumb.png")))

    def test_a_pirated_ebook_hint_is_skipped(self):
        self.assertFalse(files.keep(self.p("books/z-library-thing.pdf")))

    def test_a_binary_disguised_as_text_is_not_read(self):
        """A Photoshop file named .pdf becomes megabytes of noise if you
        trust the extension."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"\x00\x01\x02binary")
            name = f.name
        self.assertEqual(files.read_text(pathlib.Path(name)), "")


class TestChunkContract(unittest.TestCase):
    def test_a_chunk_carries_everything_the_loader_needs(self):
        from recall.sources.base import Chunk, REQUIRED_KEYS
        c = Chunk(ref="r", text="t", source="s").as_dict()
        self.assertTrue(REQUIRED_KEYS <= set(c), REQUIRED_KEYS - set(c))

    def test_date_confidence_defaults_to_low_not_exact(self):
        """A guessed date and a real one must never look alike downstream."""
        from recall.sources.base import Chunk
        self.assertEqual(Chunk(ref="r", text="t", source="s").date_confidence,
                         "low")
