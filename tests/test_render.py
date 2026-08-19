import unittest

from recall.render import blocks


def texts(bs):
    out = []
    for b in bs:
        if b["type"] == "paragraph":
            out.append("".join(s.get("text", "") for s in b["spans"]))
        else:
            for item in b["items"]:
                out.append("".join(s.get("text", "") for s in item["spans"]))
    return out


class TestParagraphs(unittest.TestCase):
    """The model answers in Markdown. Both front ends escape it and render it
    literally, so bullets show as "*   " and bold as "**text**". Parsing to a
    structure once, here, beats two untested JavaScript renderers, and no HTML
    string ever crosses the wire so nothing can inject markup."""

    def test_plain_text_is_one_paragraph(self):
        bs = blocks("Just a sentence.")
        self.assertEqual(len(bs), 1)
        self.assertEqual(bs[0]["type"], "paragraph")

    def test_a_blank_line_starts_a_new_paragraph(self):
        self.assertEqual(len(blocks("One.\n\nTwo.")), 2)

    def test_empty_input_gives_no_blocks(self):
        self.assertEqual(blocks(""), [])
        self.assertEqual(blocks(None), [])


class TestBold(unittest.TestCase):
    def test_it_marks_a_bold_span(self):
        [b] = blocks("a **bold** word")
        bold = [s for s in b["spans"] if s.get("bold")]
        self.assertEqual([s["text"] for s in bold], ["bold"])

    def test_the_asterisks_are_gone(self):
        [b] = blocks("a **bold** word")
        self.assertNotIn("*", "".join(s.get("text", "") for s in b["spans"]))

    def test_text_around_the_bold_survives(self):
        [b] = blocks("a **bold** word")
        self.assertEqual("".join(s.get("text", "") for s in b["spans"]),
                         "a bold word")

    def test_an_unclosed_asterisk_pair_stays_literal(self):
        """A lone ** is not emphasis. Swallowing it would delete a character
        the user actually wrote."""
        [b] = blocks("2 ** 3 is not bold")
        self.assertIn("**", "".join(s.get("text", "") for s in b["spans"]))


class TestLists(unittest.TestCase):
    def test_star_bullets_become_a_list(self):
        [b] = blocks("*   first\n*   second")
        self.assertEqual(b["type"], "list")
        self.assertEqual(len(b["items"]), 2)

    def test_dash_bullets_become_a_list(self):
        [b] = blocks("- first\n- second")
        self.assertEqual(b["type"], "list")

    def test_the_bullet_marker_is_removed(self):
        [b] = blocks("*   first")
        self.assertEqual(texts([b]), ["first"])

    def test_a_list_item_carries_its_spans_under_a_key(self):
        """Items are {depth, spans}, not a bare span list, because nesting
        has to travel with the text."""
        [b] = blocks("*   first")
        self.assertEqual(set(b["items"][0]), {"depth", "spans"})

    def test_a_list_item_can_hold_bold(self):
        [b] = blocks("*   **Label:** detail")
        bold = [s for s in b["items"][0]["spans"] if s.get("bold")]
        self.assertEqual([s["text"] for s in bold], ["Label:"])

    def test_a_paragraph_before_a_list_stays_separate(self):
        bs = blocks("Intro:\n\n*   one\n*   two")
        self.assertEqual([b["type"] for b in bs], ["paragraph", "list"])


class TestCitations(unittest.TestCase):
    """Citations are inert text today. They carry the only link back to the
    source, and the model writes multi-number groups, so a regex for a single
    number silently misses half of them."""

    def test_a_single_citation_becomes_a_cite_span(self):
        [b] = blocks("A claim [4].")
        cites = [s for s in b["spans"] if s.get("type") == "cite"]
        self.assertEqual(cites[0]["n"], [4])

    def test_a_multi_number_citation_keeps_every_number(self):
        [b] = blocks("A claim [4, 5].")
        cites = [s for s in b["spans"] if s.get("type") == "cite"]
        self.assertEqual(cites[0]["n"], [4, 5])

    def test_a_bracket_that_is_not_a_citation_stays_text(self):
        [b] = blocks("see [the docs] for more")
        self.assertEqual([s for s in b["spans"] if s.get("type") == "cite"], [])
        self.assertIn("[the docs]", "".join(s.get("text", "") for s in b["spans"]))

    def test_a_citation_inside_a_list_item_is_found(self):
        [b] = blocks("*   detail [2]")
        cites = [s for s in b["items"][0]["spans"] if s.get("type") == "cite"]
        self.assertEqual(cites[0]["n"], [2])


class TestSafety(unittest.TestCase):
    """The answer contains the user's own archive text. Nothing here may
    produce markup, so the front end can never be handed HTML to trust."""

    def test_markup_in_the_answer_stays_plain_text(self):
        [b] = blocks("<script>alert(1)</script>")
        joined = "".join(s.get("text", "") for s in b["spans"])
        self.assertIn("<script>", joined)

    def test_no_block_contains_an_html_key(self):
        for b in blocks("**x** and <b>y</b>\n\n*   z [1]"):
            self.assertNotIn("html", b)


if __name__ == "__main__":
    unittest.main()


class TestNestedLists(unittest.TestCase):
    """Measured on a real answer: the model indents sub-points four spaces.
    Flattening them makes a sub-point read as a peer of its parent, which
    changes the meaning of the answer."""

    def test_an_indented_bullet_records_its_depth(self):
        [b] = blocks("*   parent\n    *   child")
        self.assertEqual([i["depth"] for i in b["items"]], [0, 1])

    def test_a_top_level_bullet_is_depth_zero(self):
        [b] = blocks("*   only")
        self.assertEqual(b["items"][0]["depth"], 0)

    def test_the_item_text_is_still_reachable(self):
        [b] = blocks("*   parent\n    *   child")
        self.assertEqual("".join(s.get("text", "") for s in b["items"][1]["spans"]),
                         "child")

    def test_deeper_indentation_does_not_exceed_the_cap(self):
        """Two levels is all the model produces and all the UI needs. A
        runaway depth would let one stray line indent the whole answer."""
        [b] = blocks("*   a\n            *   very deep")
        self.assertLessEqual(max(i["depth"] for i in b["items"]), 1)


class TestHeadings(unittest.TestCase):
    """The model writes section headings as a bold-only line, not with "#".
    Verified on a real answer: zero lines start with "#", two are entirely
    bold. Typing them lets the UI separate sections instead of showing
    another paragraph."""

    def test_a_bold_only_line_becomes_a_heading(self):
        bs = blocks("**Monarch Lacrosse (2023)**")
        self.assertEqual(bs[0]["type"], "heading")

    def test_the_heading_keeps_its_text(self):
        [b] = blocks("**Monarch Lacrosse (2023)**")
        self.assertEqual(b["text"], "Monarch Lacrosse (2023)")

    def test_a_bold_word_inside_a_sentence_is_not_a_heading(self):
        [b] = blocks("a **bold** word here")
        self.assertEqual(b["type"], "paragraph")

    def test_a_heading_separates_the_lists_around_it(self):
        bs = blocks("*   one\n\n**Section**\n\n*   two")
        self.assertEqual([b["type"] for b in bs], ["list", "heading", "list"])
