import io
import json
import pathlib
import sqlite3
import struct
import tempfile
import unittest
import zipfile

from recall.sources import base, files, health, imessage, mbox, twitter


class TestMboxSeparator(unittest.TestCase):
    """A new message starts at a line beginning "From " followed by an
    address and a date. "From now on..." at the start of a body line is
    ordinary prose, and splitting on it tears a message in half."""

    def test_it_splits_real_messages(self):
        raw = (b"From 1@x Sun Aug 16 23:54:20 +0000 2026\nSubject: a\n\nbody\n"
               b"From 2@x Mon Aug 17 10:00:00 +0000 2026\nSubject: b\n\nbody\n")
        self.assertEqual(len(list(mbox.raw_messages(io.BytesIO(raw)))), 2)

    def test_prose_beginning_with_from_does_not_split(self):
        raw = (b"From 1@x Sun Aug 16 23:54:20 +0000 2026\nSubject: a\n\n"
               b"From now on I will write daily.\n")
        self.assertEqual(len(list(mbox.raw_messages(io.BytesIO(raw)))), 1)


class TestMboxFilter(unittest.TestCase):
    """An allowlist, not a denylist. A receipt carries BOTH Category
    Purchases and Category Updates, so "skip anything tagged Updates" throws
    away real receipts."""

    def keep(self, labels):
        return bool({x.strip() for x in labels.split(",")} & mbox.KEEP_LABELS)

    def test_personal_mail_is_kept(self):
        self.assertTrue(self.keep("Category Personal,Inbox"))

    def test_a_pure_promotion_is_dropped(self):
        self.assertFalse(self.keep("Category Promotions,Inbox,Opened"))

    def test_a_receipt_survives_being_tagged_updates_too(self):
        self.assertTrue(self.keep("Category Purchases,Category Updates,Inbox"))

    def test_the_keep_set_never_contains_the_noise_labels(self):
        self.assertNotIn("Category Updates", mbox.KEEP_LABELS)
        self.assertNotIn("Inbox", mbox.KEEP_LABELS)


class TestMboxQuoting(unittest.TestCase):
    def test_a_quoted_reply_is_removed(self):
        got = mbox.strip_quoted(
            "Yes.\n\nOn Sun, Aug 16 Ann wrote:\n> original question")
        self.assertIn("Yes.", got)
        self.assertNotIn("original question", got)

    def test_angle_quoted_lines_go(self):
        self.assertNotIn(">", mbox.strip_quoted("new\n> old"))

    def test_unquoted_text_survives_intact(self):
        self.assertEqual(mbox.strip_quoted("Just a note."), "Just a note.")


def streamtyped(body):
    """Minimal Apple archive carrying one string, for the decoder test."""
    payload = body.encode()
    return (b"\x04\x0bstreamtyped\x81\xe8\x03NSString\x01\x94\x84\x01+"
            + bytes([len(payload)]) + payload)


class TestAttributedBody(unittest.TestCase):
    """On modern macOS `message.text` is NULL for 99.7 percent of rows. An
    adapter that reads it captures a fraction of a percent of the corpus and
    reports no error at all."""

    def test_it_decodes_the_body(self):
        self.assertEqual(
            imessage.decode_attributed_body(streamtyped("hello there")),
            "hello there")

    def test_an_empty_blob_returns_empty(self):
        self.assertEqual(imessage.decode_attributed_body(b""), "")
        self.assertEqual(imessage.decode_attributed_body(None), "")

    def test_a_blob_without_nsstring_returns_empty(self):
        self.assertEqual(imessage.decode_attributed_body(b"nonsense"), "")

    def test_a_two_byte_length_is_read(self):
        body = "y" * 400
        blob = (b"\x04\x0bstreamtyped\x81\xe8\x03NSString\x01\x94\x84\x01+"
                + b"\x81" + struct.pack("<H", len(body)) + body.encode())
        self.assertEqual(imessage.decode_attributed_body(blob), body)


