import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from recall import captions, config
from recall.sources import imessage
from recall.sources.imessage import IMessage

APPLE = 978307200
PREFIX = "~/Library/Messages/Attachments/"
T = 1620000000   # 2021-05-03T00:00:00Z


def make_chatdb(path, messages, attachments=(), names=()):
    """messages: (rowid, chat_guid, handle, unix_ts, is_from_me, text).
    attachments: (filename, message_rowid) or (filename, None, unix_created)
    for a row whose message is gone. names: (chat_guid, display)."""
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, handle_id INTEGER,
        date INTEGER, is_from_me INTEGER, text TEXT, attributedBody BLOB);
    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT,
        created_date INTEGER);
    CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
    """)
    chats, handles = {}, {}
    display = dict(names)
    for rowid, guid, handle, ts, mine, text in messages:
        if guid not in chats:
            chats[guid] = len(chats) + 1
            con.execute("INSERT INTO chat VALUES (?, ?, ?)",
                        (chats[guid], guid, display.get(guid)))
        hid = None
        if handle:
            if handle not in handles:
                handles[handle] = len(handles) + 1
                con.execute("INSERT INTO handle VALUES (?, ?)",
                            (handles[handle], handle))
            hid = handles[handle]
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?, NULL)",
                    (rowid, hid, (ts - APPLE) * 1_000_000_000, int(mine), text))
        con.execute("INSERT INTO chat_message_join VALUES (?, ?)",
                    (chats[guid], rowid))
    for i, (filename, message_rowid, *created) in enumerate(attachments,
                                                            start=1):
        con.execute("INSERT INTO attachment VALUES (?, ?, ?)",
                    (i, filename, created[0] - APPLE if created else None))
        if message_rowid is not None:
            con.execute("INSERT INTO message_attachment_join VALUES (?, ?)",
                        (message_rowid, i))
    con.commit()
    con.close()


def export(tmp, messages, attachments, files, names=()):
    """A data dir with chat.db and an Attachments tree holding `files`."""
    root = pathlib.Path(tmp)
    make_chatdb(root / "chat.db", messages, attachments, names)
    for rel, content in files.items():
        p = root / "Attachments" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return root


class TestMedia(unittest.TestCase):
    def test_local_path_maps_the_mac_prefix_onto_attachments_beside_chat_db(self):
        root = pathlib.Path("/data/Attachments")
        self.assertEqual(imessage.local_path(PREFIX + "ab/GUID/p.jpg", root),
                         root / "ab/GUID/p.jpg")
        self.assertIsNone(imessage.local_path("/somewhere/else.jpg", root))

    def test_media_lists_each_existing_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, [(1, "c1", "+15550001111", T, False, "look")],
                          [(PREFIX + "ab/G1/p.jpg", 1),
                           (PREFIX + "ab/G1/p.jpg", 1),
                           (PREFIX + "cd/G2/gone.jpg", 1)],
                          {"ab/G1/p.jpg": b"\xff\xd8\xff"})
            files = IMessage().media(root / "chat.db")
            self.assertEqual(files, [root / "Attachments/ab/G1/p.jpg"])

    def test_attachments_carry_their_message_even_without_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, [(1, "c1", "+15550001111", T, False, None)],
                          [(PREFIX + "ab/G1/p.jpg", 1)],
                          {"ab/G1/p.jpg": b"\xff\xd8\xff"})
            [(path, msg)] = IMessage()._attachments(root / "chat.db")
        self.assertEqual(msg["thread"], "c1")
        self.assertEqual(msg["handle"], "+15550001111")
        self.assertEqual(msg["at"], T)
        self.assertFalse(msg["mine"])


class TestContextWindow(unittest.TestCase):
    def rows(self, *ats):
        return [{"thread": "c1", "handle": "+1555", "at": a, "mine": False,
                 "text": f"m{a}"} for a in ats]

    def test_three_turns_either_side_within_thirty_minutes(self):
        rows = self.rows(T - 5000, T - 100, T + 30, T + 40, T + 50, T + 60)
        text = imessage.context_window(rows, [r["at"] for r in rows], T, {})
        self.assertEqual(text.splitlines(),
                         [f"+1555: m{T-100}", f"+1555: m{T+30}",
                          f"+1555: m{T+40}", f"+1555: m{T+50}"])

    def test_names_come_from_contacts_and_me_stays_me(self):
        rows = [{"thread": "c1", "handle": "+1555", "at": T + 1, "mine": False,
                 "text": "hi"},
                {"thread": "c1", "handle": "", "at": T + 2, "mine": True,
                 "text": "yo"}]
        text = imessage.context_window(rows, [T + 1, T + 2], T,
                                       {"+1555": "Ada Lovelace"})
        self.assertEqual(text, "Ada Lovelace: hi\nme: yo")


def captioned(work, file, **rec):
    sha = captions.HashCache(work).get(file)
    captions.write_record(work, {"sha256": sha, "caption": None,
                                 "ocr_text": None, "ocr_chars": 0,
                                 "skipped": None, **rec})
    return sha


def attachment_chunks(root, work, budget=8000, contacts=None):
    with mock.patch.object(config, "WORK_DIR", pathlib.Path(work)):
        return [c for c in IMessage().chunks(root / "chat.db", budget, contacts)
                if c.ref.startswith("attachment:")]


MSGS = [(1, "c1", "+15550001111", T - 60, False, "look at this"),
        (2, "c1", "+15550001111", T, False, None),
        (3, "c1", "", T + 30, True, "cute")]
ATT = [(PREFIX + "ab/G1/p.jpg", 2)]
FILES = {"ab/G1/p.jpg": b"\xff\xd8\xff pixels"}


class TestAttachmentChunks(unittest.TestCase):
    def test_a_captioned_image_becomes_one_chunk_dated_by_its_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT, FILES)
            work = root / "work"
            sha = captioned(work, root / "Attachments/ab/G1/p.jpg",
                            caption="a dog on a beach")
            [c] = attachment_chunks(root, work,
                                    contacts={"+15550001111": "Ada Lovelace"})
        self.assertEqual(c.ref, f"attachment:{sha}")
        self.assertEqual(c.source, "messages")
        self.assertEqual(c.occurred_at, "2021-05-03T00:00:00Z")
        self.assertEqual(c.date_confidence, "exact")
        self.assertEqual(c.participants, ["+15550001111"])
        self.assertEqual(c.thread, "c1")
        self.assertEqual(c.text, "[2021-05-03, with Ada Lovelace]\n"
                                 "Image: a dog on a beach\n"
                                 "Said around it: Ada Lovelace: look at this\n"
                                 "me: cute")

    def test_a_group_name_leads_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT, FILES, names=[("c1", "Beach Crew")])
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="sand")
            [c] = attachment_chunks(root, work)
        self.assertTrue(c.text.startswith(
            '[2021-05-03, "Beach Crew" with +15550001111]'))

    def test_ocr_text_is_included_and_cut_to_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT, FILES)
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a sign",
                      ocr_text="MENU " * 200, ocr_chars=1000)
            [c] = attachment_chunks(root, work, budget=200)
        self.assertIn("\nText in image: MENU", c.text)
        self.assertLessEqual(len(c.text), 200)

    def test_ocr_that_cannot_fit_twenty_chars_is_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT, FILES)
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a sign",
                      ocr_text="x" * 100, ocr_chars=100)
            [c] = attachment_chunks(root, work, budget=100)
        self.assertNotIn("Text in image", c.text)
        self.assertLessEqual(len(c.text), 100)

    def test_gated_uncaptioned_and_unhashed_images_yield_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = dict(FILES, **{"ab/G2/q.jpg": b"\xff\xd8\xff other",
                                   "ab/G3/r.jpg": b"\xff\xd8\xff third"})
            att = ATT + [(PREFIX + "ab/G2/q.jpg", 2), (PREFIX + "ab/G3/r.jpg", 2)]
            root = export(tmp, MSGS, att, files)
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg",
                      skipped="below pixel gate")
            captioned(work, root / "Attachments/ab/G2/q.jpg")
            with mock.patch.object(captions, "sha256_of",
                                   side_effect=AssertionError("ingest hashed")):
                self.assertEqual(attachment_chunks(root, work), [])

    def test_the_same_image_sent_twice_is_one_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            att = ATT + [(PREFIX + "cd/G9/copy.jpg", 3)]
            files = dict(FILES, **{"cd/G9/copy.jpg": FILES["ab/G1/p.jpg"]})
            root = export(tmp, MSGS, att, files)
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a dog")
            captions.HashCache(work).get(root / "Attachments/cd/G9/copy.jpg")
            self.assertEqual(len(attachment_chunks(root, work)), 1)


class TestChunksRespectTheBudget(unittest.TestCase):
    """A replay over the real archive yielded a 12,903-char chunk against an
    8,000 budget: the OCR line was cut to fit, the context lines were not,
    and one long pasted message carried the whole chunk over. Swept, because
    a bound that holds at one budget can fail at another."""

    def test_long_context_is_cut_and_the_chunk_still_fits(self):
        msgs = [(1, "c1", "+15550001111", T - 60, False, "y" * 20000),
                (2, "c1", "+15550001111", T, False, None),
                (3, "c1", "", T + 30, True, "z" * 5000)]
        for budget in (300, 1000, 8000):
            with self.subTest(budget=budget), \
                 tempfile.TemporaryDirectory() as tmp:
                root = export(tmp, msgs, ATT, FILES)
                work = root / "work"
                captioned(work, root / "Attachments/ab/G1/p.jpg",
                          caption="a sign", ocr_text="w" * 3000, ocr_chars=3000)
                [c] = attachment_chunks(root, work, budget=budget)
                self.assertLessEqual(len(c.text), budget)
                self.assertIn("\nSaid around it: ", c.text)
                self.assertIn("\nText in image: ", c.text)


class TestTheEarliestMessageWins(unittest.TestCase):
    """An image sent twice has two links. Without an order the first link
    the query returned won, and it flipped between runs: an ingest rewrote
    five chunks that nothing had changed."""

    def test_a_repeated_image_dates_by_its_first_message(self):
        msgs = [(9, "c1", "+15550001111", T + 9000, False, "again"),
                (2, "c1", "+15550001111", T, False, "first time")]
        att = [(PREFIX + "zz/G9/later.jpg", 9), (PREFIX + "ab/G1/p.jpg", 2)]
        files = {"zz/G9/later.jpg": FILES["ab/G1/p.jpg"],
                 "ab/G1/p.jpg": FILES["ab/G1/p.jpg"]}
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, msgs, att, files)
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a dog")
            captions.HashCache(work).get(root / "Attachments/zz/G9/later.jpg")
            [c] = attachment_chunks(root, work)
        self.assertEqual(c.occurred_at, "2021-05-03T00:00:00Z")
        self.assertIn("first time", c.text)


class TestOrphans(unittest.TestCase):
    """An image no attachment row names: the message is gone, the file
    stayed. It indexes undated rather than not at all."""

    HEIC = b"\x00\x00\x00\x18ftypheic"

    def test_media_declares_an_orphan_and_skips_a_live_photo_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT, {
                **FILES,
                "cd/G2/IMG_1.heic": self.HEIC,
                "cd/G2/at_0_X.pvt/IMG_1.jpeg": b"\xff\xd8\xff",
                "cd/G2/at_0_X.pvt/metadata.plist": b"bplist00"})
            files = IMessage().media(root / "chat.db")
            rel = sorted(str(f.relative_to(root / "Attachments"))
                         for f in files)
        self.assertEqual(rel, ["ab/G1/p.jpg", "cd/G2/IMG_1.heic"])

    def test_a_captioned_orphan_is_one_undated_chunk_without_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT, {**FILES, "cd/G2/IMG_1.heic": self.HEIC})
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a dog")
            sha = captioned(work, root / "Attachments/cd/G2/IMG_1.heic",
                            caption="a red bicycle by a fence",
                            ocr_text="x" * 30, ocr_chars=30)
            chunks = attachment_chunks(root, work)
        self.assertEqual(len(chunks), 2)
        [c] = [c for c in chunks if c.ref == f"attachment:{sha}"]
        self.assertEqual(c.text, "[undated, attachment with no message]\n"
                                 "Image: a red bicycle by a fence\n"
                                 "Text in image: " + "x" * 30)
        self.assertIsNone(c.occurred_at)
        self.assertEqual(c.date_confidence, "low")
        self.assertEqual(c.participants, [])
        self.assertIsNone(c.thread)
        self.assertEqual(c.source, "messages")

    def test_an_orphan_with_a_linked_twin_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT,
                          {**FILES, "cd/G2/copy.jpg": FILES["ab/G1/p.jpg"]})
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a dog")
            captions.HashCache(work).get(root / "Attachments/cd/G2/copy.jpg")
            chunks = attachment_chunks(root, work)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].date_confidence, "exact")

    def test_an_orphan_chunk_fits_the_budget(self):
        for budget in (120, 500, 3000):
            with self.subTest(budget=budget), \
                 tempfile.TemporaryDirectory() as tmp:
                root = export(tmp, [], [], {"cd/G2/IMG_1.heic": self.HEIC})
                work = root / "work"
                captioned(work, root / "Attachments/cd/G2/IMG_1.heic",
                          caption="a cat", ocr_text="y" * 5000, ocr_chars=5000)
                [c] = attachment_chunks(root, work, budget=budget)
                self.assertLessEqual(len(c.text), budget)
                self.assertIn("Text in image: ", c.text)

    def test_an_orphan_row_without_a_message_dates_by_its_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT + [(PREFIX + "cd/G2/IMG_1.heic", None, T)],
                          {**FILES, "cd/G2/IMG_1.heic": self.HEIC})
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a dog")
            sha = captioned(work, root / "Attachments/cd/G2/IMG_1.heic",
                            caption="a red bicycle by a fence")
            chunks = attachment_chunks(root, work)
        [c] = [c for c in chunks if c.ref == f"attachment:{sha}"]
        self.assertEqual(c.text, "[2021-05-03, attachment with no message]\n"
                                 "Image: a red bicycle by a fence")
        self.assertEqual(c.occurred_at, "2021-05-03T00:00:00Z")
        self.assertEqual(c.date_confidence, "metadata")
        self.assertEqual(c.participants, [])
        self.assertIsNone(c.thread)

    def test_a_chat_photo_links_to_its_chat_by_directory_name(self):
        guid = "iMessage;+;chat42"
        msgs = MSGS + [(9, guid, "+15550002222", T + 100, False, "hey all")]
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, msgs, ATT,
                          {**FILES, f"ab/{guid}/GroupPhotoImage": b"\x89PNG"},
                          names=[(guid, "Beach Crew")])
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a dog")
            sha = captioned(work, root / f"Attachments/ab/{guid}/GroupPhotoImage",
                            caption="three people on a pier")
            chunks = attachment_chunks(
                root, work, contacts={"+15550002222": "Grace Hopper"})
        [c] = [c for c in chunks if c.ref == f"attachment:{sha}"]
        self.assertEqual(c.text, '[undated, chat photo of "Beach Crew" with '
                                 'Grace Hopper]\nImage: three people on a pier')
        self.assertIsNone(c.occurred_at)
        self.assertEqual(c.date_confidence, "low")
        self.assertEqual(c.participants, ["+15550002222"])
        self.assertEqual(c.thread, guid)

    def test_an_old_attachment_table_without_created_date_still_yields(self):
        if sqlite3.sqlite_version_info < (3, 35):
            self.skipTest("DROP COLUMN needs SQLite 3.35")
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, MSGS, ATT, {**FILES, "cd/G2/IMG_1.heic": self.HEIC})
            con = sqlite3.connect(root / "chat.db")
            con.execute("ALTER TABLE attachment DROP COLUMN created_date")
            con.commit()
            con.close()
            work = root / "work"
            captioned(work, root / "Attachments/ab/G1/p.jpg", caption="a dog")
            sha = captioned(work, root / "Attachments/cd/G2/IMG_1.heic",
                            caption="a red bicycle by a fence")
            chunks = attachment_chunks(root, work)
        [c] = [c for c in chunks if c.ref == f"attachment:{sha}"]
        self.assertIsNone(c.occurred_at)
        self.assertTrue(c.text.startswith("[undated, attachment with no message]"))



class TestRows(unittest.TestCase):
    """A message joined to two chats is one event. Yielded once per chat,
    it made two windows share one ref, and every ingest rewrote one of
    them while the other never reached the index."""

    def test_a_message_in_two_chats_is_one_row_in_the_first_chat_by_guid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = export(tmp, [(1, "b-chat", "+15550001111", T, False, "hello")],
                          [], {})
            con = sqlite3.connect(root / "chat.db")
            con.execute("INSERT INTO chat VALUES (2, 'a-chat', NULL)")
            con.execute("INSERT INTO chat_message_join VALUES (2, 1)")
            con.commit()
            con.close()
            rows, _ = IMessage()._rows(root / "chat.db")
            refs = [c.ref for c in IMessage().chunks(root / "chat.db", 8000)]
        self.assertEqual([(r["rowid"], r["thread"]) for r in rows],
                         [(1, "a-chat")])
        self.assertEqual(len(refs), len(set(refs)))
