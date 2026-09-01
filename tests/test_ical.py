import pathlib
import tempfile
import unittest

from recall.sources import ical


def vevent(uid="e1", summary="Dentist", start="20190604T140000Z",
           start_params="", description=None, location=None, status=None,
           rrule=None, recurrence_id=None, attendees=()):
    lines = ["BEGIN:VEVENT", f"UID:{uid}",
             f"DTSTART{start_params}:{start}", f"SUMMARY:{summary}"]
    if description is not None:
        lines.append(f"DESCRIPTION:{description}")
    if location is not None:
        lines.append(f"LOCATION:{location}")
    if status:
        lines.append(f"STATUS:{status}")
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if recurrence_id:
        lines.append(f"RECURRENCE-ID:{recurrence_id}")
    for a in attendees:
        lines.append(a)
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


VTIMEZONE = "\r\n".join([
    "BEGIN:VTIMEZONE", "TZID:America/Denver",
    "BEGIN:DAYLIGHT", "DTSTART:19700308T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "DTSTART:19701101T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU", "END:STANDARD",
    "END:VTIMEZONE"])


def ics(*events, timezone=True):
    body = [VTIMEZONE] if timezone else []
    body.extend(events)
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "\r\n".join(body) + \
        "\r\nEND:VCALENDAR\r\n"


LONG = "This is a real agenda item that carries some detail. " * 5


class TestUnfolding(unittest.TestCase):
    """RFC 5545 folds long lines. Read the file without unfolding and every
    long value truncates at the fold, with no error: 728 real descriptions
    measured as empty that way."""

    def test_a_continuation_line_joins_its_predecessor(self):
        text = "DESCRIPTION:first part\r\n  second part\r\n\tthird"
        self.assertEqual(ical.unfold(text),
                         "DESCRIPTION:first part second partthird")

    def test_a_folded_description_reads_whole(self):
        folded = "DESCRIPTION:" + "\r\n ".join(["word"] * 40)
        ev = vevent(description=None).replace(
            "SUMMARY:Dentist", "SUMMARY:Dentist\r\n" + folded)
        events = ical.parse_events(ics(ev))
        self.assertEqual(len(events[0]["description"]), len("word" * 40))


class TestUnescaping(unittest.TestCase):
    def test_the_four_text_escapes(self):
        self.assertEqual(ical.unescape(r"a\,b\;c\nd\\e"), "a,b;c\nd\\e")

    def test_an_escaped_backslash_before_n_is_not_a_newline(self):
        """A chain of replace() calls turns \\\\n into a newline. One
        left-to-right scan does not."""
        self.assertEqual(ical.unescape(r"path\\n"), "path\\n")


class TestParsing(unittest.TestCase):
    def test_a_vtimezone_block_yields_no_event(self):
        """A VTIMEZONE carries its own DTSTART for the daylight rule, always
        dated 1970. A whole-file scan invents events that never existed."""
        events = ical.parse_events(ics(vevent()))
        self.assertEqual([e["uid"] for e in events], ["e1"])

    def test_a_cancelled_event_is_dropped(self):
        events = ical.parse_events(ics(vevent(status="CANCELLED")))
        self.assertEqual(events, [])

    def test_an_event_with_no_start_is_dropped(self):
        broken = vevent().replace("DTSTART:20190604T140000Z\r\n", "")
        self.assertEqual(ical.parse_events(ics(broken)), [])

    def test_attendees_keep_the_address_and_the_display_name(self):
        ev = vevent(attendees=[
            'ATTENDEE;CN="Ada Lovelace";ROLE=REQ-PARTICIPANT:mailto:ada@example.org',
            "ATTENDEE:mailto:bob@example.org"])
        e = ical.parse_events(ics(ev))[0]
        self.assertEqual(e["attendees"], ["ada@example.org", "bob@example.org"])
        self.assertEqual(e["cn"], {"ada@example.org": "Ada Lovelace"})


