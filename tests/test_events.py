"""The structured store: what the adapters yield for it, and how it loads."""

import contextlib
import unittest
from unittest import mock

from recall import db


class Copy:
    def __init__(self, log, sql):
        self.log = log
        self.sql = sql
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append((self.sql, self.rows))
        return False

    def write_row(self, row):
        self.rows.append(tuple(row))


class Cursor:
    def __init__(self, log):
        self.log = log
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    rowcount = -1

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self.result = [len([r for s, r in self.log if "COPY" in s])]
        if sql.strip().startswith("INSERT INTO"):
            table = sql.split()[2]
            copied = [r for s, r in self.log if isinstance(s, str)
                      and s.startswith(f"COPY {table}_in")]
            self.rowcount = len({r[1] for r in copied[0]}) if copied else 0

    def copy(self, sql):
        return Copy(self.log, sql)

    def fetchone(self):
        return self.result


class Conn:
    def __init__(self):
        self.log = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self.log)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def statements(self):
        return [s for s, _ in self.log]


EVENT = {"ref": "message:1", "at": "2021-05-03T00:00:00Z", "thread": "c1",
         "sender": "+15550001111", "mine": False, "kind": "text", "chars": 4}
THREAD = {"thread": "c1", "name": None, "members": ["+15550001111"]}
ALIAS = {"handle": "42", "label": "ada"}


class TestSchema(unittest.TestCase):
    def test_the_four_tables_exist(self):
        for table in ("event", "thread", "alias", "computed"):
            with self.subTest(table=table):
                self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} ",
                              db.STATS_SCHEMA)

    def test_apply_schema_runs_it(self):
        conn = Conn()
        db.apply_schema(conn, dims=4)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS event " in s
                            for s in conn.statements()))


class TestReplaceEvents(unittest.TestCase):
    def test_the_load_is_copy_then_delete_then_insert_in_one_commit(self):
        conn = Conn()
        db.replace_events(conn, "messages", [EVENT], [THREAD], [ALIAS])
        kinds = [("copy" if "COPY" in s else
                  "delete" if s.strip().startswith("DELETE") else
                  "insert" if s.strip().startswith("INSERT") else "other")
                 for s in conn.statements()]
        self.assertLess(kinds.index("copy"), kinds.index("delete"))
        self.assertLess(kinds.index("delete"), kinds.index("insert"))
        self.assertEqual(conn.commits, 1)

    def test_every_table_is_replaced_for_the_source_only(self):
        conn = Conn()
        db.replace_events(conn, "messages", [EVENT], [THREAD], [ALIAS])
        deletes = [(s, p) for s, p in conn.log
                   if isinstance(s, str) and s.strip().startswith("DELETE")]
        self.assertEqual(sorted(s.split()[2] for s, _ in deletes),
                         ["alias", "event", "thread"])
        for s, p in deletes:
            with self.subTest(statement=s):
                self.assertIn("WHERE source = %s", s)
                self.assertEqual(p, ["messages"])

    def test_clear_false_keeps_the_rows_of_an_earlier_path(self):
        """One source can own several files, and the second file must not
        wipe what the first loaded."""
        conn = Conn()
        db.replace_events(conn, "email", [EVENT], [], [], clear=False)
        self.assertFalse(any(s.strip().startswith("DELETE")
                             for s in conn.statements()))

    def test_a_duplicate_ref_inside_one_export_is_not_an_error(self):
        conn = Conn()
        db.replace_events(conn, "messages", [EVENT, dict(EVENT)], [], [])
        inserts = [s for s in conn.statements()
                   if s.strip().startswith("INSERT INTO event")]
        self.assertEqual(len(inserts), 1)
        self.assertIn("ON CONFLICT DO NOTHING", inserts[0])

    def test_rows_carry_the_source_and_every_column(self):
        conn = Conn()
        db.replace_events(conn, "messages", [EVENT], [THREAD], [ALIAS])
        copied = {s.split("COPY ")[1].split(" ")[0]: rows
                  for s, rows in conn.log if isinstance(s, str) and "COPY" in s}
        self.assertEqual(copied["event_in"][0],
                         ("messages", "message:1", "2021-05-03T00:00:00Z",
                          "c1", "+15550001111", False, "text", 4))
        self.assertEqual(copied["thread_in"][0],
                         ("messages", "c1", None, ["+15550001111"], False))
        self.assertEqual(copied["alias_in"][0], ("messages", "42", "ada"))

    def test_it_returns_what_landed_not_what_it_was_given(self):
        conn = Conn()
        counts = db.replace_events(conn, "messages", [EVENT, dict(EVENT)],
                                   [THREAD], [ALIAS])
        self.assertEqual(counts, {"events": 1, "threads": 1, "aliases": 1})

    def test_a_failure_rolls_back(self):
        class Failing(Conn):
            def cursor(self):
                cur = Cursor(self.log)
                cur.copy = lambda sql: (_ for _ in ()).throw(RuntimeError("x"))
                return cur
        conn = Failing()
        with self.assertRaises(RuntimeError):
            db.replace_events(conn, "messages", [EVENT], [], [])
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(conn.commits, 0)


