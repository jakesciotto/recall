import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from recall import trends

DENVER = ZoneInfo("America/Denver")


def at(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


class TestByYearLocal(unittest.TestCase):
    """Years and weekdays are local. A message at 23:30 on New Year's Eve
    in Denver is 06:30 on 1 January in UTC, and the stored timestamps are
    UTC."""

    def test_the_year_is_the_local_year(self):
        groups = trends.by_year([at("2024-01-01T06:30:00")],
                                lambda x: x, DENVER)
        self.assertEqual(list(groups), [2023])

    def test_the_weekday_is_the_local_weekday(self):
        # 2024-01-01 06:30 UTC is Sunday 2023-12-31 23:30 in Denver.
        self.assertEqual(trends.weekday_counts([at("2024-01-01T06:30:00")],
                                               lambda x: x, DENVER),
                         {"Sunday": 1})


class TestStreak(unittest.TestCase):
    def test_the_longest_run_of_consecutive_days(self):
        days = {dt.date(2023, 3, d) for d in (1, 2, 3, 5, 6, 7, 8, 10)}
        self.assertEqual(trends.longest_streak(days),
                         (4, dt.date(2023, 3, 5), dt.date(2023, 3, 8)))

    def test_no_days_is_no_streak(self):
        self.assertEqual(trends.longest_streak(set()), (0, None, None))


class TestHours(unittest.TestCase):
    def test_peak_hours_are_described_in_local_time(self):
        # 03:15 UTC is 21:15 in Denver in July (UTC-6).
        stamps = [at("2023-07-01T03:15:00")] * 5 + [at("2023-07-01T15:00:00")]
        text = trends.describe_hours(stamps, lambda x: x, DENVER)
        self.assertIn("peak hour 21:00", text)
        self.assertNotIn("03:00", text)


class TestServiceHeuristic(unittest.TestCase):
    """The eval asked whether the top sender was a person or a service.
    A heuristic answers that for the rollup, and is named as one."""

    def test_obvious_services(self):
        for addr in ("noreply@x.com", "no-reply@x.com", "auto-confirm@amazon.com",
                     "deltaairlines@t.delta.com", "news@shop.example",
                     "notifications@github.com"):
            with self.subTest(addr=addr):
                self.assertTrue(trends.looks_like_service(addr))

    def test_a_person(self):
        self.assertFalse(trends.looks_like_service("ada@example.org"))
        self.assertFalse(trends.looks_like_service("Ada Lovelace"))


class TestChunk(unittest.TestCase):
    def test_the_chunk_names_the_year_the_source_and_the_zone(self):
        cs = list(trends.chunks("messages", 2023, ["a line"], 5000, DENVER))
        self.assertEqual(len(cs), 1)
        c = cs[0]
        self.assertEqual(c.ref, "messages:trends:2023")
        self.assertEqual(c.occurred_at, "2023-01-01T00:00:00Z")
        self.assertEqual(c.date_confidence, "period")
        self.assertIn("2023", c.text)
        self.assertIn("America/Denver", c.text)
        self.assertIn("a line", c.text)

    def test_the_text_fits_the_budget(self):
        cs = list(trends.chunks("messages", 2023, ["x" * 300] * 40, 2000, DENVER))
        for c in cs:
            self.assertLessEqual(len(c.text), 2000)
        self.assertEqual(len({c.ref for c in cs}), len(cs))
