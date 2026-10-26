"""Counts from the structured store, and the planner that asks for them."""

import datetime as dt
import json
import unittest
from unittest import mock

from recall import retrieve, stats

TZ = dt.timezone.utc


class Cursor:
    def __init__(self, log, result):
        self.log = log
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchone(self):
        # The driver parses the json column; the fake hands the rows back.
        return [self.result.pop(0) if self.result else []]


class Conn:
    """Answers every fetch with the next canned row set."""

    def __init__(self, *results):
        self.log = []
        self.results = list(results)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return Cursor(self.log, self.results)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


LABELS = {"+15550001111": "Ada Lovelace", "ada@example.org": "Ada Lovelace",
          "+15550002222": "Bo Peep", "+15550003333": "Jen Smith",
          "+15550004444": "Jenny Carter", "222": "ada"}


class TestValidate(unittest.TestCase):
    def test_defaults_fill_what_the_call_left_out(self):
        self.assertEqual(stats.validate({"metric": "count"}),
                         {"metric": "count", "source": "messages",
                          "with": None, "by": "year"})
        self.assertEqual(stats.validate({"metric": "top"}),
                         {"metric": "top", "source": "messages", "year": None,
                          "direction": "all", "n": 5})
        self.assertEqual(stats.validate({"metric": "streaks"}),
                         {"metric": "streaks", "source": "messages", "n": 5})

    def test_an_invalid_call_runs_nothing(self):
        """The invariant: a call outside the catalogue is dropped before
        any query, whether the planner or the terminal wrote it."""
        bad = [{"metric": "sql"}, {"metric": "count", "by": "decade"},
               {"metric": "count", "source": "notes"},
               {"metric": "count", "where": "1=1"}, {"metric": "top", "n": 0},
               {"metric": "top", "n": 999}, {"metric": "top", "year": 1066},
               {"metric": "top", "direction": "both"}, "count", None, 7]
        for call in bad:
            with self.subTest(call=call):
                self.assertIsNone(stats.validate(call))
                conn = Conn()
                with self.assertRaises(ValueError):
                    stats.run(conn, call, LABELS, TZ)
                self.assertEqual(conn.log, [])
        self.assertEqual(
            stats.parse_plan(json.dumps(bad[:2] + [{"metric": "streaks"}])),
            [{"metric": "streaks", "source": "messages", "n": 5}])

    def test_a_year_arrives_as_text_or_number(self):
        for year in (2019, "2019", 2019.0):
            with self.subTest(year=year):
                self.assertEqual(stats.validate({"metric": "top", "year": year})["year"],
                                 2019)

    def test_an_empty_person_is_no_person(self):
        self.assertIsNone(stats.validate({"metric": "count", "with": "  "})["with"])
        self.assertEqual(stats.validate({"metric": "count", "with": " Ada "})["with"],
                         "Ada")


class TestResolve(unittest.TestCase):
    def test_a_name_owns_every_identifier_mapped_to_it(self):
        handles, label, candidates = stats.resolve("ada lovelace", LABELS, "messages")
        self.assertEqual(handles, ["+15550001111", "ada@example.org"])
        self.assertEqual(label, "Ada Lovelace")
        self.assertEqual(candidates, [])

    def test_a_substring_that_matches_one_name_resolves(self):
        self.assertEqual(stats.resolve("peep", LABELS, "messages")[0],
                         ["+15550002222"])

    def test_a_substring_that_matches_several_names_lists_them(self):
        handles, label, candidates = stats.resolve("jen", LABELS, "messages")
        self.assertEqual(handles, [])
        self.assertEqual(candidates, ["Jen Smith", "Jenny Carter"])

    def test_a_raw_phone_is_normalised_and_used(self):
        handles, label, _ = stats.resolve("(555) 000-9999", LABELS, "messages")
        self.assertEqual(handles, ["+15550009999"])
        self.assertEqual(label, "+15550009999")

    def test_a_raw_email_is_used_as_it_is(self):
        self.assertEqual(stats.resolve("Zed@Example.org", LABELS, "email")[0],
                         ["zed@example.org"])

    def test_a_twitter_id_is_not_a_phone(self):
        self.assertEqual(stats.resolve("3207564556", LABELS, "twitter")[0],
                         ["3207564556"])

    def test_no_match_is_empty_with_no_candidates(self):
        self.assertEqual(stats.resolve("nobody", LABELS, "messages"),
                         ([], "nobody", []))