class TestAppleTimestamps(unittest.TestCase):
    """Apple stores seconds or nanoseconds since 2001 depending on version.
    Reading nanoseconds as seconds dates a message to the year 4000."""

    def test_seconds_convert(self):
        self.assertEqual(imessage._unix(0), imessage.APPLE_EPOCH)

    def test_nanoseconds_convert(self):
        self.assertEqual(imessage._unix(1_000_000_000_000),
                         1000 + imessage.APPLE_EPOCH)


class TestFileExclusions(unittest.TestCase):
    """A filter anchored to a rooted path fails OPEN once the tree moves: it
    stops matching while the include rules keep matching. Match on segments."""

    def p(self, s):
        return pathlib.Path(s)

    def test_a_normal_document_is_kept(self):
        self.assertTrue(files.keep(self.p("notes/trip.md")))

    def test_a_vendored_directory_is_skipped_at_any_depth(self):
        self.assertFalse(files.keep(self.p("a/b/node_modules/c/index.js")))
        self.assertFalse(files.keep(self.p("node_modules/x.js")))

    def test_a_cache_directory_is_skipped(self):
        self.assertFalse(files.keep(self.p("deep/Caches/thumb.png")))

    def test_a_pirated_ebook_hint_is_skipped(self):
        self.assertFalse(files.keep(self.p("books/z-library-thing.pdf")))

    def test_clinical_records_are_skipped_inside_a_documents_folder(self):
        """The Apple Health export carries FHIR medical records as .json,
        which the catch-all reads. Indexing medical data must be a deliberate
        choice, never a side effect of where a folder was dropped."""
        self.assertFalse(files.keep(
            self.p("apple_health_export/clinical-records/Condition-1.json")))

    def test_a_binary_disguised_as_text_is_not_read(self):
        """A Photoshop file named .pdf becomes megabytes of noise if you
        trust the extension."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"\x00\x01\x02binary")
            name = f.name
        self.assertEqual(files.read_text(pathlib.Path(name)), "")


class TestChunkContract(unittest.TestCase):
    def test_a_chunk_carries_everything_the_loader_needs(self):
        from recall.sources.base import Chunk, REQUIRED_KEYS
        c = Chunk(ref="r", text="t", source="s").as_dict()
        self.assertTrue(REQUIRED_KEYS <= set(c), REQUIRED_KEYS - set(c))

    def test_date_confidence_defaults_to_low_not_exact(self):
        """A guessed date and a real one must never look alike downstream."""
        from recall.sources.base import Chunk
        self.assertEqual(Chunk(ref="r", text="t", source="s").date_confidence,
                         "low")


def tweet(text, created_at="Wed Jun 20 12:00:00 +0000 2018", mentions=()):
    return {"tweet": {"full_text": text, "created_at": created_at,
                      "entities": {"user_mentions": [
                          {"id_str": i, "screen_name": s} for i, s in mentions]}}}


def dm(mid, sender, text, at="2018-06-20T12:00:00.000Z"):
    return {"messageCreate": {"id": mid, "senderId": sender, "text": text,
                              "createdAt": at}}


def conversation(cid, messages):
    return {"dmConversation": {"conversationId": cid, "messages": messages}}


def twitter_export(root, tweets, dms=(), me="111", account=True):
    """Write a minimal Twitter/X export tree. Returns the data directory."""
    data = pathlib.Path(root) / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "tweets.js").write_text(
        "window.YTD.tweets.part0 = " + json.dumps(tweets), encoding="utf-8")
    if account:
        (data / "account.js").write_text(
            "window.YTD.account.part0 = "
            + json.dumps([{"account": {"accountId": me}}]), encoding="utf-8")
    if dms:
        (data / "direct-messages.js").write_text(
            "window.YTD.direct_messages.part0 = " + json.dumps(list(dms)),
            encoding="utf-8")
    return data


class TestTwitterExportParsing(unittest.TestCase):
    """Twitter wraps valid JSON in a JavaScript assignment, so the file is not
    JSON. Split on the FIRST "=" only. A tweet containing "x = y" is truncated
    by a split on any later one."""

    def parse(self, raw):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(raw)
        return twitter.load_js(f.name)

    def test_it_reads_the_json_after_the_assignment(self):
        self.assertEqual(self.parse('window.YTD.tweets.part0 = [{"a": 1}]'),
                         [{"a": 1}])

    def test_an_equals_sign_inside_the_content_survives(self):
        got = self.parse('window.YTD.tweets.part0 = [{"a": "x = y"}]')
        self.assertEqual(got[0]["a"], "x = y")


class TestTweetGrouping(unittest.TestCase):
    """The median tweet is 64 characters. One chunk per tweet embeds to noise,
    for the same reason one chunk per message does. Tweets group by day."""

    def chunks(self, tweets, budget=5000):
        with tempfile.TemporaryDirectory() as d:
            return list(twitter.Twitter().chunks(
                twitter_export(d, tweets), budget))

    def test_two_tweets_on_one_day_make_one_chunk(self):
        cs = self.chunks([tweet("first"), tweet("second")])
        self.assertEqual(len(cs), 1)
        self.assertIn("first", cs[0].text)
        self.assertIn("second", cs[0].text)

    def test_separate_days_make_separate_chunks(self):
        cs = self.chunks([tweet("a"),
                          tweet("b", "Thu Jun 21 09:00:00 +0000 2018")])
        self.assertEqual(len(cs), 2)

    def test_a_retweet_keeps_its_prefix(self):
        """"RT @someone:" is the only marker that separates another person's
        words from the user's own."""
        self.assertIn("RT @ann:", self.chunks([tweet("RT @ann: her words")])[0].text)

    def test_the_ref_is_the_day(self):
        self.assertEqual(self.chunks([tweet("a")])[0].ref, "tweet:2018-06-20")

    def test_the_date_is_a_period_not_an_exact_time(self):
        """The chunk covers a whole day. Calling it exact lets a date filter
        trust a midnight timestamp nobody tweeted at."""
        self.assertEqual(self.chunks([tweet("a")])[0].date_confidence, "period")

    def test_a_busy_day_splits_and_numbers_its_parts(self):
        cs = self.chunks([tweet("x" * 400) for _ in range(20)])
        self.assertGreater(len(cs), 1)
        self.assertEqual([c.ref for c in cs[:2]],
                         ["tweet:2018-06-20#1", "tweet:2018-06-20#2"])