class TestEventTime(unittest.TestCase):
    """The export uses three DTSTART forms. Reading only the UTC form
    misplaces the other two by up to seven hours."""

    def time_of(self, **kw):
        return ical.event_time(ical.parse_events(ics(vevent(**kw)))[0])

    def test_utc(self):
        when, all_day = self.time_of(start="20190604T140000Z")
        self.assertEqual(when.isoformat(), "2019-06-04T14:00:00+00:00")
        self.assertFalse(all_day)

    def test_all_day(self):
        when, all_day = self.time_of(start="20190604",
                                     start_params=";VALUE=DATE")
        self.assertEqual(when.isoformat(), "2019-06-04T00:00:00+00:00")
        self.assertTrue(all_day)

    def test_a_named_zone_converts_to_utc(self):
        when, _ = self.time_of(start="20190604T080000",
                               start_params=";TZID=America/Denver")
        self.assertEqual(when.isoformat(), "2019-06-04T14:00:00+00:00")

    def test_an_unknown_zone_does_not_end_the_run(self):
        """UTC keeps the event on the right day, which is what a month
        rollup needs."""
        when, _ = self.time_of(start="20190604T080000",
                               start_params=";TZID=Mars/Olympus_Mons")
        self.assertEqual(when.isoformat(), "2019-06-04T08:00:00+00:00")


class TestRefs(unittest.TestCase):
    def test_a_moved_occurrence_does_not_collide_with_its_parent(self):
        """RECURRENCE-ID marks one edited occurrence and reuses the parent
        UID. Without a suffix the two collide on the UNIQUE ref and one is
        silently lost."""
        parent = vevent(uid="weekly", rrule="FREQ=WEEKLY", description=LONG)
        moved = vevent(uid="weekly", start="20190611T150000Z",
                       recurrence_id="20190611T140000Z", description=LONG)
        refs = [c.ref for c in ical.build(ical.parse_events(ics(parent, moved)),
                                          budget=5000)
                if not c.ref.startswith("calendar:2019-")]
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(set(refs)), 2)


class TestMonthRollup(unittest.TestCase):
    def chunks(self, *events, budget=5000, contacts=None):
        return list(ical.build(ical.parse_events(ics(*events)), budget,
                               contacts))

    def test_one_chunk_per_month(self):
        cs = self.chunks(vevent(uid="a", start="20190604T140000Z"),
                         vevent(uid="b", start="20190620T140000Z"),
                         vevent(uid="c", start="20190702T140000Z"))
        self.assertEqual([c.ref for c in cs],
                         ["calendar:2019-06", "calendar:2019-07"])
        self.assertEqual(cs[0].occurred_at, "2019-06-01T00:00:00Z")
        self.assertEqual(cs[0].date_confidence, "period")
        self.assertIn("2 events", cs[0].text)

    def test_a_recurring_event_lists_once_with_its_rule(self):
        """Expanding a weekly class across three years writes hundreds of
        identical lines and buries every other event."""
        cs = self.chunks(vevent(summary="BJJ", rrule="FREQ=WEEKLY;BYDAY=TU"))
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].text.count("BJJ"), 1)
        self.assertIn("(repeats weekly)", cs[0].text)

    def test_a_location_with_a_newline_stays_on_one_line(self):
        cs = self.chunks(vevent(location=r"Cafe\n12 Main St"))
        lines = cs[0].text.split("\n")
        self.assertTrue(any("at Cafe 12 Main St" in l for l in lines))

    def test_names_come_from_contacts_then_cn_then_the_address(self):
        ev = vevent(attendees=[
            'ATTENDEE;CN="A. Lovelace":mailto:ada@example.org',
            'ATTENDEE;CN="Bob":mailto:bob@example.org',
            "ATTENDEE:mailto:eve@example.org"])
        cs = self.chunks(ev, contacts={"ada@example.org": "Ada"})
        self.assertIn("with Ada, Bob, eve@example.org", cs[0].text)

    def test_a_crowded_meeting_shows_four_names_and_a_count(self):
        many = [f"ATTENDEE:mailto:p{i}@example.org" for i in range(11)]
        cs = self.chunks(vevent(attendees=many))
        self.assertIn("and 7 more", cs[0].text)

    def test_participants_carry_every_raw_address(self):
        """The raw address is the join key for the next contacts update."""
        many = [f"ATTENDEE:mailto:p{i}@example.org" for i in range(11)]
        cs = self.chunks(vevent(attendees=many))
        self.assertEqual(len(cs[0].participants), 11)


