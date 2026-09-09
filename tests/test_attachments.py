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
    attachments: (filename, message_rowid). names: (chat_guid, display)."""
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
    CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, handle_id INTEGER,
        date INTEGER, is_from_me INTEGER, text TEXT, attributedBody BLOB);
    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT);
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
    for i, (filename, message_rowid) in enumerate(attachments, start=1):
        con.execute("INSERT INTO attachment VALUES (?, ?)", (i, filename))
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
