import pathlib
import tempfile
import unittest
from unittest import mock

from recall import ingest, naming
from recall.sources import base


def chunk(ref, text):
    return base.Chunk(ref=ref, text=text, source="test")


class TestDigest(unittest.TestCase):
    """The digest must agree with Postgres md5(text), because that is what
    the stored side computes. Both values below came back identical from a
    real UTF8 database and from md5sum over the UTF-8 bytes."""

    def test_it_matches_postgres_md5_on_ascii(self):
        self.assertEqual(ingest.digest("hello"),
                         "5d41402abc4b2a76b9719d911017c592")

    def test_it_matches_postgres_md5_on_utf8(self):
        """The encoding is the part that drifts. Postgres hashes the bytes in
        the database encoding, and docker compose sets that to UTF8."""
        self.assertEqual(ingest.digest("café"),
                         "07117fe4a1ebd544965dc19573183da2")


class TestChanged(unittest.TestCase):
    """Skipping on ref alone is what makes a re-run cheap. It is also why a
    vCard dropped in after the first ingest changed nothing: the ref is
    stable by design, so renamed text read as already held. Comparing the
    text digest keeps the re-run cheap and still catches a rewrite."""

    def test_a_new_chunk_is_pending(self):
        work = ingest.changed([chunk("a", "one")], {})
        self.assertEqual([c.ref for c in work.pending], ["a"])
        self.assertEqual((work.new, work.updated), (1, 0))

    def test_an_unchanged_chunk_is_skipped(self):
        """Losing this re-embeds the whole corpus on every run."""
        work = ingest.changed([chunk("a", "one")],
                              {"a": ingest.digest("one")})
        self.assertEqual(work.pending, [])
        self.assertEqual((work.new, work.updated), (0, 0))

    def test_a_rewritten_chunk_reloads(self):
        work = ingest.changed(
            [chunk("a", "[with Ada Lovelace] hi")],
            {"a": ingest.digest("[with +15551234567] hi")})
        self.assertEqual([c.ref for c in work.pending], ["a"])
        self.assertEqual((work.new, work.updated), (0, 1))

    def test_it_counts_new_and_updated_separately(self):
        stored = {"a": ingest.digest("old"), "b": ingest.digest("same")}
        work = ingest.changed(
            [chunk("a", "new text"), chunk("b", "same"), chunk("c", "fresh")],
            stored)
        self.assertEqual((work.new, work.updated), (1, 1))

    def test_pending_keeps_generation_order(self):
        work = ingest.changed(
            [chunk("a", "1"), chunk("b", "2"), chunk("c", "3")],
            {"b": ingest.digest("old")})
        self.assertEqual([c.ref for c in work.pending], ["a", "b", "c"])

    def test_a_ref_the_source_stopped_producing_is_left_alone(self):
        """Deleting a stored row is a separate decision, and this does not
        make it. A source that stops emitting a ref leaves the row in place."""
        work = ingest.changed([chunk("a", "one")], {"gone": "0" * 32})
        self.assertEqual([c.ref for c in work.pending], ["a"])
        self.assertEqual((work.new, work.updated), (1, 0))

    def test_it_reads_a_plain_dict_too(self):
        work = ingest.changed([{"ref": "a", "text": "one", "source": "t"}], {})
        self.assertEqual((work.new, work.updated), (1, 0))


VCARD = """BEGIN:VCARD
VERSION:3.0
FN:Ada Lovelace
TEL;TYPE=CELL:+1 555 123 4567
END:VCARD
"""


class Phonebook(base.Source):
    """A source whose chunk text names a participant, which is exactly what
    a contact map rewrites."""

    name = "phonebook"

    def detect(self, root):
        return [root]

    def samples(self, path):
        return ["x" * 100]

    def chunks(self, path, budget, contacts=None):
        who = naming.label("+15551234567", contacts or {})
        yield base.Chunk(ref="phonebook:1", text=f"[with {who}] hi",
                         source=self.name, occurred_at="2020-01-01T00:00:00Z",
                         date_confidence="exact")


class Store:
    """Stands in for Postgres and keeps rows between runs. That is the whole
    property under test: the second run must see the first run's work."""

    def __init__(self):
        self.text = {}
        self.writes = []

    def apply_schema(self, conn):
        pass

    def stored_digests(self, conn):
        return {ref: ingest.digest(t) for ref, t in self.text.items()}

    def upsert(self, conn, rows):
        for row in rows:
            self.text[row[0]] = row[1]
            self.writes.append(row[0])

    def build_vector_index(self, conn):
        pass


class Conn:
    def commit(self):
        pass


class TestContactsAddedLater(unittest.TestCase):
    """The README says a vCard makes participants show names. Skipping on ref
    alone made that true only on a first ingest."""

    def ingest(self, store, root):
        with mock.patch.object(ingest, "db", store), \
             mock.patch.object(ingest.chunking, "calibrate",
                               return_value=8000), \
             mock.patch.object(ingest.embedding, "embed_safe",
                               lambda g, d, w: (g, [[0.0]] * len(g))), \
             mock.patch.object(ingest, "detect_all",
                               lambda r: [(Phonebook(), r)]):
            return ingest.run(root, Conn(), log=lambda *a: None)

    def test_a_vcard_dropped_in_later_renames_the_stored_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            store = Store()
            self.ingest(store, root)
            self.assertIn("+15551234567", store.text["phonebook:1"])

            (root / "contacts.vcf").write_text(VCARD)
            summary = self.ingest(store, root)

            self.assertIn("Ada Lovelace", store.text["phonebook:1"])
            self.assertEqual(summary["phonebook"]["updated"], 1)

    def test_a_run_that_changes_nothing_writes_nothing(self):
        """Re-runs must stay cheap. A digest that moves on its own re-embeds
        the whole corpus every time."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "contacts.vcf").write_text(VCARD)
            store = Store()
            self.ingest(store, root)
            before = len(store.writes)

            summary = self.ingest(store, root)

            self.assertEqual(len(store.writes), before)
            self.assertEqual(summary["phonebook"],
                             {"new": 0, "updated": 0, "dropped": 0})


class TestRowsNeverCarryNul(unittest.TestCase):
    """Postgres text can never hold 0x00. One email chunk with a NUL byte
    raised DataError inside upsert, nothing caught it, and every source
    after email never ran. The strip lives at the one point every row
    passes through, so no adapter has to remember it."""

    def test_nul_is_stripped_from_every_text_column(self):
        chunk = base.Chunk(ref="email:1", text="before\x00after",
                           source="email", thread="t\x00", path="p\x00.eml",
                           participants=["a\x00@x"])
        ref, text, source, _, _, participants, thread, path, _ = ingest._row(chunk)
        self.assertEqual(text, "beforeafter")
        self.assertEqual(thread, "t")
        self.assertEqual(path, "p.eml")
        self.assertEqual(participants, ["a@x"])

    def test_a_dict_chunk_is_stripped_the_same_way(self):
        row = ingest._row({"ref": "x", "text": "a\x00b", "source": "s"})
        self.assertEqual(row[1], "ab")

    def test_the_digest_sees_the_stripped_text(self):
        """Otherwise the stored text never matches what the source produces
        and the chunk re-embeds on every run."""
        chunk = base.Chunk(ref="email:1", text="a\x00b", source="email")
        stored = {"email:1": ingest.digest("ab")}
        self.assertEqual(ingest.changed([chunk], stored).pending, [])
