import io
import json
import unittest
import urllib.error
from unittest import mock

from recall import vision


def replies(*bodies):
    """A fake urlopen that answers each call with the next body, or raises it."""
    queue = list(bodies)

    def urlopen(req, timeout=None):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(json.dumps(item).encode())
    return urlopen


def answer(text):
    return {"choices": [{"message": {"content": text}}]}


class TestCaption(unittest.TestCase):
    def test_the_payload_carries_the_image_and_the_fixed_prompt(self):
        p = vision.payload(b"\xff\xd8jpeg", "gemma")
        content = p["messages"][0]["content"]
        self.assertEqual(p["model"], "gemma")
        self.assertTrue(content[0]["image_url"]["url"]
                        .startswith("data:image/jpeg;base64,"))
        self.assertEqual(content[1]["text"], vision.PROMPT)

    def test_it_returns_the_caption_text(self):
        with mock.patch.object(vision.urllib.request, "urlopen",
                               replies(answer(" a dog on a beach "))):
            self.assertEqual(vision.caption(b"x", url="http://v", model="m"),
                             "a dog on a beach")

    def test_an_empty_caption_is_retried(self):
        naps = []
        with mock.patch.object(vision.urllib.request, "urlopen",
                               replies(answer(""), answer("a dog"))):
            text = vision.caption(b"x", url="http://v", model="m",
                                  sleep=naps.append)
        self.assertEqual(text, "a dog")
        self.assertEqual(naps, [1])

    def test_three_failures_raise_with_the_last_reason(self):
        naps = []
        down = urllib.error.URLError("refused")
        with mock.patch.object(vision.urllib.request, "urlopen",
                               replies(down, down, down)):
            with self.assertRaises(vision.CaptionError) as ctx:
                vision.caption(b"x", url="http://v", model="m",
                               sleep=naps.append)
        self.assertIn("refused", str(ctx.exception))
        self.assertEqual(naps, [1, 2])

    def test_no_endpoint_is_a_named_error_not_a_request(self):
        with mock.patch.object(vision.config, "VISION_URL", ""):
            with self.assertRaises(vision.CaptionError) as ctx:
                vision.caption(b"x")
        self.assertIn("RECALL_VISION_URL", str(ctx.exception))
