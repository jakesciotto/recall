import io
import json
import pathlib
import sqlite3
import struct
import tempfile
import unittest
import zipfile

from recall.sources import base, files, health, imessage, mbox, twitter


def regular(chunks):
    """Every source now yields a yearly trends chunk on top of its regular
    ones. Tests that pin the regular chunks exclude those here."""
    return [c for c in chunks if ":trends:" not in c.ref]


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


def mime(parts):
    """A multipart message. parts = [(content_type, payload_bytes, headers)]."""
    from email.message import EmailMessage
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email import encoders
    msg = MIMEMultipart("mixed")
    msg["From"] = "a@example.org"
    msg["Subject"] = "s"
    for ctype, payload, headers in parts:
        main, sub = ctype.split("/")
        part = MIMEBase(main, sub)
        part.set_payload(payload)
        encoders.encode_base64(part)
        for k, v in headers.items():
            part[k] = v
        msg.attach(part)
    return msg.as_bytes()


BINARY = bytes(range(256)) * 8


class TestMboxTakesTextPartsOnly(unittest.TestCase):
    """A quarter of one real mailbox's chunks carried NUL bytes. Not
    corrupted headers: inline images and undeclared attachments were being
    decoded as text, because everything that was not text/plain went into
    the HTML bucket. Three ingests died on the same DataError before the
    cause was in view."""

    def test_an_inline_image_never_reaches_the_body(self):
        raw = mime([("text/plain", b"hello", {}),
                    ("image/png", BINARY, {"Content-Disposition": "inline"})])
        self.assertEqual(mbox.body_text(raw), "hello")

    def test_a_binary_part_with_no_disposition_is_skipped(self):
        raw = mime([("application/pdf", BINARY, {})])
        self.assertEqual(mbox.body_text(raw), "")

    def test_html_is_still_the_fallback(self):
        raw = mime([("text/html", b"<p>hi <b>there</b></p>", {})])
        self.assertIn("hi", mbox.body_text(raw))
        self.assertNotIn("<b>", mbox.body_text(raw))

    def test_html_plus_inline_image_yields_the_html_only(self):
        """The real shape: no plain part, so the HTML bucket was joined and
        the image bytes came with it."""
        raw = mime([("text/html", b"<p>hi there</p>", {}),
                    ("image/jpeg", BINARY, {"Content-Disposition": "inline"})])
        body = mbox.body_text(raw)
        self.assertIn("hi there", body)
        self.assertNotIn("\x00", body)
        self.assertLess(len(body), 40)

    def test_a_declared_attachment_is_still_skipped(self):
        raw = mime([("text/plain", b"body", {}),
                    ("text/plain", b"log line", {"Content-Disposition": "attachment; filename=x.log"})])
        self.assertEqual(mbox.body_text(raw), "body")


def mbox_file(d, messages):
    """An mbox with one message per (thread_id, body) pair, no labels, so
    every message is kept."""
    lines = []
    for i, (tid, body) in enumerate(messages):
        lines.append(f"From a@example.org Mon Jun  4 12:00:00 2018\n")
        lines.append(f"From: a@example.org\nSubject: s\n"
                     f"Date: Mon, 4 Jun 2018 12:00:{i:02d} +0000\n"
                     f"Message-ID: <m{i}@example.org>\nX-GM-THRID: {tid}\n"
                     f"Content-Type: text/plain\n\n{body}\n\n")
    path = pathlib.Path(d) / "mail.mbox"
    path.write_text("".join(lines))
    return path


