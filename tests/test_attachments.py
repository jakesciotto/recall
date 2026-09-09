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
