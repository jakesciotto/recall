"""Google Takeout My Activity: searches, visits, watches, one file per product.

The export is HTML unless JSON was chosen, and both carry the same three
things per entry: a verb, a title, and a local timestamp with a zone name.
The adapter rolls them up by product and month, the way Spotify rolls up
plays, and never keeps a URL or a location.
"""

import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from recall.sources import activity


def cell(verb, title, when, extra=None, url="https://example.com/x",
         caption=""):
    """One entry in the real markup, narrow spaces included."""
    body = f'{verb}\xa0<a href="{url}">{title}</a><br>' if verb else \
        f'{title}<br>'
    if extra:
        body += f'<a href="https://example.com/c">{extra}</a><br>'
    body += f"{when}<br>"
    return (
        '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
        '<div class="mdl-grid"><div class="header-cell mdl-cell mdl-cell--12-col">'
        '<p class="mdl-typography--title">Search<br></p></div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        f'{body}</div>'
        '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 '
        'mdl-typography--text-right"></div>'
        '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
        f'<b>Products:</b><br>&emsp;Search<br>{caption}</div></div></div>')


def page(*cells):
    return "<html><body>" + "".join(cells) + "</body></html>"


FEB16 = "Feb 16, 2024, 9:50:17 PM MDT"


class TestHtmlEntries(unittest.TestCase):
    def test_an_entry_yields_verb_title_and_utc_time(self):
        [r] = activity.parse_html(page(cell("Searched for", "ski resort trail map", FEB16)))
        self.assertEqual((r["verb"], r["title"], r["at"]),
                         ("Searched for", "ski resort trail map", "2024-02-17T03:50:17Z"))

    def test_a_channel_line_is_the_extra(self):
        [r] = activity.parse_html(page(cell("Watched", "Zsh prompt", FEB16,
                                            extra="Mr Fugu")))
        self.assertEqual(r["extra"], "Mr Fugu")

    def test_the_url_never_reaches_the_record(self):
        [r] = activity.parse_html(page(cell("Visited", "United", FEB16,
                                            url="https://secret.example/trip?id=9")))
        self.assertNotIn("secret.example", json.dumps(r))

    def test_the_caption_never_reaches_the_record(self):
        """Image Search captions carry a map link of where you were."""
        [r] = activity.parse_html(page(cell(
            "Searched for", "meme", FEB16,
            caption='<b>Locations:</b><br>&emsp;At <a href="https://maps">home</a>')))
        self.assertNotIn("home", json.dumps(r))
        self.assertNotIn("Locations", json.dumps(r))

    def test_an_entry_without_a_link_still_parses(self):
        [r] = activity.parse_html(page(cell("", "Watched a video that has been removed", FEB16)))
        self.assertIn("removed", r["title"])
        self.assertEqual(r["at"], "2024-02-17T03:50:17Z")

    def test_detail_after_the_timestamp_is_kept_as_extra(self):
        """AI Mode and Shopping append <p> blocks after the time: the
        prompt, or the query again. The timestamp is found where it sits."""
        raw = page(cell("Searched for", "lens", FEB16)).replace(
            f"{FEB16}<br>", f"{FEB16}<br><p><strong>Your prompt:</strong> what is this bird</p>\n<br>")
        [r] = activity.parse_html(raw)
        self.assertEqual(r["at"], "2024-02-17T03:50:17Z")
        self.assertIn("what is this bird", r["extra"])

    def test_detail_that_repeats_the_title_is_dropped(self):
        raw = page(cell("Searched for", "woox", FEB16)).replace(
            f"{FEB16}<br>", f"{FEB16}<br><p>woox</p>\n<br>")
        [r] = activity.parse_html(raw)
        self.assertEqual(r["extra"], "")

    def test_a_labelled_repeat_of_the_title_is_dropped_too(self):
        raw = page(cell("Searched for", "woox", FEB16)).replace(
            f"{FEB16}<br>", f"{FEB16}<br><p><strong>Your prompt:</strong> woox</p>\n<br>")
        [r] = activity.parse_html(raw)
        self.assertEqual(r["extra"], "")

    def test_a_url_title_keeps_host_and_path_only(self):
        """43,524 Search entries on one export were bare URLs, query strings
        and session tokens included. The host and path say where; the
        query says nothing worth keeping and sometimes something private."""
        [r] = activity.parse_html(page(cell(
            "Visited", "https://www.example.com/a/b?token=secret#top", FEB16)))
        self.assertEqual(r["title"], "example.com/a/b")

    def test_entries_come_out_in_file_order(self):
        rs = activity.parse_html(page(cell("Visited", "a", FEB16),
                                      cell("Visited", "b", FEB16)))
        self.assertEqual([r["title"] for r in rs], ["a", "b"])


