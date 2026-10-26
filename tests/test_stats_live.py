"""The stats queries against a real Postgres, on a scratch database.

The unit tests pin the SQL's shape and the Python folds. Only a running
Postgres can say the SQL counts what the fixtures hold, so this file
loads every source fixture through the real adapters and the real load,
then compares each metric with counts made by hand. It is marked live
and skips when the configured server is unreachable; CI has no database.
"""

import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

import pytest

from recall import config, db, stats
from recall.sources import imessage, mbox, twitter

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_events import MAIL, chatdb, group_conversation  # noqa: E402
from test_sources import conversation, dm, tweet, twitter_export  # noqa: E402

pytestmark = pytest.mark.live

SCRATCH = "recall_stats_test"
TZ = dt.timezone.utc
CONTACTS = {"+15550001111": "Ada Lovelace", "ada@example.org": "Ada Lovelace",
            "+15550002222": "Bo Peep", "bo@example.org": "Bo Peep"}


def scratch_dsn():
    base, _ = config.PG_DSN.rsplit("/", 1)
    return f"{base}/{SCRATCH}"


def make_scratch():
    import psycopg
    try:
        admin = psycopg.connect(config.PG_DSN, autocommit=True, connect_timeout=3)
    except Exception as e:
        pytest.skip(f"no Postgres at {config.PG_DSN}: {type(e).__name__}")
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
    admin.execute(f"CREATE DATABASE {SCRATCH}")
    admin.close()


def drop_scratch():
    import psycopg
    admin = psycopg.connect(config.PG_DSN, autocommit=True)
    admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH}")
    admin.close()


def load(conn, source, adapter, path):
    db.replace_events(conn, source, adapter.events(path), adapter.threads(path),
                      adapter.aliases(path))


class TestCountsAgainstPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_scratch()
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        chat = chatdb(root)
        mail = root / "mail.mbox"
        mail.write_text(MAIL)
        tweets = [tweet("hi @ada", mentions=[("222", "ada")])]
        dms = [conversation("111-222", [dm("1", "222", "yo"), dm("2", "111", "hey")])]
        data = twitter_export(root / "tw", tweets, dms, me="111")
        groups = [group_conversation("9001", ["111", "222", "333"],
                                     [dm("3", "333", "all"), dm("4", "111", "ok")])]
        (data / "direct-messages-group.js").write_text(
            "window.YTD.direct_messages_group.part0 = " + json.dumps(groups))
        with db.connect(scratch_dsn()) as conn:
            db.apply_schema(conn, dims=4)
            load(conn, "messages", imessage.IMessage(), chat)
            load(conn, "email", mbox.Mbox(), mail)
            load(conn, "twitter", twitter.Twitter(), data)
            db.replace_events(conn, stats.CONTACTS, [], [],
                              [{"handle": h, "label": n} for h, n in CONTACTS.items()])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        drop_scratch()

    def run_call(self, call):
        with db.connect(scratch_dsn()) as conn:
            return stats.run(conn, call, stats.labels(conn, call.get("source", "messages")), TZ)

    def test_the_store_holds_every_row_once(self):
        with db.connect(scratch_dsn()) as conn:
            rows = db.fetch(conn, "SELECT source, count(*) AS n FROM event GROUP BY 1 ORDER BY 1")
        self.assertEqual({r["source"]: r["n"] for r in rows},
                         {"messages": 9, "email": 3, "twitter": 4})

    def test_count_with_a_person_by_year(self):
        r = self.run_call({"metric": "count", "with": "Ada Lovelace"})
        self.assertEqual(r["rows"], [
            {"period": "2021", "sent_direct": 1, "received_direct": 2,
             "sent_group": 0, "received_group": 0, "excluded": 2},
            {"period": "2022", "sent_direct": 0, "received_direct": 1,
             "sent_group": 0, "received_group": 0, "excluded": 0}])
        self.assertEqual((r["first"], r["last"]),
                         (dt.date(2021, 5, 3), dt.date(2022, 6, 7)))
        self.assertEqual(r["streak"][0], 1)
        self.assertEqual(r["gap"], (399, dt.date(2021, 5, 3), dt.date(2022, 6, 7)))

    def test_count_with_a_group_member(self):
        r = self.run_call({"metric": "count", "with": "Bo Peep", "by": "all"})
        self.assertEqual(r["rows"], [
            {"period": "all", "sent_direct": 0, "received_direct": 0,
             "sent_group": 1, "received_group": 1, "excluded": 1}])

    def test_count_of_the_whole_source(self):
        r = self.run_call({"metric": "count", "by": "all"})
        self.assertEqual(r["rows"], [{"period": "all", "sent": 2, "received": 4,
                                      "excluded": 3}])

    def test_top_all_years_and_one_year(self):
        r = self.run_call({"metric": "top"})
        self.assertEqual(r["rows"], [
            {"label": "Ada Lovelace", "sent": 1, "received": 3, "groups": 0},
            {"label": "Bo Peep", "sent": 0, "received": 1, "groups": 1},
            {"label": "+15550003333", "sent": 0, "received": 0, "groups": 1}])
        r = self.run_call({"metric": "top", "year": 2022})
        self.assertEqual(r["rows"], [
            {"label": "Ada Lovelace", "sent": 0, "received": 1, "groups": 0}])

    def test_streaks(self):
        r = self.run_call({"metric": "streaks"})
        self.assertEqual([(p["label"], p["days"], p["streak"][0]) for p in r["streaks"]],
                         [("Ada Lovelace", 2, 1), ("Bo Peep", 1, 1)])
        self.assertEqual(r["gaps"], [])

    def test_mail_counts_a_two_person_thread_as_a_group(self):
        r = self.run_call({"metric": "count", "source": "email", "with": "Ada", "by": "all"})
        self.assertEqual(r["rows"], [
            {"period": "all", "sent_direct": 0, "received_direct": 0,
             "sent_group": 1, "received_group": 1, "excluded": 0}])

    def test_twitter_resolves_a_screen_name_through_the_alias(self):
        r = self.run_call({"metric": "count", "source": "twitter", "with": "ada", "by": "all"})
        self.assertEqual(r["handles"], ["222"])
        self.assertEqual(r["rows"], [
            {"period": "all", "sent_direct": 1, "received_direct": 1,
             "sent_group": 1, "received_group": 0, "excluded": 0}])

    def test_a_hit_is_readable_back_through_the_judge_join(self):
        from recall import judge
        call = stats.validate({"metric": "count", "with": "Ada Lovelace"})
        with db.connect(scratch_dsn()) as conn:
            hit = stats.hit(conn, call, tz=TZ, today=dt.date(2026, 10, 26))
            with conn.cursor() as cur:
                cur.execute("INSERT INTO query_log (client, question, k, pool) "
                            "VALUES ('test', 'q', 8, 50) RETURNING id")
                qid = cur.fetchone()[0]
                cur.execute("INSERT INTO query_candidate (query_id, ref, rrf_score, "
                            "final_rank) VALUES (%s, %s, 1.0, 1)", [qid, hit["ref"]])
            sources = judge.sources_for(conn, qid)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["text"], hit["text"])
        self.assertEqual(sources[0]["source"], "messages")