class TestTwitterHandles(unittest.TestCase):
    """The export names DM senders by numeric account id and ships no name
    table. user_mentions inside your own tweets pair an id with a screen name,
    which recovers the busiest conversations."""

    def test_a_mentioned_id_resolves_to_its_screen_name(self):
        m = twitter.handle_map([tweet("hi @ann", mentions=[("42", "ann")])])
        self.assertEqual(m["42"], "ann")

    def test_the_most_recent_screen_name_wins(self):
        """A renamed account should read as the handle you would recognise."""
        m = twitter.handle_map([
            tweet("old", "Wed Jun 20 12:00:00 +0000 2018", [("42", "ann_old")]),
            tweet("new", "Thu Jun 21 12:00:00 +0000 2018", [("42", "ann_new")])])
        self.assertEqual(m["42"], "ann_new")

    def test_file_order_does_not_change_the_result(self):
        m = twitter.handle_map([
            tweet("new", "Thu Jun 21 12:00:00 +0000 2018", [("42", "ann_new")]),
            tweet("old", "Wed Jun 20 12:00:00 +0000 2018", [("42", "ann_old")])])
        self.assertEqual(m["42"], "ann_new")


class TestDirectMessages(unittest.TestCase):
    def chunks(self, dms, tweets=(), me="111", budget=5000):
        with tempfile.TemporaryDirectory() as d:
            path = twitter_export(d, list(tweets), dms, me)
            return [c for c in twitter.Twitter().chunks(path, budget)
                    if c.ref.startswith("dm:")]

    def test_an_unresolved_sender_keeps_its_numeric_id(self):
        """Only about a third of senders resolve. One shared "unknown" bucket
        merges separate people into a single apparent speaker."""
        cs = self.chunks([conversation("c1", [dm("1", "999", "hello")])])
        self.assertIn("999", cs[0].text)
        self.assertNotIn("unknown", cs[0].text.lower())

    def test_a_resolved_sender_reads_as_a_handle(self):
        cs = self.chunks([conversation("c1", [dm("1", "42", "hello")])],
                         tweets=[tweet("hi @ann", mentions=[("42", "ann")])])
        self.assertIn("ann: hello", cs[0].text)

    def test_your_own_messages_read_as_me(self):
        cs = self.chunks([conversation("c1", [dm("1", "111", "mine")])])
        self.assertIn("me: mine", cs[0].text)

    def test_two_messages_two_hours_apart_stay_in_one_chunk(self):
        """The session gap belongs to the medium. Twitter DMs are
        asynchronous. At the 30 minutes iMessage uses, 39 percent of sessions
        came out as one short message."""
        cs = self.chunks([conversation("c1", [
            dm("1", "999", "morning", "2018-06-20T09:00:00.000Z"),
            dm("2", "999", "afternoon", "2018-06-20T11:00:00.000Z")])])
        self.assertEqual(len(cs), 1)

    def test_a_two_day_gap_starts_a_new_chunk(self):
        cs = self.chunks([conversation("c1", [
            dm("1", "999", "monday", "2018-06-18T09:00:00.000Z"),
            dm("2", "999", "wednesday", "2018-06-20T09:00:00.000Z")])])
        self.assertEqual(len(cs), 2)

    def test_a_membership_event_carrying_no_text_is_skipped(self):
        """joinConversation and participantsLeave entries hold no text at
        all. Reading one as a message raises KeyError."""
        cs = self.chunks([conversation("c1", [
            {"joinConversation": {"initiatingUserId": "999"}},
            dm("1", "999", "real message")])])
        self.assertEqual(len(cs), 1)
        self.assertIn("real message", cs[0].text)

    def test_the_raw_sender_id_stays_in_participants(self):
        """The handle goes in the text. The id is the join key that finds
        these chunks again when more handles resolve."""
        cs = self.chunks([conversation("c1", [dm("1", "42", "hi")])],
                         tweets=[tweet("hi @ann", mentions=[("42", "ann")])])
        self.assertEqual(cs[0].participants, ["42"])

    def test_the_date_is_exact(self):
        cs = self.chunks([conversation("c1", [dm("1", "999", "hi")])])
        self.assertEqual(cs[0].date_confidence, "exact")