class TestSaveComputed(unittest.TestCase):
    def test_it_upserts_by_ref(self):
        conn = Conn()
        db.save_computed(conn, "stats:count?x#abcd", "table", "messages")
        s, p = conn.log[-1]
        self.assertIn("INSERT INTO computed", s)
        self.assertIn("ON CONFLICT (ref) DO UPDATE", s)
        self.assertEqual(p, ["stats:count?x#abcd", "table", "messages"])
        self.assertEqual(conn.commits, 1)


# ---- adapters -------------------------------------------------------------

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_attachments import T, make_chatdb          # noqa: E402
from test_sources import conversation, dm, tweet, twitter_export  # noqa: E402

from recall.sources import base, imessage, mbox, twitter  # noqa: E402

MESSAGES = [
    (1, "c1", "+15550001111", T, False, "hi"),
    (2, "c1", "+15550001111", T + 60, True, "hello"),
    (3, "g1", "+15550002222", T + 120, False, "group hi"),
    (4, "g1", "", T + 180, True, "group reply"),
    (5, "c1", "+15550001111", T + 240, False, None),      # a tapback
    (6, "g1", "", T + 300, True, None),                   # a system row
    (7, "c1", "+15550001111", T + 360, False, None),      # a photo alone
    (8, "c1", "+15550001111", T + 420, False, None),      # nothing at all
    (9, "c1", "+15550001111", T + 86400 * 400, False, "next year"),
]


def chatdb(root):
    """MESSAGES in chat.db, with 5 a tapback, 6 a system row, 7 an
    attachment-only row, and 9 joined to both chats."""
    make_chatdb(root / "chat.db", MESSAGES, names=[("g1", "the gang")])
    con = sqlite3.connect(root / "chat.db")
    con.execute("UPDATE message SET associated_message_type = 2000 WHERE ROWID = 5")
    con.execute("UPDATE message SET item_type = 1 WHERE ROWID = 6")
    con.execute("UPDATE message SET cache_has_attachments = 1 WHERE ROWID = 7")
    con.execute("INSERT INTO chat_message_join VALUES (2, 9)")
    # A third member of the group who never wrote. chat_handle_join names
    # every member; the messages alone would make the group look direct.
    con.execute("INSERT INTO handle VALUES (3, '+15550003333')")
    con.execute("INSERT INTO chat_handle_join VALUES (2, 3)")
    con.execute("UPDATE chat SET style = 43 WHERE guid = 'g1'")
    con.commit()
    con.close()
    return root / "chat.db"


def digest(chunks):
    return hashlib.md5("\n".join(f"{c.ref}\n{c.text}"
                                 for c in chunks).encode()).hexdigest()


class TestBaseContract(unittest.TestCase):
    def test_a_source_yields_nothing_for_the_store_by_default(self):
        src = base.Source()
        self.assertEqual(list(src.events("p")), [])
        self.assertEqual(list(src.threads("p")), [])
        self.assertEqual(list(src.aliases("p")), [])


class TestMessageEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = chatdb(pathlib.Path(self.tmp.name))
        self.src = imessage.IMessage()

    def tearDown(self):
        self.tmp.cleanup()

    def events(self):
        return {e["ref"]: e for e in self.src.events(self.path)}

    def test_one_event_per_message_even_when_joined_to_two_chats(self):
        events = list(self.src.events(self.path))
        self.assertEqual(len(events), len(MESSAGES))
        self.assertEqual(len({e["ref"] for e in events}), len(MESSAGES))
        self.assertEqual(self.events()["message:9"]["thread"], "c1")

    def test_every_kind_is_read_from_the_row(self):
        kinds = {ref: e["kind"] for ref, e in self.events().items()}
        self.assertEqual(kinds, {
            "message:1": "text", "message:2": "text", "message:3": "text",
            "message:4": "text", "message:5": "tapback", "message:6": "system",
            "message:7": "attachment", "message:8": "empty", "message:9": "text"})

    def test_a_photo_sent_alone_is_an_attachment_not_a_text(self):
        """On a real export 25,072 messages with an attachment decode to
        nothing but U+FFFC, the object replacement character."""
        for body, attached, kind in (("\ufffc", 1, "attachment"),
                                     ("\ufffc\ufffc ", 1, "attachment"),
                                     ("\ufffc", 0, "empty"),
                                     ("\ufffc look", 1, "text")):
            with self.subTest(body=body, attached=attached):
                self.assertEqual(imessage.kind_of(body, 0, 0, attached), kind)
        self.assertEqual(imessage.chars_of("\ufffc look"), 4)

    def test_the_user_has_no_sender_and_the_other_side_keeps_its_handle(self):
        events = self.events()
        self.assertEqual(events["message:2"]["sender"], "")
        self.assertTrue(events["message:2"]["mine"])
        self.assertEqual(events["message:1"]["sender"], "+15550001111")
        self.assertFalse(events["message:1"]["mine"])

    def test_time_is_iso_utc_and_chars_is_the_body_length(self):
        e = self.events()["message:3"]
        self.assertEqual(e["at"], "2021-05-03T00:02:00Z")
        self.assertEqual(e["chars"], len("group hi"))

    def test_threads_carry_their_members_and_name(self):
        threads = {t["thread"]: t for t in self.src.threads(self.path)}
        self.assertEqual(threads["c1"]["members"], ["+15550001111"])
        self.assertEqual(threads["g1"]["members"], ["+15550002222", "+15550003333"])
        self.assertEqual(threads["g1"]["name"], "the gang")
        self.assertIsNone(threads["c1"]["name"])

    def test_direct_comes_from_the_chat_style_not_the_member_count(self):
        """Two group-style chats on a real export have one member left."""
        threads = {t["thread"]: t for t in self.src.threads(self.path)}
        self.assertTrue(threads["c1"]["direct"])
        self.assertFalse(threads["g1"]["direct"])
        import sqlite3
        con = sqlite3.connect(self.path)
        con.execute("UPDATE chat SET style = 43 WHERE guid = 'c1'")
        con.commit()
        con.close()
        threads = {t["thread"]: t for t in imessage.IMessage().threads(self.path)}
        self.assertFalse(threads["c1"]["direct"])

    def test_the_chunk_output_did_not_move(self):
        """Pinned before the adapter learned to yield events."""
        rows, names = self.src._rows(self.path)
        contacts = {"+15550001111": "Ada"}
        out = list(self.src._windows(rows, names, contacts, 2000))
        out += list(imessage.trend_chunks(rows, contacts, 2000, dt.timezone.utc))
        self.assertEqual(digest(out), "d9511328d701a8f0192702cffb73a5d3")


MAIL = """From a@example.org Mon Jun  4 12:00:00 2018
From: Ada <ada@example.org>
To: me@example.org
Cc: bo@example.org
Subject: lunch
Date: Mon, 4 Jun 2018 12:00:00 +0000
Message-ID: <m1@example.org>
X-GM-THRID: 7
X-Gmail-Labels: Category Personal,Inbox
Content-Type: text/plain

are we on?

From me@example.org Mon Jun  4 12:05:00 2018
From: Me <me@example.org>
To: ada@example.org, bo@example.org
Subject: Re: lunch
Date: Mon, 4 Jun 2018 12:05:00 +0000
Message-ID: <m2@example.org>
X-GM-THRID: 7
X-Gmail-Labels: Sent
Content-Type: text/plain

yes

From bo@example.org Mon Jun  4 12:06:00 2018
From: Bo <bo@example.org>
To: me@example.org
Subject: Re: lunch
Date: Mon, 4 Jun 2018 12:06:00 +0000
Message-ID: <m3@example.org>
X-GM-THRID: 7
X-Gmail-Labels: Category Personal
Content-Type: text/plain


From shop@example.org Mon Jun  4 13:00:00 2018
From: shop@example.org
To: me@example.org
Subject: 20 percent off
Date: Mon, 4 Jun 2018 13:00:00 +0000
Message-ID: <m4@example.org>
X-GM-THRID: 8
X-Gmail-Labels: Category Promotions
Content-Type: text/plain

sale

"""


class TestMailEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "mail.mbox"
        self.path.write_text(MAIL)
        self.src = mbox.Mbox()

    def tearDown(self):
        self.tmp.cleanup()

    def test_kept_mail_is_an_event_with_or_without_a_body(self):
        events = {e["ref"]: e for e in self.src.events(self.path)}
        self.assertEqual(set(events), {"mail:<m1@example.org>",
                                       "mail:<m2@example.org>",
                                       "mail:<m3@example.org>"})
        self.assertEqual(events["mail:<m3@example.org>"]["chars"], 0)
        self.assertEqual(events["mail:<m1@example.org>"]["kind"], "mail")
        self.assertEqual(events["mail:<m1@example.org>"]["thread"], "7")
        self.assertEqual(events["mail:<m1@example.org>"]["at"],
                         "2018-06-04T12:00:00Z")

    def test_the_sent_label_makes_it_mine_with_no_sender(self):
        events = {e["ref"]: e for e in self.src.events(self.path)}
        self.assertTrue(events["mail:<m2@example.org>"]["mine"])
        self.assertEqual(events["mail:<m2@example.org>"]["sender"], "")
        self.assertEqual(events["mail:<m1@example.org>"]["sender"],
                         "ada@example.org")

    def test_members_are_every_address_but_the_users_own(self):
        """The user's addresses are the From of every Sent message."""
        threads = {t["thread"]: t for t in self.src.threads(self.path)}
        self.assertEqual(threads["7"]["members"],
                         ["ada@example.org", "bo@example.org"])
        self.assertEqual(threads["7"]["name"], "lunch")
        self.assertFalse(threads["7"]["direct"])
        self.assertNotIn("8", threads)

    def test_the_chunk_output_did_not_move(self):
        """Pinned under the zone the capture ran in. chunks() reads the
        zone from the environment, and a runner with no RECALL_TZ writes
        a different rollup header."""
        from zoneinfo import ZoneInfo
        from recall import trends
        with mock.patch.object(trends, "zone",
                               return_value=ZoneInfo("America/Denver")):
            out = list(self.src.chunks(self.path, 2000, {"ada@example.org": "Ada"}))
        self.assertEqual([c.ref for c in out], ["email:trends:2018", "email:7"])
        self.assertEqual(digest(out), "6ea644147ec0b90ea36ae31e7cb08237")


def group_conversation(cid, snapshot, messages):
    return {"dmConversation": {"conversationId": cid, "messages": [
        {"joinConversation": {"initiatingUserId": snapshot[0],
                              "participantsSnapshot": list(snapshot),
                              "createdAt": "2018-06-20T11:00:00.000Z"}},
        *messages]}}


class TestTwitterEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tweets = [tweet("hi @ada", mentions=[("222", "ada")])]
        dms = [conversation("111-222", [dm("1", "222", "yo"),
                                        dm("2", "111", "hey")])]
        self.data = twitter_export(self.tmp.name, tweets, dms, me="111")
        groups = [group_conversation("9001", ["111", "222", "333"],
                                     [dm("3", "333", "all"), dm("4", "111", "ok")])]
        (self.data / "direct-messages-group.js").write_text(
            "window.YTD.direct_messages_group.part0 = " + json.dumps(groups),
            encoding="utf-8")
        self.src = twitter.Twitter()

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_dm_is_an_event(self):
        events = {e["ref"]: e for e in self.src.events(self.data)}
        self.assertEqual(set(events), {"dm:1", "dm:2", "dm:3", "dm:4"})
        self.assertEqual(events["dm:1"]["sender"], "222")
        self.assertFalse(events["dm:1"]["mine"])
        self.assertEqual(events["dm:2"]["sender"], "")
        self.assertTrue(events["dm:2"]["mine"])
        self.assertEqual(events["dm:1"]["kind"], "dm")
        self.assertEqual(events["dm:1"]["thread"], "111-222")
        self.assertEqual(events["dm:3"]["thread"], "9001")

    def test_a_direct_conversation_names_its_member_in_its_id(self):
        threads = {t["thread"]: t for t in self.src.threads(self.data)}
        self.assertEqual(threads["111-222"]["members"], ["222"])
        self.assertTrue(threads["111-222"]["direct"])
        self.assertFalse(threads["9001"]["direct"])

    def test_a_group_takes_the_snapshot_and_the_senders_minus_me(self):
        threads = {t["thread"]: t for t in self.src.threads(self.data)}
        self.assertEqual(threads["9001"]["members"], ["222", "333"])

    def test_aliases_name_the_ids_the_tweets_resolve(self):
        self.assertEqual(list(self.src.aliases(self.data)),
                         [{"handle": "222", "label": "ada"}])


