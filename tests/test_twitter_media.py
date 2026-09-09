import pathlib
import tempfile
import unittest
from unittest import mock

from recall import captions, config
from recall.sources import twitter
from test_sources import conversation, dm, tweet, twitter_export


def tweet_with_id(tid, text, created_at="Wed Jun 20 12:00:00 +0000 2018",
                  mentions=()):
    t = tweet(text, created_at, mentions)
    t["tweet"]["id_str"] = tid
    return t


def media_file(data, folder, name, content=b"\xff\xd8\xff pixels"):
    p = pathlib.Path(data) / folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def captioned(work, file, **rec):
    sha = captions.HashCache(work).get(file)
    captions.write_record(work, {"sha256": sha, "caption": None,
                                 "ocr_text": None, "ocr_chars": 0,
                                 "skipped": None, **rec})
    return sha


def media_chunks(data, work, budget=8000):
    with mock.patch.object(config, "WORK_DIR", pathlib.Path(work)):
        return [c for c in twitter.Twitter().chunks(data, budget)
                if c.ref.startswith("twitter-media:")]


class TestMediaId(unittest.TestCase):
    """Twitter names every media file <parent id>-<hash>.<ext>. The hash can
    start with a dash, so the id is the leading digit run."""

    def test_the_leading_digits_are_the_parent_id(self):
        self.assertEqual(twitter.media_id("1000148468948709376-DeE9-x.mp4"),
                         "1000148468948709376")

    def test_a_name_without_the_prefix_has_no_parent(self):
        self.assertIsNone(twitter.media_id("cover.jpg"))


class TestMedia(unittest.TestCase):
    def test_it_lists_the_three_media_folders_and_skips_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(d, [tweet("a")])
            a = media_file(data, "tweets_media", "1-a.jpg")
            b = media_file(data, "direct_messages_media", "2-b.png")
            c = media_file(data, "direct_messages_group_media", "3-c.jpg")
            media_file(data, "tweets_media", ".DS_Store", b"junk")
            self.assertEqual(twitter.Twitter().media(data), [c, b, a][::-1])


class TestMediaChunks(unittest.TestCase):
    def test_a_tweet_image_carries_its_tweet(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(d, [tweet_with_id("77", "hello world")])
            work = pathlib.Path(d) / "work"
            f = media_file(data, "tweets_media", "77-abc.jpg")
            sha = captioned(work, f, caption="a dog")
            [c] = media_chunks(data, work)
        self.assertEqual(c.ref, f"twitter-media:{sha}")
        self.assertEqual(c.source, "twitter")
        self.assertEqual(c.occurred_at, "2018-06-20T12:00:00Z")
        self.assertEqual(c.date_confidence, "exact")
        self.assertEqual(c.text, "[2018-06-20, tweet media]\nImage: a dog\n"
                                 "Posted with: hello world")

    def test_a_dm_image_carries_the_message_and_its_sender(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(
                d, [tweet("hi @ann", mentions=[("42", "ann")])],
                [conversation("c1", [dm("5", "42", "look")])])
            work = pathlib.Path(d) / "work"
            f = media_file(data, "direct_messages_media", "5-xyz.png")
            captioned(work, f, caption="a cat")
            [c] = media_chunks(data, work)
        self.assertEqual(c.text, "[2018-06-20, DM media with ann]\n"
                                 "Image: a cat\nSaid with: ann: look")
        self.assertEqual(c.participants, ["42"])
        self.assertEqual(c.thread, "c1")

    def test_your_own_dm_image_reads_as_me(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(d, [tweet("a")],
                                  [conversation("c1", [dm("5", "111", "mine")])])
            work = pathlib.Path(d) / "work"
            captioned(work, media_file(data, "direct_messages_media", "5-q.jpg"),
                      caption="a cat")
            [c] = media_chunks(data, work)
        self.assertEqual(c.text, "[2018-06-20, DM media]\nImage: a cat\n"
                                 "Said with: me: mine")
        self.assertEqual(c.participants, [])

    def test_an_orphan_keeps_its_chunk_undated(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(d, [tweet("a")])
            work = pathlib.Path(d) / "work"
            captioned(work, media_file(data, "tweets_media", "999-zzz.jpg"),
                      caption="a bridge")
            [c] = media_chunks(data, work)
        self.assertEqual(c.text, "[undated, tweet media]\nImage: a bridge")
        self.assertIsNone(c.occurred_at)
        self.assertEqual(c.date_confidence, "low")

    def test_a_seeded_video_caption_reads_as_a_video(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(d, [tweet_with_id("77", "clip")])
            work = pathlib.Path(d) / "work"
            captioned(work, media_file(data, "tweets_media", "77-v.mp4", b"mp4"),
                      caption="a dog running", kind="video")
            [c] = media_chunks(data, work)
        self.assertIn("\nVideo: a dog running\n", c.text)

    def test_long_text_is_cut_and_the_chunk_fits(self):
        for budget in (300, 1000, 8000):
            with self.subTest(budget=budget), \
                 tempfile.TemporaryDirectory() as d:
                data = twitter_export(d, [tweet("a")],
                                      [conversation("c1", [dm("5", "999", "y" * 20000)])])
                work = pathlib.Path(d) / "work"
                captioned(work, media_file(data, "direct_messages_media", "5-q.jpg"),
                          caption="a sign", ocr_text="w" * 3000, ocr_chars=3000)
                [c] = media_chunks(data, work, budget=budget)
                self.assertLessEqual(len(c.text), budget)
                self.assertIn("\nText in image: ", c.text)
                self.assertIn("\nSaid with: ", c.text)

    def test_gated_uncaptioned_and_unhashed_media_yield_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            data = twitter_export(d, [tweet_with_id("77", "x")])
            work = pathlib.Path(d) / "work"
            captioned(work, media_file(data, "tweets_media", "77-a.jpg"),
                      skipped="below pixel gate")
            captioned(work, media_file(data, "tweets_media", "77-b.jpg", b"\xff\xd8\xff b"))
            media_file(data, "tweets_media", "77-c.jpg", b"\xff\xd8\xff c")
            with mock.patch.object(captions, "sha256_of",
                                   side_effect=AssertionError("ingest hashed")):
                self.assertEqual(media_chunks(data, work), [])
