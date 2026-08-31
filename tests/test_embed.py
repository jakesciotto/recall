import io
import json
import unittest
import urllib.error
from unittest import mock

from recall import embed


def items(n, size=10):
    return [{"text": "x" * size, "id": i} for i in range(n)]


def http502():
    return urllib.error.HTTPError("u", 502, "Bad Gateway", None, None)


class TestEmbedSafe(unittest.TestCase):
    """The most expensive lesson in the project. A dead server and an
    oversized request both answer 502. Reacting to a dead server by shrinking
    the request bisects a batch into singles and writes every one off as bad
    data, blaming chunks that were never too large."""

    def test_the_happy_path_makes_one_request(self):
        with mock.patch.object(embed, "embed",
                               return_value=[[0.0]] * 3) as m:
            kept, vecs = embed.embed_safe(items(3))
        self.assertEqual(len(kept), 3)
        m.assert_called_once()

    def test_a_restarted_server_loses_nothing(self):
        calls = {"n": 0}

        def flaky(texts, timeout=300):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http502()
            return [[0.0]] * len(texts)

        with mock.patch.object(embed, "embed", flaky):
            kept, _ = embed.embed_safe(items(8), _ready=lambda: True)
        self.assertEqual(len(kept), 8, "a restart must not drop anything")

    def test_it_checks_health_before_bisecting(self):
        ready = mock.Mock(return_value=True)
        with mock.patch.object(embed, "embed", side_effect=http502()):
            embed.embed_safe(items(1), _ready=ready)
        ready.assert_called()

    def test_a_dead_server_stops_the_run_instead_of_dropping_everything(self):
        """Ending the run is correct: everything already stored survives, and
        the next run resumes. Draining the corpus into a drop list does not."""
        dropped = []
        with mock.patch.object(embed, "embed", side_effect=http502()):
            with self.assertRaises(embed.EmbeddingServerDown):
                embed.embed_safe(items(8), dropped.append,
                                 _ready=lambda: False)
        self.assertEqual(dropped, [])

    def test_a_genuinely_oversized_item_is_isolated_and_dropped(self):
        """A healthy server that still refuses means the input really is too
        large. Only then does bisecting make sense."""
        def picky(texts, timeout=300):
            if any(len(t) > 50 for t in texts):
                raise http502()
            return [[0.0]] * len(texts)

        bad = [{"text": "x" * 100, "id": "big"}]
        good = [{"text": "ok", "id": "small"}]
        dropped = []
        with mock.patch.object(embed, "embed", picky):
            kept, _ = embed.embed_safe(good + bad + good,
                                       lambda i, e: dropped.append(i["id"]),
                                       _ready=lambda: True)
        self.assertEqual([i["id"] for i in kept], ["small", "small"])
        self.assertEqual(dropped, ["big"])

    def test_it_reports_a_restart_to_the_caller(self):
        calls = {"n": 0}

        def flaky(texts, timeout=300):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http502()
            return [[0.0]] * len(texts)

        waits = []
        with mock.patch.object(embed, "embed", flaky):
            embed.embed_safe(items(4), None, waits.append, _ready=lambda: True)
        self.assertEqual(waits, [4])


class TestBatches(unittest.TestCase):
    """Batching by COUNT breaks the moment item length changes: 64 short
    messages fit, 64 document chunks do not."""

    def test_it_groups_within_the_character_budget(self):
        for b in embed.batches(items(10, 300), 1000):
            self.assertLessEqual(sum(len(i["text"]) for i in b), 1000)

    def test_the_count_cap_still_applies_to_tiny_items(self):
        out = list(embed.batches(items(200, 1), 100_000, cap=64))
        self.assertEqual([len(b) for b in out], [64, 64, 64, 8])

    def test_an_oversized_item_goes_alone(self):
        out = list(embed.batches(
            [{"text": "a"}, {"text": "b" * 5000}, {"text": "c"}], 1000))
        self.assertEqual([len(b) for b in out], [1, 1, 1])

    def test_every_item_appears_exactly_once_and_in_order(self):
        src = items(120, 50)
        flat = [i for b in embed.batches(src, 1000) for i in b]
        self.assertEqual([i["id"] for i in flat], [i["id"] for i in src])


def response(vectors):
    """A fake endpoint body holding `vectors` in the server's own shape."""
    return io.BytesIO(json.dumps(
        {"data": [{"index": i, "embedding": v}
                  for i, v in enumerate(vectors)]}).encode())


class TestDimensionFault(unittest.TestCase):
    """The 502 lesson in a new dress. A wrong RECALL_EMBED_DIMS is a setup
    fault, not oversized input, but it reaches embed_safe as an ordinary
    error. The bisect then drops every chunk one at a time and the log blames
    the data. The README invites the fault by advertising a model swap, and
    swapping the model changes the dimension."""

    def test_a_wrong_dimension_is_a_setup_fault(self):
        with mock.patch.object(embed.urllib.request, "urlopen",
                               return_value=response([[0.0] * 768])):
            with self.assertRaises(embed.EmbeddingMisconfigured):
                embed.embed(["a"])

    def test_the_message_names_the_setting_to_change(self):
        with mock.patch.object(embed.urllib.request, "urlopen",
                               return_value=response([[0.0] * 768])):
            with self.assertRaises(embed.EmbeddingMisconfigured) as caught:
                embed.embed(["a"])
        self.assertIn("RECALL_EMBED_DIMS", str(caught.exception))

    def test_a_short_response_is_a_setup_fault_too(self):
        """Fewer vectors than inputs cannot be fixed by sending less."""
        with mock.patch.object(embed.urllib.request, "urlopen",
                               return_value=response([[0.0] * 1024])):
            with self.assertRaises(embed.EmbeddingMisconfigured):
                embed.embed(["a", "b"])

    def test_a_correct_dimension_still_embeds(self):
        with mock.patch.object(embed.urllib.request, "urlopen",
                               return_value=response([[0.5] * 1024])):
            self.assertEqual(embed.embed(["a"]), [[0.5] * 1024])

    def test_it_stops_the_run_instead_of_dropping_the_corpus(self):
        dropped = []
        with mock.patch.object(embed, "embed",
                               side_effect=embed.EmbeddingMisconfigured("x")):
            with self.assertRaises(embed.EmbeddingMisconfigured):
                embed.embed_safe(items(8), dropped.append,
                                 _ready=lambda: True)
        self.assertEqual(dropped, [], "a setup fault must drop nothing")

    def test_it_stops_even_when_the_fault_appears_after_a_restart(self):
        """The server can come back running a different model, so the retry
        inside the 502 path must not swallow the fault either."""
        calls = {"n": 0}

        def swaps_model(texts, timeout=300):
            calls["n"] += 1
            if calls["n"] == 1:
                raise http502()
            raise embed.EmbeddingMisconfigured("x")

        dropped = []
        with mock.patch.object(embed, "embed", swaps_model):
            with self.assertRaises(embed.EmbeddingMisconfigured):
                embed.embed_safe(items(8), dropped.append,
                                 _ready=lambda: True)
        self.assertEqual(dropped, [])
