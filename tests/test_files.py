"""The documents adapter, after the lessons a 9,745-file archive taught.

Extensions lie in both directions, the mtime records a sync rather than a
writing, two copies of one file are one document, and the exclusion list is
the user's to write. Each class below pins one of those.
"""

import pathlib
import shutil
import tempfile
import unittest
import zipfile

from recall.sources import files


def write(root, rel, data):
    p = pathlib.Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())
    return p


def docx_bytes(text, created=None):
    """The smallest thing Word would still open: one paragraph, and the core
    properties file when a creation date is wanted."""
    buf = tempfile.SpooledTemporaryFile()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                   'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                   f"{text}</w:t></w:r></w:p></w:body></w:document>")
        if created:
            z.writestr("docProps/core.xml",
                       '<cp:coreProperties xmlns:cp="x" xmlns:dcterms="y" '
                       'xmlns:xsi="z"><dcterms:created xsi:type="dcterms:'
                       f'W3CDTF">{created}</dcterms:created></cp:coreProperties>')
    buf.seek(0)
    return buf.read()


S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def xlsx_bytes():
    """One sheet named Budget: a header of shared strings, then a row with a
    shared string, a number, and an inline string."""
    buf = tempfile.SpooledTemporaryFile()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="{S}"><sheets><sheet name="Budget" '
                   'sheetId="1"/></sheets></workbook>')
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="{S}"><si><t>Item</t></si><si><t>Cost</t></si>'
                   '<si><t>Sauna</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="{S}"><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                   '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>42.5</v></c>'
                   '<c r="C2" t="inlineStr"><is><t>deposit paid</t></is></c></row>'
                   '</sheetData></worksheet>')
    buf.seek(0)
    return buf.read()


PDF = (b"%PDF-1.4\n"
       b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
       b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
       b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
       b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
       b"4 0 obj << /Length 44 >> stream\n"
       b"BT /F1 12 Tf 20 100 Td (hello pdf) Tj ET\n"
       b"endstream endobj\n"
       b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
       b"6 0 obj << /CreationDate (D:20160304100000Z) >> endobj\n"
       b"trailer << /Root 1 0 R /Info 6 0 R >>\n%%EOF\n")


