import unittest

from recall import answer


def hit(text, ref="message:1", when="2021-09-12T15:00:00Z"):
    return {"ref": ref, "text": text, "occurred_at": when, "source": "messages"}


class TestSpeakerLabels(unittest.TestCase):
    """The corpus labels the user's lines "me:", and that label binds the
    subject to the user. Asked who did a half ironman, a model answered "the
    user (me)" from a source where the user was congratulating somebody
    else. The same line relabelled with a name, nothing else changed, made
    the same model name the right person."""

    def test_a_leading_me_becomes_the_label(self):
        self.assertEqual(answer.speaker_labels("me: nice race", "Ada"),
                         "Ada: nice race")

    def test_every_line_is_relabelled(self):
        text = "them: hi\nme: hello\nme: again"
        self.assertEqual(answer.speaker_labels(text, "Ada"),
                         "them: hi\nAda: hello\nAda: again")

    def test_me_inside_a_line_is_untouched(self):
        """Anchored to the start of a line, or "tell me: what time" and any
        word ending in me would be rewritten."""
        text = "them: tell me: what time\nthem: same: as before"
        self.assertEqual(answer.speaker_labels(text, "Ada"), text)

    def test_no_label_means_no_change(self):
        """Storage keeps "me:" because every embedding was computed from it.
        With no label configured the prompt matches the corpus exactly."""
        self.assertEqual(answer.speaker_labels("me: hi", ""), "me: hi")
        self.assertEqual(answer.speaker_labels("me: hi", None), "me: hi")


class TestBuildPrompt(unittest.TestCase):
    def test_sources_are_numbered_from_one_and_carry_a_date(self):
        prompt = answer.build_prompt("q", [hit("a"), hit("b", ref="message:2")])
        self.assertIn("[1] message:1 (2021-09-12)\na", prompt)
        self.assertIn("[2] message:2 (2021-09-12)\nb", prompt)

    def test_the_relabel_reaches_every_source(self):
        prompt = answer.build_prompt("q", [hit("me: one"), hit("me: two")],
                                     label="Ada")
        self.assertNotIn("me:", prompt)
        self.assertEqual(prompt.count("Ada:"), 2)

    def test_no_hits_forbids_an_answer(self):
        """A bare question about the user's own life gets answered from the
        model's weights, and nothing downstream can tell that from recall."""
        prompt = answer.build_prompt("q", [])
        self.assertIn("do not answer from your own knowledge", prompt)


class TestSystemPrompt(unittest.TestCase):
    def test_it_demands_attribution(self):
        self.assertIn("Never report another person's experience as the "
                      "user's own", answer.system_prompt(""))

    def test_it_names_the_label_when_one_is_set(self):
        self.assertIn("the user (Ada)", answer.system_prompt("Ada"))

    def test_it_never_describes_a_label_the_prompt_no_longer_carries(self):
        """An earlier version said "me:" marks the user while the relabel
        had already rewritten every such line. An instruction about absent
        text teaches the model the wrong shape."""
        self.assertNotIn('"me:"', answer.system_prompt("Ada"))
        self.assertNotIn("me:", answer.system_prompt(""))


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        import json
        return json.dumps(self.payload).encode()


class TestChatMeta(unittest.TestCase):
    """The log records what the server actually did, not what was asked."""

    def test_it_fills_the_resolved_model_and_the_token_counts(self):
        from unittest import mock
        import urllib.request
        payload = {"model": "/models/llama3.1-Q8.gguf",
                   "choices": [{"message": {"content": "hi [1]"}}],
                   "usage": {"prompt_tokens": 120, "completion_tokens": 9}}
        meta = {}
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=Response(payload)), \
             mock.patch.object(answer.config, "CHAT_URL", "http://x/v1"):
            text = answer.chat("prompt", meta=meta)
        self.assertEqual(text, "hi [1]")
        self.assertEqual(meta["model_resolved"], "/models/llama3.1-Q8.gguf")
        self.assertEqual((meta["prompt_tokens"], meta["completion_tokens"]),
                         (120, 9))
        self.assertIsInstance(meta["generate_ms"], int)

    def test_a_response_without_usage_still_answers(self):
        from unittest import mock
        import urllib.request
        payload = {"choices": [{"message": {"content": "hi"}}]}
        meta = {}
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=Response(payload)), \
             mock.patch.object(answer.config, "CHAT_URL", "http://x/v1"):
            self.assertEqual(answer.chat("prompt", meta=meta), "hi")
        self.assertIsNone(meta.get("prompt_tokens"))
