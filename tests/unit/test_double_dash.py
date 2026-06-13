"""Tests for the shadow-library ' -- ' filename convention.

Archives such as Anna's Archive name files
``{title} -- {author} -- {year} -- {publisher} -- {md5}.ext``.
parse_double_dash extracts structured fields; the resolver uses them as
authoritative title/author/year/isbn/series instead of whole-stem heuristics.

Five improvements tested here (in addition to the original contract):
1. Underscore-as-dot: AA encodes "D.J." as "D_ J_" — reversed on output.
2. Narrator split: "Author, Narrator" in the author field → author + narrator.
3. isbn13/isbn10 field: "isbn13 9781416914204" → isbn key in result.
4. Series+ordinal from title: "Series_ Book N, Title" → series/series_index/title.
5. Attribution strings: "Anna's Archive" field consumed silently.
"""

from __future__ import annotations

import pytest

from libris.cleaner import clean_query, parse_double_dash
from libris.config import MetadataConfig
from libris.metadata.resolver import resolve_metadata

STEM = "The Vegetarian -- Han Kang -- 2016 -- Hogarth -- 9daef8addc95e0ada7560d13586a1ede"

# Expected "null" values for keys added in this sprint — saves repetition below.
_NULLS = {"isbn": None, "series": None, "series_index": None, "narrator": None}

# The real-world example that motivated all five improvements.
PENDRAGON_STEM = (
    "Pendragon_ Book ten, The soldiers of Halla"
    " -- D_ J_ MacHale, William Dufris"
    " -- 2011 -- Aladdin; 1"
    " -- isbn13 9781416914204"
    " -- 3fbea2dba0b00fb15c6bd81f7183cfa5"
    " -- Anna's Archive"
)


# ---------------------------------------------------------------------------
# Original contract (keys unchanged; extra keys now always present)
# ---------------------------------------------------------------------------

class TestParseDoubleDash:
    def test_full_convention(self):
        assert parse_double_dash(STEM) == {
            "title": "The Vegetarian",
            "author": "Han Kang",
            "year": 2016,
            **_NULLS,
        }

    def test_without_hash(self):
        assert parse_double_dash("The Vegetarian -- Han Kang -- 2016 -- Hogarth") == {
            "title": "The Vegetarian",
            "author": "Han Kang",
            "year": 2016,
            **_NULLS,
        }

    def test_without_year(self):
        parsed = parse_double_dash(
            "The Vegetarian -- Han Kang -- Hogarth -- 9daef8addc95e0ada7560d13586a1ede"
        )
        assert parsed["title"] == "The Vegetarian"
        assert parsed["author"] == "Han Kang"
        assert parsed["year"] is None

    def test_title_author_year_only(self):
        assert parse_double_dash("Dune -- Frank Herbert -- 1965") == {
            "title": "Dune",
            "author": "Frank Herbert",
            "year": 1965,
            **_NULLS,
        }

    @pytest.mark.parametrize("stem", [
        "Title - Author",                    # single dash — not the convention
        "Plain Title",                       # no separators at all
        "Title -- Author",                   # one separator is too ambiguous
        "Inheritance Cycle 2 - Eldest",      # series convention, single dash
    ])
    def test_non_convention_returns_none(self, stem):
        assert parse_double_dash(stem) is None

    def test_sha1_and_sha256_hashes_consumed(self):
        parsed = parse_double_dash("Dune -- Frank Herbert -- " + "a" * 40)
        assert parsed is not None
        assert parsed["title"] == "Dune"
        assert parsed["author"] == "Frank Herbert"
        assert parsed["year"] is None
        parsed = parse_double_dash("Dune -- Frank Herbert -- 1965 -- " + "0f" * 32)
        assert parsed is not None
        assert parsed["year"] == 1965


# ---------------------------------------------------------------------------
# Improvement 1 — Underscore-as-dot in author names
# ---------------------------------------------------------------------------