class TestZones(unittest.TestCase):
    def test_a_known_abbreviation_sets_the_offset(self):
        self.assertEqual(activity.to_utc("Feb 16, 2024, 9:50:17 PM PST"),
                         "2024-02-17T05:50:17Z")
        self.assertEqual(activity.to_utc("Feb 16, 2024, 9:50:17 PM UTC"),
                         "2024-02-16T21:50:17Z")

    def test_an_unknown_abbreviation_falls_back_to_the_configured_zone(self):
        with mock.patch.dict(os.environ, {"RECALL_TZ": "UTC"}):
            self.assertEqual(activity.to_utc("Feb 16, 2024, 9:50:17 PM XYZ"),
                             "2024-02-16T21:50:17Z")


class TestJsonEntries(unittest.TestCase):
    def test_a_json_entry_yields_the_same_record(self):
        raw = json.dumps([{"header": "Search",
                           "title": "Searched for ski resort trail map",
                           "titleUrl": "https://www.google.com/search?q=x",
                           "time": "2024-02-17T03:50:17.123Z",
                           "products": ["Search"],
                           "locationInfos": [{"name": "At home"}]}])
        [r] = activity.parse_json(raw)
        self.assertEqual((r["verb"], r["title"], r["at"]),
                         ("Searched for", "ski resort trail map", "2024-02-17T03:50:17Z"))
        self.assertNotIn("home", json.dumps(r))
        self.assertNotIn("google.com", json.dumps(r))

    def test_a_subtitle_is_the_extra(self):
        raw = json.dumps([{"title": "Watched Zsh prompt",
                           "subtitles": [{"name": "Mr Fugu", "url": "https://c"}],
                           "time": "2024-02-17T03:50:17Z"}])
        [r] = activity.parse_json(raw)
        self.assertEqual((r["verb"], r["title"], r["extra"]),
                         ("Watched", "Zsh prompt", "Mr Fugu"))

    def test_a_title_without_a_known_verb_is_kept_whole(self):
        [r] = activity.parse_json(json.dumps([{"title": "1 notification",
                                               "time": "2024-02-17T03:50:17Z"}]))
        self.assertEqual((r["verb"], r["title"]), ("", "1 notification"))


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.root = pathlib.Path(self.dir)

    def put(self, rel, text="<html></html>"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_it_finds_every_product_file_and_names_the_product(self):
        a = self.put("takeout/My Activity/Search/MyActivity.html")
        b = self.put("takeout/My Activity/Chrome/MyActivity.json", "[]")
        c = self.put("takeout/YouTube and YouTube Music/history/watch-history.html")
        found = activity.Activity().detect(self.root)
        self.assertEqual(sorted(found), sorted([a, b, c]))
        self.assertEqual({activity.product_of(p) for p in found},
                         {"Search", "Chrome", "YouTube watch"})

    def test_an_absent_export_detects_nothing(self):
        self.put("documents/notes.txt", "hi")
        self.assertEqual(activity.Activity().detect(self.root), [])


class TestChunks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.file = pathlib.Path(self.dir) / "My Activity" / "Search" / "MyActivity.html"
        self.file.parent.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"RECALL_TZ": "America/Denver"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def chunks(self, *cells, budget=5000):
        self.file.write_text(page(*cells))
        return list(activity.Activity().chunks(self.file, budget))

    def regular(self, *cells, budget=5000):
        return [c for c in self.chunks(*cells, budget=budget) if ":trends:" not in c.ref]

    def test_one_chunk_per_product_and_month(self):
        cs = self.regular(cell("Searched for", "a", FEB16),
                          cell("Searched for", "b", "Feb 20, 2024, 8:00:00 AM MDT"),
                          cell("Searched for", "c", "Mar 1, 2024, 8:00:00 AM MDT"))
        self.assertEqual([c.ref for c in cs], ["activity:search:2024-02",
                                               "activity:search:2024-03"])
        self.assertEqual(cs[0].occurred_at, "2024-02-01T00:00:00Z")
        self.assertEqual(cs[0].date_confidence, "period")
        self.assertEqual(cs[0].source, "activity")

    def test_lines_carry_the_day_and_the_local_time(self):
        """Takeout stamps every entry with the account's current
        abbreviation, MDT here, even in February when Denver is on MST. The
        stamp is an offset, not a zone: 9:50 PM at UTC-6 is 20:50 MST."""
        [c] = self.regular(cell("Searched for", "ski resort trail map", FEB16))
        self.assertIn("[2024-02, Search activity", c.text)
        self.assertIn("2024-02-16", c.text)
        self.assertIn("20:50 Searched for ski resort trail map", c.text)

    def test_repeats_in_a_day_fold_into_a_count(self):
        [c] = self.regular(cell("Searched for", "same", FEB16),
                           cell("Searched for", "same", FEB16),
                           cell("Visited", "other", FEB16))
        self.assertEqual(c.text.count("Searched for same"), 1)
        self.assertIn("Searched for same (x2)", c.text)

    def test_the_extra_follows_the_title(self):
        [c] = self.regular(cell("Watched", "Zsh prompt", FEB16, extra="Mr Fugu"))
        self.assertIn("Watched Zsh prompt [Mr Fugu]", c.text)

    def test_no_url_in_the_text(self):
        cs = self.chunks(cell("Visited", "United", FEB16, url="https://secret.example/t"))
        self.assertFalse(any("secret.example" in c.text for c in cs))
        self.assertFalse(any("http" in c.text for c in cs))

    def test_a_busy_month_splits_to_budget_with_numbered_parts(self):
        cells = [cell("Searched for", f"query number {i}",
                      f"Feb {i % 28 + 1}, 2024, 9:00:{i % 60:02d} AM MDT")
                 for i in range(300)]
        cs = self.regular(*cells, budget=1500)
        self.assertGreater(len(cs), 1)
        self.assertEqual(cs[0].ref, "activity:search:2024-02#1")
        self.assertTrue(all(len(c.text) <= 1500 for c in cs), [len(c.text) for c in cs])
        self.assertTrue(all("[2024-02, Search activity" in c.text for c in cs))

    def test_a_trends_chunk_per_year_carries_the_source_name(self):
        cs = self.chunks(cell("Searched for", "a", FEB16),
                         cell("Searched for", "b", "Mar 1, 2025, 8:00:00 AM MDT"))
        trends = [c for c in cs if ":trends:" in c.ref]
        self.assertIn("activity:search:trends:2024", [c.ref for c in trends])
        self.assertIn("activity:search:trends:2025", [c.ref for c in trends])
        self.assertTrue(all(c.source == "activity" for c in trends))
        year = next(c for c in trends if c.ref.endswith("2024"))
        self.assertIn("1 events", year.text.replace("1 event.", "1 events"))


class TestRegistration(unittest.TestCase):
    def test_the_adapter_is_registered_before_the_catch_all(self):
        from recall.sources import ADAPTERS
        names = [a.name for a in ADAPTERS]
        self.assertIn("activity", names)
        self.assertLess(names.index("activity"), names.index("documents"))


if __name__ == "__main__":
    unittest.main()