class TestFolds(unittest.TestCase):
    def test_the_longest_gap_counts_the_silent_days_between_two_active_days(self):
        days = [dt.date(2021, 1, 1), dt.date(2021, 1, 2), dt.date(2021, 1, 10),
                dt.date(2021, 3, 1)]
        self.assertEqual(stats.longest_gap(days),
                         (49, dt.date(2021, 1, 10), dt.date(2021, 3, 1)))

    def test_one_day_has_no_gap(self):
        self.assertEqual(stats.longest_gap([dt.date(2021, 1, 1)]), (0, None, None))


class TestRef(unittest.TestCase):
    def test_the_ref_names_the_call_and_the_digest_of_the_text(self):
        call = stats.validate({"metric": "count", "with": "Ada", "by": "year"})
        ref = stats.ref(call, "table one")
        self.assertTrue(ref.startswith("stats:count?by=year&source=messages&with=Ada#"))
        self.assertEqual(ref, stats.ref(call, "table one"))
        self.assertNotEqual(ref, stats.ref(call, "table two"))
        self.assertEqual(len(ref.split("#")[1]), 8)


class TestCountQuery(unittest.TestCase):
    def test_the_person_is_bound_never_interpolated(self):
        """The invariant: a planner or a terminal argument reaches Postgres
        as a parameter only."""
        # A raw email passes through resolve as it is, so this is the
        # value that reaches the query. A label never does: only the
        # handles it maps to.
        hostile = "ada'; drop table event; --@example.org"
        conn = Conn([], [])
        stats.run(conn, {"metric": "count", "with": hostile}, LABELS, TZ)
        self.assertTrue(conn.log)
        for sql, params in conn.log:
            with self.subTest(sql=sql[:40]):
                self.assertNotIn("drop table", sql)
                self.assertNotIn("example.org", sql)
                self.assertEqual(params.get("handles"), [hostile])

    def test_with_a_person_it_splits_direct_and_group_in_both_directions(self):
        rows = [{"period": "2021", "sent_direct": 1, "received_direct": 2,
                 "sent_group": 1, "received_group": 1, "excluded": 3}]
        days = [{"day": "2021-05-03"}, {"day": "2021-05-04"}, {"day": "2022-06-07"}]
        conn = Conn(rows, days)
        result = stats.run(conn, {"metric": "count", "with": "ada lovelace"},
                           LABELS, TZ)
        self.assertEqual(result["label"], "Ada Lovelace")
        self.assertEqual(result["rows"], rows)
        self.assertEqual(result["first"], dt.date(2021, 5, 3))
        self.assertEqual(result["last"], dt.date(2022, 6, 7))
        self.assertEqual(result["streak"], (2, dt.date(2021, 5, 3), dt.date(2021, 5, 4)))
        self.assertEqual(result["gap"], (398, dt.date(2021, 5, 4), dt.date(2022, 6, 7)))
        sql = conn.log[0][0]
        for column in ("sent_direct", "received_direct", "sent_group",
                       "received_group", "excluded"):
            self.assertIn(column, sql)
        self.assertIn("members && %(handles)s", sql)

    def test_without_a_person_it_counts_the_whole_source(self):
        conn = Conn([{"period": "all", "sent": 5, "received": 7, "excluded": 1}], [])
        result = stats.run(conn, {"metric": "count", "by": "all"}, LABELS, TZ)
        self.assertIsNone(result["label"])
        self.assertNotIn("thread", conn.log[0][0].split("FROM event")[1].split("WHERE")[0])
        self.assertEqual(result["rows"][0]["sent"], 5)

    def test_the_period_follows_by(self):
        for by, expr in (("year", "'YYYY'"), ("month", "'YYYY-MM'"), ("all", "'all'")):
            with self.subTest(by=by):
                conn = Conn([], [])
                stats.run(conn, {"metric": "count", "by": by}, LABELS, TZ)
                self.assertIn(expr, conn.log[0][0])

    def test_an_unknown_person_runs_no_query(self):
        conn = Conn()
        result = stats.run(conn, {"metric": "count", "with": "nobody"}, LABELS, TZ)
        self.assertEqual(conn.log, [])
        self.assertEqual(result["handles"], [])

    def test_an_ambiguous_person_runs_no_query_and_names_the_candidates(self):
        conn = Conn()
        result = stats.run(conn, {"metric": "count", "with": "jen"}, LABELS, TZ)
        self.assertEqual(conn.log, [])
        self.assertEqual(result["candidates"], ["Jen Smith", "Jenny Carter"])


