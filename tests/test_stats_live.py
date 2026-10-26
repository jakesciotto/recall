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
    db.replace_events(conn, source,
                      [e for e in adapter.events(path) if not e.get("source")],
                      adapter.threads(path), adapter.aliases(path))


BENCH = {
    "how many texts have I sent to Ada Lovelace over time": "count",
    "whom did I text most in 2019, and whom in 2021? Did the top contact change?": "top",
    "which contact shows the longest unbroken messaging streak, and over which dates?": "streaks",
    "did I ever message Bo Peep?": "count",
    "when did I first and last message Bo Peep, and how many messages did we exchange?": "count",
    "how many messages have I exchanged with Ada Lovelace in group chats compared with one-to-one?": "count",
    "who sent me the most email in 2021, and is that sender a person or a service?": "top",
    "how often did I DM ada on twitter each year?": "count",
    "when did I stop tweeting regularly, and what was my last tweet before the gap?": "count",
    "what did I write about the Chicago trip in 2018": None,
    "when did I last see the dentist": None,
    "show the email where Bo Peep and I planned a dinner": None,
    "what was on my calendar on 2011-06-15?": None,
}
BENCH_FLOOR = 11


class TestPlannerBench(unittest.TestCase):
    """The planner is a model, so it is measured, not trusted. Thirteen
    questions, nine the store can count and four it cannot. The floor
    leaves room for one miss; a second miss is a prompt regression or a
    model change worth reading."""

    def test_the_planner_plans_the_bench(self):
        if not config.CHAT_URL:
            self.skipTest("no RECALL_CHAT_URL")
        from recall import answer
        hits, misses = 0, []
        for question, want in BENCH.items():
            plan = stats.plan(question, chat=answer.chat)
            got = [c["metric"] for c in plan.calls]
            if (want is None and not got) or (want in got):
                hits += 1
            else:
                misses.append((question, want, got, plan.error))
        print(f"\nplanner bench: {hits} of {len(BENCH)}; misses: {misses}")
        self.assertGreaterEqual(hits, BENCH_FLOOR, misses)


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
            tw = twitter.Twitter()
            load(conn, "twitter", tw, data)
            db.replace_events(conn, "tweets", [e for e in tw.events(data)
                                               if e.get("source") == "tweets"], [], [])
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
                         {"messages": 9, "email": 3, "twitter": 4, "tweets": 1})

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

    def test_a_short_code_member_is_in_no_ranking(self):
        r = self.run_call({"metric": "top", "n": 10})
        self.assertNotIn("78156", [row["label"] for row in r["rows"]])
        r = self.run_call({"metric": "streaks", "n": 10})
        self.assertNotIn("78156", [p["label"] for p in r["streaks"]])

    def test_tweets_count_by_year_with_their_gap(self):
        r = self.run_call({"metric": "count", "source": "tweets", "by": "year"})
        self.assertEqual(r["rows"], [{"period": "2018", "sent": 1, "received": 0,
                                      "excluded": 0}])
        self.assertEqual(r["first"], dt.date(2018, 6, 20))

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