class TestTwitterDetection(unittest.TestCase):
    def test_it_finds_the_directory_holding_tweets(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(d, [tweet("a")])
            self.assertEqual(twitter.Twitter().detect(pathlib.Path(d)), [data])

    def test_an_absent_export_detects_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(twitter.Twitter().detect(pathlib.Path(d)), [])

    def test_a_missing_account_file_does_not_stop_the_ingest(self):
        """Without account.js nothing is marked as yours. That is a worse
        chunk, not a failed run."""
        with tempfile.TemporaryDirectory() as d:
            path = twitter_export(d, [tweet("a")],
                                  [conversation("c1", [dm("1", "999", "hi")])],
                                  account=False)
            self.assertEqual(len(list(twitter.Twitter().chunks(path, 5000))), 2)


HEALTH_XML = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<HealthData locale="en_US">{body}</HealthData>\n')

RECORD = ('<Record type="HKQuantityTypeIdentifierHeartRate" value="72" '
          'startDate="2021-02-14 08:00:01 -0500"/>')


def workout_xml(kind="TraditionalStrengthTraining",
                start="2021-02-14 08:00:00 -0500", duration="45.5",
                stats=(("ActiveEnergyBurned", "320"),)):
    inner = "".join(
        f'<WorkoutStatistics type="HKQuantityTypeIdentifier{k}" sum="{v}"/>'
        for k, v in stats)
    return (f'<Workout workoutActivityType="HKWorkoutActivityType{kind}" '
            f'duration="{duration}" durationUnit="min" sourceName="Watch" '
            f'startDate="{start}">{inner}</Workout>')


def wo(start, kind="Brazilian Jiu Jitsu", duration=60.0, stats=None):
    return {"type": kind, "duration": duration, "unit": "min", "start": start,
            "source_name": "Watch", "stats": stats or {}}


class TestActivityName(unittest.TestCase):
    """"HKWorkoutActivityTypeTraditionalStrengthTraining" matches no question
    anyone asks. Splitting the camel case gives the embedding real words."""

    def test_the_apple_prefix_goes(self):
        self.assertNotIn(
            "HKWorkout", health.activity_name("HKWorkoutActivityTypeRunning"))

    def test_camel_case_becomes_separate_words(self):
        self.assertEqual(
            health.activity_name(
                "HKWorkoutActivityTypeTraditionalStrengthTraining"),
            "Traditional Strength Training")


class TestWorkoutStreaming(unittest.TestCase):
    """The export holds millions of sample records against a few thousand
    workouts. iterparse must clear the samples to keep memory flat, but never
    WorkoutStatistics: a child's end event fires BEFORE its parent, so
    clearing there empties every workout of its distance and calories."""

    def parse(self, body):
        return list(health.workouts(
            io.BytesIO(HEALTH_XML.format(body=body).encode())))

    def test_a_workout_is_read(self):
        w = self.parse(workout_xml())[0]
        self.assertEqual(w["type"], "Traditional Strength Training")
        self.assertAlmostEqual(w["duration"], 45.5)

    def test_workout_statistics_survive_the_streaming_clear(self):
        w = self.parse(RECORD * 3 + workout_xml() + RECORD * 3)[0]
        self.assertAlmostEqual(w["stats"]["ActiveEnergyBurned"], 320.0)

    def test_sample_records_are_not_returned(self):
        """Heart rate readings are telemetry. They belong in a table, not in
        a retrieval corpus."""
        self.assertEqual(len(self.parse(RECORD * 50 + workout_xml())), 1)


class TestWorkoutRollup(unittest.TestCase):
    """One workout is about 120 characters and embeds to noise. A bare monthly
    total cannot answer "when did I start jiu jitsu". A monthly chunk that
    lists its sessions answers both."""

    def chunks(self, items, budget=5000):
        return list(health.month_chunks(items, budget))

    def test_workouts_group_into_one_chunk_per_month(self):
        cs = self.chunks([wo("2021-02-14 08:00:00 -0500"),
                          wo("2021-02-20 08:00:00 -0500"),
                          wo("2021-03-01 08:00:00 -0500")])
        self.assertEqual([c.ref for c in cs],
                         ["health:2021-02", "health:2021-03"])

    def test_the_chunk_lists_each_session_with_its_day(self):
        cs = self.chunks([wo("2021-02-14 08:00:00 -0500"),
                          wo("2021-02-20 08:00:00 -0500")])
        self.assertIn("2021-02-14", cs[0].text)
        self.assertIn("2021-02-20", cs[0].text)

    def test_the_chunk_summarises_the_month(self):
        cs = self.chunks([wo("2021-02-14 08:00:00 -0500"),
                          wo("2021-02-20 08:00:00 -0500")])
        self.assertIn("2 workouts", cs[0].text)
        self.assertIn("Brazilian Jiu Jitsu", cs[0].text)

    def test_distance_and_calories_reach_the_text(self):
        cs = self.chunks([wo("2021-02-14 08:00:00 -0500", "Running",
                             stats={"DistanceWalkingRunning": 3.1,
                                    "ActiveEnergyBurned": 320.0})])
        self.assertIn("3.10 mi", cs[0].text)
        self.assertIn("320 Cal", cs[0].text)

    def test_a_reading_that_rounds_to_zero_is_left_out(self):
        """A sub-hundredth-mile distance renders as "0.00 mi", which is noise
        in the text and in the embedding. Real data carries these."""
        cs = self.chunks([wo("2021-02-14 08:00:00 -0500", "Other",
                             stats={"DistanceWalkingRunning": 0.001,
                                    "ActiveEnergyBurned": 0.4})])
        self.assertNotIn("0.00 mi", cs[0].text)
        self.assertNotIn("0 Cal", cs[0].text)

    def test_a_month_that_fits_keeps_a_bare_ref(self):
        self.assertEqual(self.chunks([wo("2021-02-14 08:00:00 -0500")])[0].ref,
                         "health:2021-02")

    def test_a_busy_month_splits_and_numbers_its_parts(self):
        items = [wo(f"2021-02-{d:02d} 08:00:00 -0500")
                 for d in range(1, 29)] * 20
        cs = self.chunks(items, budget=600)
        self.assertGreater(len(cs), 1)
        self.assertEqual([c.ref for c in cs[:2]],
                         ["health:2021-02#1", "health:2021-02#2"])

    def test_a_split_part_says_which_part_it_is(self):
        """Part 2 counts its own sessions. Without the label that count reads
        as the month total, which is wrong."""
        items = [wo(f"2021-02-{d:02d} 08:00:00 -0500")
                 for d in range(1, 29)] * 20
        self.assertIn("part 2", self.chunks(items, budget=600)[1].text)

    def test_the_date_is_a_period(self):
        cs = self.chunks([wo("2021-02-14 08:00:00 -0500")])
        self.assertEqual(cs[0].date_confidence, "period")
        self.assertEqual(cs[0].occurred_at, "2021-02-01T00:00:00Z")


class TestHealthDetection(unittest.TestCase):
    """The phone hands you export.zip. Making someone unzip 674 MB first is a
    step that buys nothing."""

    def tree(self, d, xml=True, zipped=False, clinical=False):
        root = pathlib.Path(d) / "apple_health_export"
        root.mkdir(parents=True)
        body = HEALTH_XML.format(body=workout_xml())
        if xml:
            (root / "export.xml").write_text(body)
        if clinical:
            (root / "clinical-records").mkdir()
            (root / "clinical-records" / "Condition-1.json").write_text("{}")
        if zipped:
            with zipfile.ZipFile(pathlib.Path(d) / "export.zip", "w") as z:
                z.writestr("apple_health_export/export.xml", body)
        return pathlib.Path(d)

    def test_a_loose_export_xml_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            found = health.Health().detect(self.tree(d))
            self.assertEqual([p.name for p in found], ["export.xml"])

    def test_the_export_zip_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            found = health.Health().detect(self.tree(d, xml=False, zipped=True))
            self.assertEqual([p.name for p in found], ["export.zip"])

    def test_an_unrelated_zip_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as d:
            with zipfile.ZipFile(pathlib.Path(d) / "photos.zip", "w") as z:
                z.writestr("a.jpg", "x")
            self.assertEqual(health.Health().detect(pathlib.Path(d)), [])

    def test_the_zip_is_read_without_unpacking_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.tree(d, xml=False, zipped=True)
            path = health.Health().detect(root)[0]
            cs = list(health.Health().chunks(path, 5000))
            self.assertEqual(len(cs), 1)
            self.assertIn("Traditional Strength Training", cs[0].text)

    def test_clinical_records_are_not_detected(self):
        """The export also carries FHIR medical records in their own folder.
        This adapter opens export.xml and nothing else, so medical data never
        reaches the corpus without a deliberate choice."""
        with tempfile.TemporaryDirectory() as d:
            found = health.Health().detect(self.tree(d, clinical=True))
            self.assertEqual([p.name for p in found], ["export.xml"])


class TestNewAdaptersAreRegistered(unittest.TestCase):
    """An adapter nobody registers is dead code. detect_all is the only path
    the pipeline uses."""

    def test_every_adapter_is_in_the_registry(self):
        from recall.sources import ADAPTERS
        names = {a.name for a in ADAPTERS}
        self.assertIn("twitter", names)
        self.assertIn("health", names)

    def test_the_catch_all_runs_last(self):
        from recall.sources import ADAPTERS
        self.assertEqual(ADAPTERS[-1].name, "documents")


class TestChunksStayInsideTheBudget(unittest.TestCase):
    """A session window bounds turns, not characters, and a rollup bounds a
    period, not characters. Twenty long turns build one chunk the embedding
    server refuses, and embed_safe bisects a single oversized item down to a
    silent drop. Bound the size where the chunk is built."""

    BUDGET = 5000

    def test_a_long_dm_thread_splits_instead_of_overflowing(self):
        turns = [dm(str(i), "999", "x" * 2000,
                    f"2018-06-20T09:{i:02d}:00.000Z") for i in range(20)]
        with tempfile.TemporaryDirectory() as d:
            path = twitter_export(d, [tweet("a")], [conversation("c1", turns)])
            cs = [c for c in twitter.Twitter().chunks(path, self.BUDGET)
                  if c.ref.startswith("dm:")]
        self.assertGreater(len(cs), 1)
        self.assertEqual(len({c.ref for c in cs}), len(cs))
        for c in cs:
            self.assertLessEqual(len(c.text), self.BUDGET)

    def test_a_busy_tweet_day_never_exceeds_the_budget(self):
        """The body packs right up to the budget, so the header is what
        pushes a chunk over. Only some tweet lengths land the body close
        enough to the edge for the header to matter, so sweep them."""
        for n in (3, 4, 7, 60, 200):
            with self.subTest(tweet_chars=n):
                with tempfile.TemporaryDirectory() as d:
                    path = twitter_export(
                        d, [tweet("x" * n) for _ in range(2000)])
                    cs = list(twitter.Twitter().chunks(path, self.BUDGET))
                    self.assertGreater(len(cs), 1)
                    for c in cs:
                        self.assertLessEqual(len(c.text), self.BUDGET)

    def test_a_busy_workout_month_never_exceeds_the_budget(self):
        items = [wo(f"2021-02-{d:02d} 08:00:00 -0500",
                    kind=f"Kind {d}") for d in range(1, 29)] * 20
        for c in health.month_chunks(items, self.BUDGET):
            self.assertLessEqual(len(c.text), self.BUDGET)

    def test_a_long_message_thread_splits_instead_of_overflowing(self):
        rows = [{"rowid": i, "thread": "t", "handle": "+15551234567",
                 "at": 1_600_000_000 + i * 60, "mine": False,
                 "text": "x" * 2000} for i in range(20)]
        cs = list(imessage.IMessage()._windows(rows, {}, {}, self.BUDGET))
        self.assertGreater(len(cs), 1)
        self.assertEqual(len({c.ref for c in cs}), len(cs))
        for c in cs:
            self.assertLessEqual(len(c.text), self.BUDGET)

    def test_a_short_thread_keeps_a_bare_ref(self):
        """A part suffix on an unsplit chunk would change every stable ref
        already stored."""
        rows = [{"rowid": 7, "thread": "t", "handle": "+1",
                 "at": 1_600_000_000, "mine": False, "text": "hi"}]
        cs = list(imessage.IMessage()._windows(rows, {}, {}, self.BUDGET))
        self.assertEqual([c.ref for c in cs], ["message:7"])


class TestSymlinkedExports(unittest.TestCase):
    """People symlink a large export into the data directory rather than copy
    tens of gigabytes. pathlib.rglob never enters a symlinked directory, so
    every adapter found nothing and doctor said "no recognised sources"."""

    def test_walk_enters_a_symlinked_directory(self):
        with tempfile.TemporaryDirectory() as d:
            real = pathlib.Path(d) / "elsewhere"
            real.mkdir()
            (real / "tweets.js").write_text("x")
            data = pathlib.Path(d) / "data"
            data.mkdir()
            (data / "twitter").symlink_to(real)
            self.assertEqual([p.name for p in base.walk(data)], ["tweets.js"])

    def test_walk_ends_on_a_symlink_loop(self):
        """followlinks=True recurses forever through a link back to a parent."""
        import itertools
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "a").mkdir()
            (root / "a" / "x.txt").write_text("x")
            (root / "a" / "loop").symlink_to(root)
            got = list(itertools.islice(base.walk(root), 200))
            self.assertLess(len(got), 200)
            self.assertIn("x.txt", [p.name for p in got])

    def test_a_symlinked_twitter_export_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            real = twitter_export(pathlib.Path(d) / "elsewhere", [tweet("a")])
            data = pathlib.Path(d) / "data"
            data.mkdir()
            (data / "twitter").symlink_to(real)
            self.assertTrue(twitter.Twitter().detect(data))

    def test_a_symlinked_health_export_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            real = pathlib.Path(d) / "elsewhere"
            real.mkdir()
            (real / "export.xml").write_text(
                HEALTH_XML.format(body=workout_xml()))
            data = pathlib.Path(d) / "data"
            data.mkdir()
            (data / "health").symlink_to(real)
            self.assertEqual([p.name for p in health.Health().detect(data)],
                             ["export.xml"])
