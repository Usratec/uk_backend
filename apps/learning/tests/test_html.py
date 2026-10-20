"""Cleaning the HTML staff write.

What arrives from a rich-text editor cannot be trusted: it may carry a script from a paste, or a
wall of styling from Word. These tests pin what survives and what does not.
"""

import pytest

from apps.learning import html
from apps.learning.html import clean, is_empty
from apps.learning.models import LearningContent


class TestWhatSurvives:
    @pytest.mark.parametrize(
        "html",
        [
            "<p>Stopping distance is thinking plus braking.</p>",
            "<h2>Speed limits</h2>",
            "<ul><li>Look</li><li>Signal</li></ul>",
            "<ol><li>First</li></ol>",
            "<p><strong>Bold</strong> and <em>italic</em>.</p>",
            "<blockquote><p>Rule 126</p></blockquote>",
            "<table><tbody><tr><td>30</td><td>mph</td></tr></tbody></table>",
        ],
    )
    def test_ordinary_formatting_is_kept(self, html):
        assert clean(html) == html

    def test_links_are_kept_and_made_safe(self):
        """rel stops a new tab being able to reach back into the page it came from."""
        cleaned = clean('<p><a href="https://gov.uk">Read more</a></p>')

        assert 'href="https://gov.uk"' in cleaned
        assert "noopener" in cleaned


class TestWhatIsRemoved:
    def test_a_script_is_removed(self):
        cleaned = clean("<p>Fine</p><script>alert('hi')</script>")

        assert "script" not in cleaned
        assert "<p>Fine</p>" in cleaned

    def test_an_event_handler_is_removed(self):
        cleaned = clean('<p onclick="steal()">Text</p>')

        assert "onclick" not in cleaned
        assert "Text" in cleaned

    def test_a_javascript_link_is_removed(self):
        """Keeping the text, losing the trap."""
        cleaned = clean('<a href="javascript:alert(1)">Click</a>')

        assert "javascript" not in cleaned
        assert "Click" in cleaned

    def test_an_iframe_is_removed(self):
        assert "iframe" not in clean('<iframe src="https://evil.test"></iframe><p>Hi</p>')

    def test_pasted_styling_is_dropped_but_the_words_are_kept(self):
        """Pasting from Word brings styling that would fight the site's own design."""
        cleaned = clean('<p style="font-family: Calibri; color: red" class="MsoNormal">Give way</p>')

        assert cleaned == "<p>Give way</p>"

    def test_nothing_in_nothing_out(self):
        assert clean("") == ""
        assert clean(None) == ""


class TestEmptiness:
    @pytest.mark.parametrize("html", ["", None, "<p></p>", "<p>&nbsp;</p>", "<p> </p>", "<p><br></p>"])
    def test_blank_looking_content_counts_as_empty(self, html):
        """An editor leaves markup behind when staff type and delete; that is still empty."""
        assert is_empty(html) is True

    @pytest.mark.parametrize("html", ["<p>Something</p>", "<h2>Heading</h2>"])
    def test_real_content_is_not_empty(self, html):
        assert is_empty(html) is False


@pytest.mark.django_db
class TestCleaningOnSave:
    def test_content_is_cleaned_however_it_was_written(self, subchapter):
        """Cleaned in the model, so an import or the shell is as safe as the admin form."""
        item = LearningContent.objects.create(
            subchapter=subchapter,
            slug="imported",
            title="Imported",
            body_html="<p>Hi</p><script>bad()</script>",
        )

        item.refresh_from_db()
        assert item.body_html == "<p>Hi</p>"

    def test_the_database_never_holds_a_script(self, content):
        content.body_html = '<p onmouseover="x()">Hover</p>'
        content.save()

        content.refresh_from_db()
        assert "onmouseover" not in content.body_html


class TestSplittingOnSigns:
    def test_text_with_no_marker_is_one_piece(self):
        assert html.split_on_signs("<p>Just words.</p>") == [{"html": "<p>Just words.</p>"}]

    def test_a_marker_splits_the_text_around_it(self):
        pieces = html.split_on_signs("<p>Before.</p><p>[signs: give-way | Common signs]</p><p>After.</p>")

        assert pieces == [
            {"html": "<p>Before.</p>"},
            {"codes": ["give-way"], "caption": "Common signs"},
            {"html": "<p>After.</p>"},
        ]

    def test_the_caption_is_optional(self):
        assert html.split_on_signs("<p>[signs: give-way]</p>") == [{"codes": ["give-way"], "caption": ""}]

    def test_several_codes_are_kept_in_order(self):
        pieces = html.split_on_signs("<p>[signs: stop, give-way , no-entry]</p>")

        assert pieces[0]["codes"] == ["stop", "give-way", "no-entry"]

    def test_the_paragraph_around_a_marker_is_taken_with_it(self):
        pieces = html.split_on_signs("<p>Before.</p><p>[signs: give-way]</p>")

        assert pieces == [{"html": "<p>Before.</p>"}, {"codes": ["give-way"], "caption": ""}]

    def test_two_markers_in_a_row_leave_no_blank_text_between_them(self):
        pieces = html.split_on_signs("<p>[signs: stop]</p><p>[signs: give-way]</p>")

        assert [piece["codes"] for piece in pieces] == [["stop"], ["give-way"]]

    def test_an_empty_body_has_no_pieces(self):
        assert html.split_on_signs("") == []

    def test_the_codes_it_asks_for_can_be_listed(self):
        body = "<p>[signs: stop]</p><p>Words.</p><p>[signs: give-way, stop]</p>"

        assert html.sign_codes_in(body) == ["stop", "give-way", "stop"]