class TestSniffing(unittest.TestCase):
    """The header does not lie. The extension did, in both directions, on
    the first real archive: a Photoshop file named .pdf became two million
    characters of noise, and PDFs named .txt were read as text."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_a_pdf_header_is_a_pdf_whatever_the_name(self):
        p = write(self.dir, "notes.txt", b"%PDF-1.4 rest")
        self.assertEqual(files.sniff(p), "pdf")

    def test_whitespace_before_the_pdf_header_is_still_a_pdf(self):
        """poppler scans the first block for %PDF; one real file led with
        newlines and tabs, and a strict prefix check called it binary."""
        p = write(self.dir, "scan.pdf", b"\n\n\t\t%PDF-1.3 rest")
        self.assertEqual(files.sniff(p), "pdf")

    def test_a_photoshop_file_named_pdf_is_binary(self):
        p = write(self.dir, "resume.pdf", b"8BPS\x00\x01" + b"\x00" * 100)
        self.assertEqual(files.sniff(p), "binary")

    def test_a_docx_named_txt_is_office(self):
        p = write(self.dir, "essay.txt", docx_bytes("hello"))
        self.assertEqual(files.sniff(p), "docx")

    def test_a_legacy_word_file_is_ole(self):
        p = write(self.dir, "memo.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        self.assertEqual(files.sniff(p), "ole")

    def test_rtf_is_rtf(self):
        p = write(self.dir, "memo.doc", b"{\\rtf1\\ansi Hello}")
        self.assertEqual(files.sniff(p), "rtf")

    def test_plain_text_falls_to_its_extension(self):
        self.assertEqual(files.sniff(write(self.dir, "a.md", "# hi")), "text")
        self.assertEqual(files.sniff(write(self.dir, "a.html", "<p>hi")), "html")


class TestBinaryGuard(unittest.TestCase):
    """A NUL byte or too few printable bytes means binary. A byte order
    mark is checked first, because UTF-16 ASCII is half NUL bytes and the
    guard would otherwise throw away every such file, silently."""

    def test_a_nul_byte_means_binary(self):
        self.assertTrue(files.is_binary(b"abc\x00def"))

    def test_high_bytes_without_a_nul_still_mean_binary(self):
        self.assertTrue(files.is_binary(bytes(range(128, 256)) * 8))

    def test_ordinary_text_is_not_binary(self):
        self.assertFalse(files.is_binary(b"just some words\n" * 50))

    def test_utf16_with_a_bom_is_text(self):
        raw = b"\xff\xfe" + "hello".encode("utf-16-le")
        self.assertFalse(files.is_binary(raw))
        self.assertEqual(files.decode_bytes(raw), "hello")

    def test_utf8_in_a_non_latin_script_is_text(self):
        """A printable-byte ratio alone calls every Japanese note binary,
        because UTF-8 multibyte text has no ASCII in it."""
        self.assertFalse(files.is_binary("日本語のメモです。\n".encode() * 200))

    def test_a_multibyte_char_cut_at_the_probe_boundary_is_still_text(self):
        raw = b"a" * (files.PROBE - 1) + "é".encode() * 10
        self.assertFalse(files.is_binary(raw))

    def test_windows_1252_decodes_rather_than_replaces(self):
        self.assertEqual(files.decode_bytes("caf\xe9".encode("cp1252")), "caf\xe9")


class TestReadText(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_a_utf16_text_file_is_read(self):
        p = write(self.dir, "notes.txt",
                  b"\xff\xfe" + "hello there".encode("utf-16-le"))
        self.assertEqual(files.read_text(p), "hello there")

    def test_a_docx_named_txt_is_read_as_office(self):
        p = write(self.dir, "essay.txt", docx_bytes("hello from word"))
        self.assertIn("hello from word", files.read_text(p))

    def test_a_photoshop_file_named_pdf_yields_nothing(self):
        p = write(self.dir, "resume.pdf", b"8BPS" + b"\x01\x00" * 2000)
        self.assertEqual(files.read_text(p), "")

    def test_binary_without_nul_bytes_yields_nothing(self):
        p = write(self.dir, "data.txt", bytes(range(128, 256)) * 64)
        self.assertEqual(files.read_text(p), "")

    def test_rtf_control_words_are_stripped(self):
        p = write(self.dir, "memo.rtf",
                  b"{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Arial;}}"
                  b"\\f0\\fs24 Hello\\par World}")
        text = files.read_text(p)
        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("\\par", text)
        self.assertNotIn("fonttbl", text)

    @unittest.skipUnless(shutil.which("pdftotext"), "needs poppler")
    def test_a_pdf_named_txt_is_read_through_pdftotext(self):
        p = write(self.dir, "scan.txt", PDF)
        text = files.read_text(p)
        self.assertIn("hello pdf", text)
        self.assertNotIn("endobj", text, "read as source, not as a PDF")


class TestSizeCap(unittest.TestCase):
    """Thirty-one CSV exports held 64 percent of every character in one
    archive. A file is read up to MAX_CHARS and the rest is left, which
    keeps the header and the first rows without drowning the corpus."""

    def test_text_past_the_cap_is_not_read(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = write(d, "big.csv", "row,row,row\n" * (files.MAX_CHARS // 10))
        self.assertLessEqual(len(files.read_text(p)), files.MAX_CHARS)


class TestSpreadsheets(unittest.TestCase):
    """A grid read as shared strings only keeps the labels and loses every
    number. Rows come out tab-joined under the sheet name, so a question
    about a figure can find the row that holds it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.text = files.read_text(write(self.dir, "b.xlsx", xlsx_bytes()))

    def test_numbers_survive(self):
        self.assertIn("42.5", self.text)

    def test_a_row_is_one_tab_joined_line(self):
        self.assertIn("Sauna\t42.5\tdeposit paid", self.text)

    def test_the_sheet_name_leads(self):
        self.assertIn("[sheet: Budget]", self.text)
        self.assertLess(self.text.index("Budget"), self.text.index("Item"))


class TestSkips(unittest.TestCase):
    """Two names macOS leaves in every export: AppleDouble resource forks,
    which carry the real file's extension, and Finder's .DS_Store."""

    def test_an_appledouble_fork_is_skipped(self):
        self.assertFalse(files.keep(pathlib.Path("a/._report.pdf")))

    def test_ds_store_is_skipped(self):
        self.assertFalse(files.keep(pathlib.Path("a/.DS_Store")))

    def test_a_file_another_adapter_owns_is_left_to_it(self):
        """A Takeout dropped under documents/ would index its 146 MB
        activity file as HTML text and again through the activity adapter."""
        self.assertFalse(files.keep(pathlib.Path("Takeout/My Activity/Search/MyActivity.html")))
        self.assertFalse(files.keep(pathlib.Path("YouTube and YouTube Music/history/watch-history.html")))

    def test_a_dotfile_with_a_real_name_is_kept(self):
        self.assertTrue(files.keep(pathlib.Path("a/.plan.md")))