class TestTopQuery(unittest.TestCase):
    def test_members_resolve_to_a_person_inside_the_query(self):
        """A contact with a phone and an email in one group is one member,
        so the query joins the stored contacts map and groups by label."""
        rows = [{"who": "Ada Lovelace", "sent": 4, "received": 3, "groups": 1},
                {"who": "Bo Peep", "sent": 2, "received": 2, "groups": 0}]
        conn = Conn(rows)
        result = stats.run(conn, {"metric": "top", "n": 2}, LABELS, TZ)
        self.assertEqual(result["rows"], [
            {"label": "Ada Lovelace", "sent": 4, "received": 3, "groups": 1},
            {"label": "Bo Peep", "sent": 2, "received": 2, "groups": 0}])
        sql, params = conn.log[0]
        self.assertIn("SELECT DISTINCT t.thread, coalesce(a.label, h) AS who", sql)
        self.assertIn("LEFT JOIN alias a ON a.handle = e.sender", sql)
        self.assertEqual(params["contacts"], "contacts")
        self.assertEqual(params["limit"], 2)

    def test_a_year_bounds_the_query_in_the_local_zone(self):
        conn = Conn([])
        tz = dt.timezone(dt.timedelta(hours=-7))
        stats.run(conn, {"metric": "top", "year": 2019}, LABELS, tz)
        sql, params = conn.log[0]
        self.assertIn("e.occurred_at >= %(since)s", sql)
        self.assertEqual(params["since"], "2019-01-01T07:00:00+00:00")
        self.assertEqual(params["until"], "2020-01-01T07:00:00+00:00")

    def test_the_rank_follows_the_direction(self):
        for direction, order in (("sent", "ORDER BY sent DESC"),
                                 ("received", "ORDER BY received DESC"),
                                 ("all", "ORDER BY sent + received DESC")):
            with self.subTest(direction=direction):
                conn = Conn([])
                stats.run(conn, {"metric": "top", "direction": direction}, LABELS, TZ)
                self.assertIn(order, conn.log[0][0])


class TestStreaksQuery(unittest.TestCase):
    def test_streaks_and_gaps_fold_per_person(self):
        rows = [{"who": "Ada Lovelace", "day": "2021-01-01"},
                {"who": "Ada Lovelace", "day": "2021-01-02"},
                {"who": "Ada Lovelace", "day": "2021-01-03"},
                {"who": "Ada Lovelace", "day": "2021-01-04"},
                {"who": "Bo Peep", "day": "2021-01-01"},
                {"who": "Bo Peep", "day": "2021-03-01"}]
        conn = Conn(rows)
        result = stats.run(conn, {"metric": "streaks", "n": 5}, LABELS, TZ)
        self.assertEqual(result["streaks"][0],
                         {"label": "Ada Lovelace", "days": 4,
                          "streak": (4, dt.date(2021, 1, 1), dt.date(2021, 1, 4)),
                          "gap": (0, None, None)})
        self.assertEqual([g["label"] for g in result["gaps"]], [])

    def test_a_gap_needs_enough_active_days_to_mean_anything(self):
        rows = [{"who": "Bo Peep", "day": f"2021-01-{d:02d}"}
                for d in range(1, 31)] + [{"who": "Bo Peep", "day": "2021-06-01"}]
        conn = Conn(rows)
        result = stats.run(conn, {"metric": "streaks"}, LABELS, TZ)
        self.assertEqual(result["gaps"][0]["label"], "Bo Peep")
        self.assertEqual(result["gaps"][0]["gap"][0], 121)


