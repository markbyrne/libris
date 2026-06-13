"""Tests for the shadow-library ' -- ' filename convention.

Archives such as Anna's Archive name files
``{title} -- {author} -- {year} -- {publisher} -- {md5}.ext``.
parse_double_dash extracts the structured fields; the resolver uses them
as authoritative title/author/year instead of whole-stem heuristics.
"""

from __future__ import annotations

import pytest

from libris.cleaner import clean_query, parse_double_dash
from libris.config import MetadataConfig
from libris.metadata.resolver import resolve_metadata

STEM = "The Vegetarian -- Han Kang -- 2016 -- Hogarth -- 9daef8addc95e0ada7560d13586a1ede"


class TestParseDoubleDash:
    def test_full_convention(self):
        assert parse_double_dash(STEM) == {
            "title": "The Vegetarian",
            "author": "Han Kang",
            "year": 2016,
        }

    def test_without_hash(self):
        assert parse_double_dash("The Vegetarian -- Han Kang -- 2016 -- Hogarth") == {
            "title": "The Vegetarian",
            "author": "Han Kang",
            "year": 2016,
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
        parsed = parse_double_dash(
            "Dune -- Frank Herbert -- " + "a" * 40
        )
        assert parsed == {"title": "Dune", "author": "Frank Herbert", "year": None}
        parsed = parse_double_dash(
            "Dune -- Frank Herbert -- 1965 -- " + "0f" * 32
        )
        assert parsed["year"] == 1965


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