# ---- ingest ---------------------------------------------------------------

from unittest import mock  # noqa: E402

from recall import ingest  # noqa: E402


class Chatty(base.Source):
    """Two folders of one source, each with a chunk and an event."""

    name = "chatty"

    def detect(self, root):
        return [root / "a", root / "b"]

    def samples(self, path):
        return ["x" * 100]

    def chunks(self, path, budget, contacts=None):
        yield base.Chunk(ref=f"chatty:{path.name}", text="hi", source=self.name,
                         occurred_at="2020-01-01T00:00:00Z",
                         date_confidence="exact")

    def events(self, path):
        yield {"ref": f"{path.name}:1", "at": "2020-01-01T00:00:00Z",
               "thread": "t", "sender": "x", "mine": False, "kind": "text",
               "chars": 2}

    def threads(self, path):
        yield {"thread": "t", "name": None, "members": ["x"]}


class Quiet(base.Source):
    name = "quiet"

    def detect(self, root):
        return [root]

    def samples(self, path):
        return ["x" * 100]

    def chunks(self, path, budget, contacts=None):
        yield base.Chunk(ref="quiet:1", text="hi", source=self.name,
                         occurred_at="2020-01-01T00:00:00Z",
                         date_confidence="exact")


class StoreDb:
    def __init__(self):
        self.replaced = []

    def apply_schema(self, conn):
        pass

    def stored_digests(self, conn):
        return {}

    def upsert(self, conn, rows):
        pass

    def build_vector_index(self, conn):
        pass

    def replace_events(self, conn, source, events, threads, aliases, clear=True):
        events, threads, aliases = list(events), list(threads), list(aliases)
        self.replaced.append((source, clear, events, threads, aliases))
        return {"events": len(events), "threads": len(threads),
                "aliases": len(aliases)}


class NoConn:
    def commit(self):
        pass


def run_ingest(adapter, store):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        with mock.patch.object(ingest, "db", store), \
             mock.patch.object(ingest.chunking, "calibrate", return_value=8000), \
             mock.patch.object(ingest.embedding, "embed_safe",
                               lambda g, d, w: (g, [[0.0]] * len(g))), \
             mock.patch.object(ingest, "detect_all",
                               lambda r: [(adapter, p) for p in adapter.detect(r)]):
            return ingest.run(root, NoConn(), log=lambda *a: None)


class TestIngestLoadsTheStore(unittest.TestCase):
    def test_a_second_folder_of_one_source_does_not_clear_the_first(self):
        store = StoreDb()
        summary = run_ingest(Chatty(), store)
        self.assertEqual([(s, c) for s, c, *_ in store.replaced],
                         [("chatty", True), ("chatty", False)])
        self.assertEqual(summary["chatty"]["events"], 2)
        self.assertEqual(summary["chatty"]["threads"], 2)

    def test_the_rows_reach_the_store_as_the_adapter_yielded_them(self):
        store = StoreDb()
        run_ingest(Chatty(), store)
        _, _, events, threads, aliases = store.replaced[0]
        self.assertEqual(events[0]["ref"], "a:1")
        self.assertEqual(threads[0]["members"], ["x"])
        self.assertEqual(aliases, [])

    def test_a_source_with_no_events_never_touches_the_store(self):
        store = StoreDb()
        summary = run_ingest(Quiet(), store)
        self.assertEqual(store.replaced, [])
        self.assertNotIn("events", summary["quiet"])


VCARD = """BEGIN:VCARD
FN:Ada Lovelace
TEL:+1 555 123 4567
END:VCARD
"""


class TestIngestStoresTheContacts(unittest.TestCase):
    def test_the_vcard_map_lands_as_aliases_under_contacts(self):
        store = StoreDb()
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "contacts.vcf").write_text(VCARD)
            with mock.patch.object(ingest, "db", store), \
                 mock.patch.object(ingest.chunking, "calibrate", return_value=8000), \
                 mock.patch.object(ingest.embedding, "embed_safe",
                                   lambda g, d, w: (g, [[0.0]] * len(g))), \
                 mock.patch.object(ingest, "detect_all", lambda r: [(Quiet(), r)]):
                ingest.run(root, NoConn(), log=lambda *a: None)
        self.assertEqual(store.replaced, [("contacts", True, [], [],
                                           [{"handle": "+15551234567",
                                             "label": "Ada Lovelace"}])])