class TestRender(unittest.TestCase):
    def test_a_count_renders_its_definitions_and_its_table(self):
        rows = [{"period": "2021", "sent_direct": 1, "received_direct": 2,
                 "sent_group": 1, "received_group": 1, "excluded": 3}]
        conn = Conn(rows, [{"day": "2021-05-03"}])
        result = stats.run(conn, {"metric": "count", "with": "ada lovelace"},
                           LABELS, TZ)
        text = stats.render(result, today=dt.date(2026, 10, 26), tz=TZ)
        self.assertTrue(text.startswith("[stats: count, messages with Ada Lovelace, by year, times in UTC]"))
        self.assertIn("2026-10-26", text)
        self.assertIn("excluded 3", text)
        self.assertIn("direct", text)
        self.assertIn("2021 | 1 | 2 | 1 | 1", text)
        self.assertIn("First 2021-05-03", text)
        self.assertNotIn("+15550001111", text)

    def test_a_one_period_count_renders_as_sentences_not_a_table(self):
        """A 26B model crossed the columns of a one-row pipe table and
        reported the group count as the direct count."""
        rows = [{"period": "all", "sent_direct": 5748, "received_direct": 6291,
                 "sent_group": 748, "received_group": 1631, "excluded": 2498}]
        conn = Conn(rows, [{"day": "2017-05-29"}])
        result = stats.run(conn, {"metric": "count", "with": "ada lovelace", "by": "all"},
                           LABELS, TZ)
        text = stats.render(result, today=dt.date(2026, 10, 26), tz=TZ)
        self.assertIn("Direct thread: sent 5,748, received 6,291.", text)
        self.assertIn("Group threads: sent 748, received 1,631.", text)
        self.assertNotIn("period |", text)
        whole = stats.run(Conn([{"period": "all", "sent": 2, "received": 4,
                                 "excluded": 1}], []),
                          {"metric": "count", "by": "all"}, LABELS, TZ)
        text = stats.render(whole, today=dt.date(2026, 10, 26), tz=TZ)
        self.assertIn("Sent 2, received 4.", text)
        self.assertNotIn("period |", text)

    def test_a_zero_count_is_one_plain_sentence(self):
        """A model read "Total 0 ... No rows." past and answered from mail.
        The sentence has to say what zero means."""
        conn = Conn([], [])
        result = stats.run(conn, {"metric": "count", "with": "ada lovelace", "by": "all"},
                           LABELS, TZ)
        text = stats.render(result, today=dt.date(2026, 10, 26), tz=TZ)
        self.assertIn("No messages with Ada Lovelace in messages", text)
        self.assertIn("holds no message in any thread that includes Ada Lovelace", text)
        self.assertNotIn("No rows", text)
        self.assertNotIn("Total 0", text)

    def test_candidates_render_as_a_question_not_a_count(self):
        conn = Conn()
        result = stats.run(conn, {"metric": "count", "with": "jen"}, LABELS, TZ)
        text = stats.render(result, today=dt.date(2026, 10, 26))
        self.assertIn("Jen Smith", text)
        self.assertIn("Jenny Carter", text)
        self.assertIn("counted nothing", text)

    def test_no_match_says_so(self):
        conn = Conn()
        result = stats.run(conn, {"metric": "count", "with": "nobody"}, LABELS, TZ)
        text = stats.render(result, today=dt.date(2026, 10, 26))
        self.assertIn('No contact or identifier matches "nobody"', text)
        self.assertIn("counted nothing", text)

    def test_top_and_streaks_render_one_line_per_person(self):
        top = stats.run(Conn([{"who": "Bo Peep", "sent": 2, "received": 2,
                               "groups": 0}]), {"metric": "top"}, LABELS, TZ)
        self.assertIn("Bo Peep | 2 | 2 | 0", stats.render(top, today=dt.date(2026, 1, 1)))
        streaks = stats.run(Conn([{"who": "Bo Peep", "day": "2021-01-01"}]),
                            {"metric": "streaks"}, LABELS, TZ)
        self.assertIn("Bo Peep | 1 day", stats.render(streaks, today=dt.date(2026, 1, 1)))