class TestUnderscoreAsDot:
    def test_initials_restored(self):
        parsed = parse_double_dash("Some Title -- D_ J_ MacHale -- 2011 -- Publisher")
        assert parsed is not None
        assert parsed["author"] == "D.J. MacHale"

    def test_rowling_style(self):
        parsed = parse_double_dash("Some Title -- J_ K_ Rowling -- 2000 -- Bloomsbury")
        assert parsed is not None
        assert parsed["author"] == "J.K. Rowling"

    def test_plain_author_unchanged(self):
        parsed = parse_double_dash("Some Title -- Stephen King -- 1980 -- Publisher")
        assert parsed is not None
        assert parsed["author"] == "Stephen King"


# ---------------------------------------------------------------------------
# Improvement 2 — Narrator split from author field
# ---------------------------------------------------------------------------

class TestNarratorSplit:
    def test_narrator_extracted(self):
        parsed = parse_double_dash(
            "Some Title -- D_ J_ MacHale, William Dufris -- 2011 -- Publisher"
        )
        assert parsed is not None
        assert parsed["author"] == "D.J. MacHale"
        assert parsed["narrator"] == "William Dufris"

    def test_no_narrator_when_no_comma(self):
        parsed = parse_double_dash("Some Title -- Stephen King -- 1980 -- Publisher")
        assert parsed is not None
        assert parsed["narrator"] is None

    def test_narrator_after_dot_restoration(self):
        # Dot restoration happens before the comma split
        parsed = parse_double_dash("Some Title -- J_ K_ Rowling, Someone Else -- 2000 -- Publisher")
        assert parsed is not None
        assert parsed["author"] == "J.K. Rowling"
        assert parsed["narrator"] == "Someone Else"


# ---------------------------------------------------------------------------
# Improvement 3 — isbn13 / isbn10 field extraction
# ---------------------------------------------------------------------------

class TestIsbnField:
    def test_isbn13_extracted(self):
        parsed = parse_double_dash(
            "Some Title -- Author Name -- 2011 -- Publisher -- isbn13 9781416914204 -- " + "a" * 32
        )
        assert parsed is not None
        assert parsed["isbn"] == "9781416914204"

    def test_isbn10_extracted(self):
        parsed = parse_double_dash(
            "Some Title -- Author Name -- 2011 -- Publisher -- isbn10 0689877811"
        )
        assert parsed is not None
        assert parsed["isbn"] == "0689877811"

    def test_isbn_with_hyphens(self):
        parsed = parse_double_dash(
            "Some Title -- Author Name -- 2011 -- Publisher -- isbn13 978-1-4169-1420-4"
        )
        assert parsed is not None
        assert parsed["isbn"] == "9781416914204"

    def test_no_isbn_when_absent(self):
        parsed = parse_double_dash("Some Title -- Author Name -- 2011 -- Publisher -- " + "a" * 32)
        assert parsed is not None
        assert parsed["isbn"] is None

    def test_isbn_field_not_in_title_or_author(self):
        parsed = parse_double_dash(
            "Some Title -- Author Name -- 2011 -- Publisher -- isbn13 9781416914204"
        )
        assert parsed is not None
        assert parsed["title"] == "Some Title"
        assert parsed["author"] == "Author Name"


# ---------------------------------------------------------------------------
# Improvement 4 — Series + ordinal extraction from title field
# ---------------------------------------------------------------------------