class TestIgnoreFile(unittest.TestCase):
    """The user's own exclusions, one pattern per line in
    documents/.recallignore. A pattern without a slash matches a name or a
    directory at any depth; one with a slash matches the relative path."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def ignore(self, text):
        write(self.dir, ".recallignore", text)
        return files.Ignore.load(pathlib.Path(self.dir))

    def rel(self, s):
        return pathlib.Path(s)

    def test_no_file_ignores_nothing(self):
        ig = files.Ignore.load(pathlib.Path(self.dir))
        self.assertFalse(ig.matches(self.rel("anything.pdf")))

    def test_a_name_pattern_matches_a_file_at_any_depth(self):
        ig = self.ignore("secret-*.pdf\n")
        self.assertTrue(ig.matches(self.rel("deep/er/secret-1.pdf")))
        self.assertFalse(ig.matches(self.rel("deep/er/open.pdf")))

    def test_a_name_pattern_matches_a_directory_at_any_depth(self):
        ig = self.ignore("drafts\n")
        self.assertTrue(ig.matches(self.rel("a/drafts/b.txt")))
        self.assertFalse(ig.matches(self.rel("a/final/b.txt")))

    def test_a_path_pattern_matches_the_relative_path(self):
        ig = self.ignore("finance/tax*\n")
        self.assertTrue(ig.matches(self.rel("finance/tax-2019.pdf")))
        self.assertFalse(ig.matches(self.rel("personal/tax-2019.pdf")))

    def test_comments_and_blank_lines_are_ignored(self):
        ig = self.ignore("# books I did not write\n\nphp-solutions.pdf\n")
        self.assertTrue(ig.matches(self.rel("x/php-solutions.pdf")))
        self.assertFalse(ig.matches(self.rel("#")))

    def test_matching_ignores_case(self):
        ig = self.ignore("Datamining.PDF\n")
        self.assertTrue(ig.matches(self.rel("school/datamining.pdf")))

    def test_the_adapter_reads_it_from_the_documents_root(self):
        write(self.dir, ".recallignore", "skip.txt\n")
        write(self.dir, "keep.txt", "kept words\n")
        write(self.dir, "sub/skip.txt", "dropped words\n")
        chunks = list(files.Files().chunks(pathlib.Path(self.dir), 1000))
        texts = " ".join(c.text for c in chunks)
        self.assertIn("kept words", texts)
        self.assertNotIn("dropped words", texts)
        self.assertNotIn(".recallignore", texts)


class TestDates(unittest.TestCase):
    """Path beats metadata beats mtime. A folder named 2019 is the user's
    own claim; metadata is what the writing application stamped; mtime is
    the last sync and says so."""

    def when(self, rel, mtime="2024-06-01T00:00:00Z", meta=None):
        return files.occurred_at(rel, mtime, meta)

    def test_an_iso_date_in_the_path_is_the_day(self):
        self.assertEqual(self.when("trips/2018-05-29-chicago.md"),
                         ("2018-05-29T00:00:00Z", "path"))

    def test_a_year_in_a_folder_name_is_that_year(self):
        self.assertEqual(self.when("chicago-2018/notes.txt"),
                         ("2018-01-01T00:00:00Z", "path"))

    def test_a_season_sets_the_month(self):
        self.assertEqual(self.when("school/fall2015/essay.docx"),
                         ("2015-10-01T00:00:00Z", "path"))

    def test_a_month_name_sets_the_month(self):
        self.assertEqual(self.when("invoices/2019/march-rent.pdf"),
                         ("2019-03-01T00:00:00Z", "path"))

    def test_the_latest_year_in_the_path_wins(self):
        self.assertEqual(self.when("archive/2015/reports/2019/q1.pdf")[0],
                         "2019-01-01T00:00:00Z")

    def test_a_phone_number_is_not_a_year(self):
        self.assertEqual(self.when("contacts/6782026543.txt"),
                         ("2024-06-01T00:00:00Z", "mtime"))

    def test_metadata_beats_mtime(self):
        self.assertEqual(self.when("misc/letter.pdf", meta="2016-03-04T10:00:00Z"),
                         ("2016-03-04T10:00:00Z", "metadata"))

    def test_the_path_beats_metadata(self):
        self.assertEqual(self.when("2012/letter.pdf", meta="2016-03-04T10:00:00Z"),
                         ("2012-01-01T00:00:00Z", "path"))

    def test_mtime_is_last_and_says_so(self):
        self.assertEqual(self.when("misc/letter.pdf"),
                         ("2024-06-01T00:00:00Z", "mtime"))


class TestMetadataDate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def test_an_office_creation_date_is_read(self):
        p = write(self.dir, "a.docx", docx_bytes("x", created="2016-03-04T10:00:00Z"))
        self.assertEqual(files.metadata_date(p), "2016-03-04T10:00:00Z")

    def test_an_office_file_without_properties_has_none(self):
        p = write(self.dir, "a.docx", docx_bytes("x"))
        self.assertIsNone(files.metadata_date(p))

    def test_an_epoch_default_is_rejected(self):
        """Broken writers stamp 1980 or 1601. A wrong date is worse than
        none, because the mtime fallback is at least honest."""
        p = write(self.dir, "a.docx", docx_bytes("x", created="1980-01-01T00:00:00Z"))
        self.assertIsNone(files.metadata_date(p))

    def test_plain_text_has_none(self):
        self.assertIsNone(files.metadata_date(write(self.dir, "a.txt", "hi")))

    @unittest.skipUnless(shutil.which("pdfinfo"), "needs poppler")
    def test_a_pdf_creation_date_is_read(self):
        p = write(self.dir, "a.pdf", PDF)
        self.assertEqual(files.metadata_date(p), "2016-03-04T10:00:00Z")


class TestIdentity(unittest.TestCase):
    """The ref follows the content. A moved file keeps its ref and is not
    re-embedded; two copies of one file are one document, indexed once."""

    def setUp(self):
        self.a = tempfile.mkdtemp()
        self.b = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.a)
        self.addCleanup(shutil.rmtree, self.b)

    def refs(self, root):
        return [c.ref for c in files.Files().chunks(pathlib.Path(root), 1000)]

    def test_a_moved_file_keeps_its_ref(self):
        write(self.a, "old/name.txt", "the same words\n")
        write(self.b, "new/place.txt", "the same words\n")
        self.assertEqual(self.refs(self.a), self.refs(self.b))

    def test_different_content_gets_a_different_ref(self):
        write(self.a, "x.txt", "one thing\n")
        write(self.b, "x.txt", "another thing\n")
        self.assertNotEqual(self.refs(self.a), self.refs(self.b))

    def test_a_duplicate_is_indexed_once_under_its_first_path(self):
        write(self.a, "a/first.txt", "the same words\n")
        write(self.a, "b/second.txt", "the same words\n")
        chunks = list(files.Files().chunks(pathlib.Path(self.a), 1000))
        self.assertEqual(len(chunks), 1)
        self.assertIn("a/first.txt", chunks[0].text)
        self.assertEqual(chunks[0].path, "a/first.txt")


class TestChunkDates(unittest.TestCase):
    def test_a_chunk_carries_the_path_date_and_its_confidence(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        write(d, "chicago-2018/notes.txt", "some words\n")
        c = next(iter(files.Files().chunks(pathlib.Path(d), 1000)))
        self.assertEqual(c.occurred_at, "2018-01-01T00:00:00Z")
        self.assertEqual(c.date_confidence, "path")


if __name__ == "__main__":
    unittest.main()


class TestBudget(unittest.TestCase):
    """A 22 MB text file with no blank line in it was one chunk on the
    first real archive. The splitter must never hand the loader a chunk
    over budget, header included, whatever the line lengths."""

    def test_a_paragraph_over_budget_is_split_to_budget(self):
        pieces = files.split("word " * 3000, 1000)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 1000 for p in pieces),
                        [len(p) for p in pieces])

    def test_every_chunk_fits_the_budget_with_its_header(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for width in (4, 60, 200):
            with self.subTest(width=width):
                body = "\n\n".join(["x" * width] * 400)
                write(d, f"a-directory-name-that-is-long/w{width}.txt", body)
                for c in files.Files().chunks(pathlib.Path(d), 500):
                    self.assertLessEqual(len(c.text), 500, c.text[:60])


class TestSamples(unittest.TestCase):
    """The budget calibrates from the samples, and the loader trusts it. A
    sample set of the longest texts measured 1.5 chars per token on one
    archive while an 18 KB stock price table ran at about 1.0, so its
    chunks sat under the character budget and over the token ceiling. The
    samples must carry the densest text as well as the longest."""

    def test_a_short_numeric_table_is_sampled_beside_long_prose(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        prose = ("The quick brown fox jumps over the lazy dog again. " * 400)
        for i in range(12):
            write(d, f"essays/long-{i}.txt", prose + str(i))
        table = "Date,Open,High,Low,Close\n" + "".join(
            f"2018-01-{i%28+1:02d},170.16,172.30,169.26,172.26\n"
            for i in range(120))
        write(d, "archive/prices.csv", table)
        samples = files.Files().samples(pathlib.Path(d))
        self.assertTrue(any("170.16,172.30" in s for s in samples),
                        [len(s) for s in samples])

    def test_a_numeric_spreadsheet_is_sampled_too(self):
        """The density proxy reads text files by their first block. A
        spreadsheet is a zip, so it is extracted first and judged on its
        rows, or a grid of figures never reaches the tokenizer."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        prose = ("The quick brown fox jumps over the lazy dog again. " * 400)
        for i in range(12):
            write(d, f"essays/long-{i}.txt", prose + str(i))
        write(d, "finance/grid.xlsx", xlsx_bytes())
        samples = files.Files().samples(pathlib.Path(d))
        self.assertTrue(any("[sheet: Budget]" in s for s in samples),
                        [len(s) for s in samples])