class TestParsePlan(unittest.TestCase):
    def test_null_is_no_call(self):
        for text in ("null", " null\n", "NULL", "```json\nnull\n```"):
            with self.subTest(text=text):
                self.assertEqual(stats.parse_plan(text), [])

    def test_a_fenced_list_and_a_bare_object_both_parse(self):
        call = {"metric": "count", "with": "Ada", "by": "year"}
        clean = stats.validate(call)
        self.assertEqual(stats.parse_plan("```json\n" + json.dumps([call]) + "\n```"),
                         [clean])
        self.assertEqual(stats.parse_plan(json.dumps(call)), [clean])

    def test_at_most_three_calls_survive(self):
        calls = [{"metric": "top", "year": y} for y in (2017, 2018, 2019, 2020)]
        self.assertEqual(len(stats.parse_plan(json.dumps(calls))), 3)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            stats.parse_plan("I think you want the count metric")


class TestPlan(unittest.TestCase):
    def test_no_endpoint_means_no_call_and_no_request(self):
        with mock.patch.object(stats.config, "CHAT_URL", ""):
            plan = stats.plan("how many texts to Ada")
        self.assertEqual(plan, stats.Plan([], None, 0, None))

    def test_the_model_answer_becomes_calls(self):
        seen = {}

        def chat(prompt, **kw):
            seen.update(prompt=prompt, **kw)
            return json.dumps([{"metric": "count", "with": "Ada"}])
        plan = stats.plan("how many texts to Ada", chat=chat)
        self.assertEqual(plan.calls, [stats.validate({"metric": "count", "with": "Ada"})])
        self.assertIn("how many texts to Ada", seen["prompt"])
        self.assertEqual(seen["system"], stats.PLANNER)
        self.assertIsNone(plan.error)

    def test_an_unparseable_answer_is_an_error_not_a_crash(self):
        plan = stats.plan("q", chat=lambda prompt, **kw: "sure, count?")
        self.assertEqual(plan.calls, [])
        self.assertIn("ValueError", plan.error)
        self.assertEqual(plan.raw, "sure, count?")

    def test_the_catalogue_prompt_names_every_metric_and_the_null_rule(self):
        for word in ("count", "top", "streaks", "null", "messages", "email",
                     "twitter"):
            with self.subTest(word=word):
                self.assertIn(word, stats.PLANNER)


def _search(hits):
    def search_traced(question, conn, embedder, k=8, pool=50, source=None,
                      today=None, dates=None):
        search_traced.calls.append(k)
        return (list(hits[:k]), retrieve.NO_DATES,
                {"dense": [], "sparse": [], "fused": [(h["ref"], 0.01) for h in hits[:k]],
                 "dense_n": 0, "sparse_n": 0, "fused_n": len(hits[:k]),
                 "embed_ms": 1, "dense_ms": 1, "sparse_ms": 1})
    search_traced.calls = []
    return search_traced


def _hit(ref="stats:count?by=year&source=messages&with=Ada#deadbeef"):
    return {"ref": ref, "text": "[stats: count]", "occurred_at": None,
            "source": "messages", "path": None, "score": 1.0}


CHUNKS = [{"ref": f"message:{i}", "text": "t", "occurred_at": None,
           "source": "messages", "path": None, "score": 0.01} for i in range(8)]