class TestSeriesExtraction:
    def test_word_ordinal(self):
        parsed = parse_double_dash(
            "Pendragon_ Book ten, The soldiers of Halla -- D_ J_ MacHale -- 2011 -- Publisher"
        )
        assert parsed is not None
        assert parsed["series"] == "Pendragon"
        assert parsed["series_index"] == 10
        assert parsed["title"] == "The soldiers of Halla"

    def test_digit_ordinal(self):
        parsed = parse_double_dash(
            "Pendragon_ Book 1, The Merchant of Death -- D_ J_ MacHale -- 2002 -- Publisher"
        )
        assert parsed is not None
        assert parsed["series"] == "Pendragon"
        assert parsed["series_index"] == 1
        assert parsed["title"] == "The Merchant of Death"

    def test_no_series_when_absent(self):
        parsed = parse_double_dash("The Vegetarian -- Han Kang -- 2016 -- Hogarth")
        assert parsed is not None
        assert parsed["series"] is None
        assert parsed["series_index"] is None

    def test_all_ordinals_one_to_ten(self):
        ordinals = ["one", "two", "three", "four", "five",
                    "six", "seven", "eight", "nine", "ten"]
        for i, word in enumerate(ordinals, start=1):
            parsed = parse_double_dash(
                f"Series_ Book {word}, Title Here -- Author -- 2000 -- Publisher"
            )
            assert parsed is not None, f"Failed for '{word}'"
            assert parsed["series_index"] == i, f"Expected {i} for '{word}'"


# ---------------------------------------------------------------------------
# Improvement 5 — Attribution strings consumed silently
# ---------------------------------------------------------------------------

class TestAttributionStrings:
    @pytest.mark.parametrize("attribution", [
        "Anna's Archive",
        "Z-Library",
        "Zlibrary",
        "Libgen",
        "Library Genesis",
        "Sci-Hub",
    ])
    def test_attribution_consumed(self, attribution):
        parsed = parse_double_dash(
            f"Some Title -- Author Name -- 2011 -- Publisher -- {attribution}"
        )
        assert parsed is not None
        assert parsed["title"] == "Some Title"
        assert parsed["author"] == "Author Name"


# ---------------------------------------------------------------------------
# Integration: the full Pendragon example that motivated all five improvements
# ---------------------------------------------------------------------------

class TestPendragonIntegration:
    def test_all_fields_extracted(self):
        parsed = parse_double_dash(PENDRAGON_STEM)
        assert parsed is not None
        assert parsed["title"] == "The soldiers of Halla"
        assert parsed["author"] == "D.J. MacHale"
        assert parsed["narrator"] == "William Dufris"
        assert parsed["year"] == 2011
        assert parsed["isbn"] == "9781416914204"
        assert parsed["series"] == "Pendragon"
        assert parsed["series_index"] == 10

    def test_attribution_and_hash_not_in_result(self):
        parsed = parse_double_dash(PENDRAGON_STEM)
        assert parsed is not None
        # Anna's Archive and the MD5 hash must not appear anywhere
        result_str = str(parsed)
        assert "Anna" not in result_str
        assert "3fbea2" not in result_str


# ---------------------------------------------------------------------------
# Existing heuristic cleaner tests (regression)
# ---------------------------------------------------------------------------

class TestCleanQueryFallback:
    """Even outside the structured path, hashes and -- separators are noise."""

    def test_hash_stripped(self):
        assert "9daef8" not in clean_query(STEM)

    def test_double_dash_collapsed(self):
        assert "--" not in clean_query(STEM)

    def test_user_reported_filename(self):
        result = clean_query(STEM)
        assert "The Vegetarian" in result
        assert "Han Kang" in result
        assert "2016" not in result


class TestResolverUsesStructuredFields:
    def _resolve(self, filename: str):
        config = MetadataConfig(confidence_threshold=0.75, mock_mode=True)
        return resolve_metadata(filename, config)

    def test_query_built_from_fields(self):
        result = self._resolve(STEM + ".epub")
        assert result.query.clean_title == "The Vegetarian"
        assert result.query.author_hint == "Han Kang"
        assert result.query.year_hint == 2016

    def test_single_dash_path_unaffected(self):
        result = self._resolve("Caliban and the Witch - Silvia Federici.epub")
        assert result.query.author_hint == "Silvia Federici"
        assert "Caliban" in result.query.clean_title

    def test_pendragon_isbn_wired_into_query(self):
        result = self._resolve(PENDRAGON_STEM + ".epub")
        assert result.query.isbn == "9781416914204"
        assert result.query.series_hint == "Pendragon"
        assert result.query.series_index_hint == 10.0
        assert result.query.author_hint == "D.J. MacHale"
        assert "soldiers" in result.query.clean_title.lower()