class TestEventChunks(unittest.TestCase):
    def chunks(self, *events, budget=5000):
        return [c for c in ical.build(ical.parse_events(ics(*events)), budget)
                if not c.ref.startswith("calendar:20")]

    def test_a_short_description_gets_no_chunk_of_its_own(self):
        """Below the floor a description is a conference link, and it adds
        nothing the month does not carry."""
        self.assertEqual(self.chunks(vevent(description="zoom.us/j/1")), [])

    def test_a_real_description_gets_its_own_chunk(self):
        cs = self.chunks(vevent(uid="agenda", description=LONG))
        self.assertEqual([c.ref for c in cs], ["calendar:agenda"])
        self.assertEqual(cs[0].occurred_at, "2019-06-04T14:00:00Z")
        self.assertEqual(cs[0].date_confidence, "exact")
        self.assertIn("Dentist", cs[0].text)
        self.assertIn("real agenda item", cs[0].text)

    def test_an_all_day_event_is_a_period(self):
        cs = self.chunks(vevent(start="20190604", start_params=";VALUE=DATE",
                                description=LONG))
        self.assertEqual(cs[0].date_confidence, "period")
        self.assertIn("all day", cs[0].text)


class TestBudget(unittest.TestCase):
    """The body packs right up to the budget, so the header is what pushes a
    chunk over. Sweep the sizes rather than trusting one fixture."""

    def test_a_busy_month_never_exceeds_the_budget(self):
        for n in (3, 4, 7, 60, 200):
            with self.subTest(title_chars=n):
                events = [vevent(uid=f"e{i}", summary="x" * n,
                                 start=f"201906{1 + i % 28:02d}T140000Z")
                          for i in range(400)]
                cs = list(ical.build(ical.parse_events(ics(*events)), 2000))
                self.assertGreater(len(cs), 1)
                self.assertEqual(len({c.ref for c in cs}), len(cs))
                for c in cs:
                    self.assertLessEqual(len(c.text), 2000)

    def test_a_long_description_splits_with_unique_refs(self):
        cs = [c for c in ical.build(
            ical.parse_events(ics(vevent(uid="long", description=LONG * 40))),
            2000) if c.ref.startswith("calendar:long")]
        self.assertGreater(len(cs), 1)
        self.assertEqual(len({c.ref for c in cs}), len(cs))
        for c in cs:
            self.assertLessEqual(len(c.text), 2000)


class TestAdapter(unittest.TestCase):
    def test_an_ics_file_is_found_and_other_files_are_not(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "Personal.ics").write_text(ics(vevent()))
            (root / "notes.txt").write_text("x")
            found = ical.ICal().detect(root)
            self.assertEqual([p.name for p in found], ["Personal.ics"])

    def test_samples_are_the_longest_descriptions(self):
        """Budgets calibrate from the worst text a source produces."""
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "cal.ics"
            path.write_text(ics(vevent(uid="a", description=LONG),
                                vevent(uid="b", description="short")))
            samples = ical.ICal().samples(path)
            self.assertTrue(samples)
            self.assertIn("real agenda item", samples[0])

    def test_it_is_registered(self):
        from recall.sources import ADAPTERS
        self.assertIn("calendar", {a.name for a in ADAPTERS})

    def test_chunks_read_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "cal.ics"
            path.write_text(ics(vevent()))
            cs = list(ical.ICal().chunks(path, 5000))
            self.assertEqual([c.ref for c in cs], ["calendar:2019-06"])
            self.assertEqual(cs[0].source, "calendar")
            self.assertEqual(cs[0].path, str(path))