class TestMboxRefsAreUnique(unittest.TestCase):
    """One real mailbox lost 1,387 rows to 729 colliding refs. A thread of
    one message longer than the budget splits into parts, and the part
    suffix was gated on the thread having more messages than the part,
    which a split single message never satisfies."""

    def test_one_long_message_splits_with_distinct_refs(self):
        with tempfile.TemporaryDirectory() as d:
            path = mbox_file(d, [("111", "word " * 2000)])
            cs = list(mbox.Mbox().chunks(path, 3000))
        self.assertGreater(len(cs), 1)
        self.assertEqual(len({c.ref for c in cs}), len(cs))
        for c in cs:
            self.assertLessEqual(len(c.text), 3000)

    def test_a_short_thread_keeps_a_bare_ref(self):
        """A part suffix on an unsplit chunk would change every stable ref
        already stored."""
        with tempfile.TemporaryDirectory() as d:
            path = mbox_file(d, [("222", "short")])
            cs = regular(mbox.Mbox().chunks(path, 3000))
        self.assertEqual([c.ref for c in cs], ["email:222"])

    def test_no_chunk_exceeds_the_budget_across_message_sizes(self):
        """The body packs right up to the budget, so the header is what
        pushes a chunk over. Only some lengths land close enough to the
        edge for it to matter, so sweep them."""
        for n in (3, 4, 7, 60, 200, 700):
            with self.subTest(word_chars=n):
                with tempfile.TemporaryDirectory() as d:
                    path = mbox_file(d, [("444", ("x" * n + " ") * 400)] * 3)
                    cs = list(mbox.Mbox().chunks(path, 2000))
                self.assertGreater(len(cs), 1)
                self.assertEqual(len({c.ref for c in cs}), len(cs))
                for c in cs:
                    self.assertLessEqual(len(c.text), 2000)

    def test_a_long_subject_cannot_starve_the_body(self):
        with tempfile.TemporaryDirectory() as d:
            path = mbox_file(d, [("555", "body")])
            raw = path.read_text().replace("Subject: s", "Subject: " + "S" * 5000)
            path.write_text(raw)
            cs = regular(mbox.Mbox().chunks(path, 2000))
        self.assertEqual(len(cs), 1)
        self.assertLessEqual(len(cs[0].text), 2000)
        self.assertIn("body", cs[0].text)

    def test_a_long_thread_of_short_messages_still_suffixes(self):
        with tempfile.TemporaryDirectory() as d:
            path = mbox_file(d, [("333", "line " * 100)] * 12)
            cs = list(mbox.Mbox().chunks(path, 1500))
        self.assertGreater(len(cs), 1)
        self.assertEqual(len({c.ref for c in cs}), len(cs))


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
            return regular(twitter.Twitter().chunks(
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
            self.assertEqual(len(regular(twitter.Twitter().chunks(path, 5000))), 2)


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


class TestPdfWithoutPdftotext(unittest.TestCase):
    """read_text returns nothing for a PDF when the binary is missing, and
    that is the right call per file. But it must say so once, or a corpus
    of PDFs indexes as nothing with a clean-looking summary."""

    def setUp(self):
        files._warned_pdftotext = False

    def test_it_warns_once_and_yields_nothing(self):
        import io
        import contextlib
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            pdf = pathlib.Path(d) / "a.pdf"
            pdf.write_bytes(b"%PDF-1.4 not really")
            with unittest.mock.patch.object(files, "PDFTOTEXT", None), \
                 contextlib.redirect_stderr(err):
                self.assertEqual(files.read_text(pdf), "")
                self.assertEqual(files.read_text(pdf), "")
        self.assertEqual(err.getvalue().count("pdftotext"), 1)
        self.assertIn("poppler", err.getvalue())


class TestMessageTrends(unittest.TestCase):
    """"Which day of the week did I text the most in 2023" cannot be
    answered from eight chunks. A yearly rollup answers it exactly."""

    def rows(self):
        import datetime as dt
        base = dt.datetime(2023, 3, 6, 18, 0, tzinfo=dt.timezone.utc)  # a Monday, noon Denver
        rows = []
        rid = 0
        # Ada: 5 consecutive days, Mondays weighted.
        for d in range(5):
            for _ in range(3 if d == 0 else 1):
                rid += 1
                rows.append({"rowid": rid, "thread": "t1", "handle": "+15550001",
                             "at": (base + dt.timedelta(days=d)).timestamp(),
                             "mine": rid % 2 == 0, "text": "hi"})
        # Bob: two days with a gap.
        for d in (0, 2):
            rid += 1
            rows.append({"rowid": rid, "thread": "t2", "handle": "+15550002",
                         "at": (base + dt.timedelta(days=d)).timestamp(),
                         "mine": False, "text": "yo"})
        return rows

    def trends(self):
        from zoneinfo import ZoneInfo
        cs = list(imessage.trend_chunks(self.rows(), {"+15550001": "Ada"},
                                        5000, ZoneInfo("America/Denver")))
        self.assertEqual([c.ref for c in cs], ["messages:trends:2023"])
        return cs[0].text

    def test_the_busiest_weekday_is_named(self):
        self.assertIn("busiest day of the week: Monday", self.trends())

    def test_top_contacts_use_names_and_counts(self):
        text = self.trends()
        self.assertIn("Ada (7)", text)
        self.assertIn("+15550002 (2)", text)

    def test_the_longest_streak_is_named_with_its_dates(self):
        text = self.trends()
        self.assertIn("Ada 5 days (2023-03-06 to 2023-03-10)", text)

    def test_sent_and_received_are_split(self):
        text = self.trends()
        self.assertIn("9 messages", text)
        self.assertIn("sent", text)
        self.assertIn("received", text)

    def test_participants_carry_the_raw_handles(self):
        from zoneinfo import ZoneInfo
        cs = list(imessage.trend_chunks(self.rows(), {}, 5000, ZoneInfo("UTC")))
        self.assertEqual(set(cs[0].participants), {"+15550001", "+15550002"})


class TestTwitterTrends(unittest.TestCase):
    def test_tweets_per_year_with_peak_hour_and_retweet_share(self):
        from zoneinfo import ZoneInfo
        tweets = [tweet("hello", "Wed Jun 20 03:00:00 +0000 2018")] * 4 + \
                 [tweet("RT @x: y", "Wed Jun 20 15:00:00 +0000 2018")]
        cs = list(twitter.trend_chunks(tweets, [], {}, "111", 5000,
                                       ZoneInfo("America/Denver")))
        self.assertEqual([c.ref for c in cs], ["twitter:trends:2018"])
        text = cs[0].text
        self.assertIn("5 tweets", text)
        self.assertIn("1 retweet", text)
        self.assertIn("peak hour 21:00", text)     # 03:00 UTC is 21:00 in Denver in June
        self.assertIn("busiest day of the week: Tuesday", text)


class TestEmailTrends(unittest.TestCase):
    def test_top_senders_are_labelled_person_or_service(self):
        from zoneinfo import ZoneInfo
        threads = {
            "1": [{"sender": "noreply@shop.example", "at": "2021-03-01T10:00:00Z",
                   "subject": "s", "text": "t"}] * 3,
            "2": [{"sender": "ada@example.org", "at": "2021-05-02T10:00:00Z",
                   "subject": "s", "text": "t"}] * 2,
        }
        cs = list(mbox.trend_chunks(threads, {"ada@example.org": "Ada"}, 5000,
                                    ZoneInfo("UTC")))
        self.assertEqual([c.ref for c in cs], ["email:trends:2021"])
        text = cs[0].text
        self.assertIn("2 threads", text)
        self.assertIn("5 messages", text)
        self.assertIn("noreply@shop.example (3, service)", text)
        self.assertIn("Ada (2, person)", text)
        self.assertIn("busiest month: March", text)