class TestGather(unittest.TestCase):
    def gather(self, planner, hit=None, k=8):
        search = _search(CHUNKS)
        call = stats.validate({"metric": "count", "with": "Ada"})
        conn = Conn()
        with mock.patch.object(retrieve, "search_traced", search), \
             mock.patch.object(stats, "hit", hit or (lambda *a, **kw: _hit())):
            hits, dates, trace = stats.gather("q", conn, lambda t: [0.0], k=k,
                                              planner=planner)
        return hits, trace, search.calls, conn, call

    def test_a_planned_table_leads_the_sources_and_takes_one_of_k(self):
        call = stats.validate({"metric": "count", "with": "Ada"})
        hits, trace, ks, _, _ = self.gather(lambda q: stats.Plan([call], "[...]", 5, None))
        self.assertEqual(hits[0]["ref"], _hit()["ref"])
        self.assertEqual(len(hits), 8)
        self.assertEqual(ks, [7])
        self.assertEqual(trace["fused"][0], (_hit()["ref"], 1.0))
        self.assertEqual(trace["fused_n"], 8)
        self.assertEqual(trace["plan_ms"], 5)
        self.assertEqual(json.loads(trace["plan"])["calls"], [call])

    def test_no_plan_changes_nothing(self):
        hits, trace, ks, _, _ = self.gather(lambda q: stats.Plan([], "null", 3, None))
        self.assertEqual([h["ref"] for h in hits], [c["ref"] for c in CHUNKS])
        self.assertEqual(ks, [8])
        self.assertIsNone(trace["plan"])
        self.assertEqual(trace["plan_ms"], 3)

    def test_a_planner_that_raises_never_costs_the_answer(self):
        """The invariant. The planner is a model call on the answer path,
        and a model call fails; the ask then runs as if nothing was asked."""
        def planner(q):
            raise RuntimeError("model down")
        hits, trace, ks, _, _ = self.gather(planner)
        self.assertEqual(len(hits), 8)
        self.assertEqual(ks, [8])
        self.assertIn("model down", json.loads(trace["plan"])["error"])

    def test_a_call_that_fails_rolls_back_and_the_rest_continues(self):
        call = stats.validate({"metric": "count", "with": "Ada"})

        def hit(conn, c, **kw):
            raise RuntimeError("no such table")
        hits, trace, ks, conn, _ = self.gather(
            lambda q: stats.Plan([call], "[...]", 5, None), hit=hit)
        self.assertEqual(len(hits), 8)
        self.assertEqual(conn.rollbacks, 1)
        self.assertIn("no such table", json.loads(trace["plan"])["error"])

    def test_k_never_drops_below_one(self):
        call = stats.validate({"metric": "count", "with": "Ada"})
        hits, trace, ks, _, _ = self.gather(
            lambda q: stats.Plan([call, call, call], "[...]", 5, None), k=2)
        self.assertEqual(ks, [1])
        self.assertEqual(len(hits), 4)


class TestHit(unittest.TestCase):
    def test_it_renders_saves_and_returns_a_source(self):
        rows = [{"period": "2021", "sent_direct": 1, "received_direct": 2,
                 "sent_group": 0, "received_group": 0, "excluded": 0}]
        conn = Conn([{"handle": "+15550001111", "label": "Ada Lovelace"}],
                    rows, [{"day": "2021-05-03"}])
        call = stats.validate({"metric": "count", "with": "Ada Lovelace"})
        hit = stats.hit(conn, call, tz=TZ, today=dt.date(2026, 10, 26))
        self.assertTrue(hit["ref"].startswith("stats:count?"))
        self.assertIn("2021 | 1 | 2 | 0 | 0", hit["text"])
        self.assertEqual(hit["source"], "messages")
        self.assertEqual(hit["score"], 1.0)
        saved = [(s, p) for s, p in conn.log if "INSERT INTO computed" in s]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][1][:2], [hit["ref"], hit["text"]])
        self.assertEqual(stats.ref(call, hit["text"]), hit["ref"])
