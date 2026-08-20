import pathlib
import tempfile
import unittest

from recall import contacts, naming

VCF = """BEGIN:VCARD
VERSION:3.0
FN:Aaron Amin
TEL;TYPE=CELL:+1 (678) 697-5225
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Beth Carter
TEL:4043844361
EMAIL:Beth@Example.com
END:VCARD
BEGIN:VCARD
VERSION:3.0
TEL:+15550001111
END:VCARD
"""


class TestNormalizePhone(unittest.TestCase):
    """A contact list writes one number five ways. Every one has to land on
    the form the corpus stores or the join finds nothing."""

    def test_already_normal_passes_through(self):
        self.assertEqual(contacts.normalize_phone("+14044447376"), "+14044447376")

    def test_it_strips_formatting(self):
        self.assertEqual(contacts.normalize_phone("+1 (678) 697-5225"),
                         "+16786975225")

    def test_a_bare_ten_digit_number_gains_a_country_code(self):
        self.assertEqual(contacts.normalize_phone("4043844361"), "+14043844361")

    def test_eleven_digits_starting_with_one_normalize(self):
        self.assertEqual(contacts.normalize_phone("1-404-384-4361"),
                         "+14043844361")

    def test_an_international_number_keeps_its_own_code(self):
        """Forcing +1 onto a foreign number attaches the wrong name to a real
        conversation."""
        self.assertEqual(contacts.normalize_phone("+44 20 7946 0958"),
                         "+442079460958")

    def test_a_short_code_is_rejected(self):
        """Five and six digit senders are banks and 2FA, not people."""
        self.assertIsNone(contacts.normalize_phone("262966"))

    def test_junk_is_rejected(self):
        self.assertIsNone(contacts.normalize_phone("not a phone"))
        self.assertIsNone(contacts.normalize_phone(""))


class TestParse(unittest.TestCase):
    def write(self, body):
        d = pathlib.Path(tempfile.mkdtemp())
        p = d / "contacts.vcf"
        p.write_text(body)
        return p

    def test_it_maps_phones_to_names(self):
        m = contacts.load(self.write(VCF))
        self.assertEqual(m["+16786975225"], "Aaron Amin")
        self.assertEqual(m["+14043844361"], "Beth Carter")

    def test_it_maps_emails_lowercased(self):
        m = contacts.load(self.write(VCF))
        self.assertEqual(m["beth@example.com"], "Beth Carter")

    def test_a_nameless_card_is_skipped(self):
        """An empty name would replace a usable number with nothing."""
        m = contacts.load(self.write(VCF))
        self.assertNotIn("+15550001111", m)

    def test_the_first_card_wins_a_shared_number(self):
        """Two cards claiming one number is usually a shared landline.
        Deciding it deterministically stops the map moving between runs."""
        m = contacts.load(self.write(
            "BEGIN:VCARD\nFN:Alice\nTEL:+14044447376\nEND:VCARD\n"
            "BEGIN:VCARD\nFN:Bob\nTEL:+14044447376\nEND:VCARD\n"))
        self.assertEqual(m["+14044447376"], "Alice")

    def test_detect_finds_a_vcard_anywhere_under_the_root(self):
        p = self.write(VCF)
        self.assertEqual(contacts.detect(p.parent), [p])

    def test_detect_returns_nothing_when_absent(self):
        self.assertEqual(contacts.detect(pathlib.Path(tempfile.mkdtemp())), [])

    def test_load_all_merges_every_file(self):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "a.vcf").write_text("BEGIN:VCARD\nFN:A\nTEL:+15551110000\nEND:VCARD\n")
        (d / "b.vcf").write_text("BEGIN:VCARD\nFN:B\nTEL:+15552220000\nEND:VCARD\n")
        m = contacts.load_all(d)
        self.assertEqual(len(m), 2)


class TestNaming(unittest.TestCase):
    """A contact list named 52 percent of one corpus. The name goes in the
    chunk TEXT; the raw identifier stays as the join key for the next
    contacts update, because a display name cannot be re-mapped."""

    MAP = {"+16786467057": "cal", "beth@example.com": "Beth Carter"}

    def test_a_known_phone_becomes_a_name(self):
        self.assertEqual(naming.label("+16786467057", self.MAP), "cal")

    def test_an_email_matches_case_insensitively(self):
        self.assertEqual(naming.label("BETH@Example.COM", self.MAP),
                         "Beth Carter")

    def test_an_unknown_identifier_stays_identifiable(self):
        """Most participants have no contact. One shared "unknown" bucket
        would merge separate people into a single apparent speaker."""
        self.assertEqual(naming.label("+15550001111", self.MAP), "+15550001111")

    def test_an_empty_map_changes_nothing(self):
        self.assertEqual(naming.label("+16786467057", {}), "+16786467057")

    def test_header_sorts_so_reruns_produce_identical_text(self):
        """Unstable ordering makes an unchanged chunk look changed, and it
        re-embeds for nothing."""
        a = naming.header(["+16786467057", "beth@example.com"], self.MAP)
        b = naming.header(["beth@example.com", "+16786467057"], self.MAP)
        self.assertEqual(a, b)

    def test_header_collapses_duplicates(self):
        self.assertEqual(naming.header(["+16786467057"] * 3, self.MAP), "cal")

    def test_header_with_no_participants_reads_as_unknown(self):
        self.assertEqual(naming.header([], self.MAP), "unknown")


class TestAdaptersUseContacts(unittest.TestCase):
    """The constraint that matters, asserted where chunks are built: the
    stored participant list keeps the raw identifier."""

    def test_the_source_contract_accepts_contacts(self):
        import inspect
        from recall.sources.base import Source
        self.assertIn("contacts", inspect.signature(Source.chunks).parameters)

    def test_every_adapter_accepts_contacts(self):
        import inspect
        from recall.sources import ADAPTERS
        for a in ADAPTERS:
            self.assertIn("contacts", inspect.signature(a.chunks).parameters,
                          f"{a.name} cannot be given a contact map")

    def test_a_message_chunk_shows_the_name_and_keeps_the_identifier(self):
        import datetime as dt
        from recall.sources import imessage
        rows = [{"rowid": 1, "thread": "g", "handle": "+15551234567",
                 "at": 1_600_000_000, "mine": False, "text": "hello"}]
        names = {}
        got = list(imessage.IMessage()._windows(rows, names,
                                                {"+15551234567": "cal"},
                                                5000))
        self.assertIn("cal", got[0].text)
        self.assertEqual(got[0].participants, ["+15551234567"],
                         "the raw identifier is the join key for the next update")

    def test_an_empty_map_leaves_the_text_unchanged(self):
        from recall.sources import imessage
        rows = [{"rowid": 1, "thread": "g", "handle": "+15551234567",
                 "at": 1_600_000_000, "mine": False, "text": "hello"}]
        a = list(imessage.IMessage()._windows(rows, {}, {}, 5000))[0].text
        b = list(imessage.IMessage()._windows(rows, {}, None, 5000))[0].text
        self.assertEqual(a, b)


class TestEveryModuleImports(unittest.TestCase):
    """120 tests passed while cli.py held a syntax error, because nothing
    imported it. Import every module so a broken one cannot ship."""

    def test_all_modules_import(self):
        import importlib
        import pkgutil
        import recall
        failed = []
        for m in pkgutil.walk_packages(recall.__path__, "recall."):
            try:
                importlib.import_module(m.name)
            except Exception as e:
                failed.append(f"{m.name}: {type(e).__name__}: {e}")
        self.assertEqual(failed, [])
