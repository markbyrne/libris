"""Tests for libris.cleaner — filename noise stripping."""

import pytest

from libris.cleaner import clean_query, extract_isbn


@pytest.mark.parametrize("raw,expected_contains,expected_not_contains", [
    # Extension stripped
    ("Project Hail Mary.epub", ["Project Hail Mary"], [".epub"]),
    # Part markers removed
    ("Eragon Part 1 of 4.mp3", ["Eragon"], ["Part 1"]),
    ("Eragon (Part 2)", ["Eragon"], ["Part 2"]),
    ("Eragon Pt. 3", ["Eragon"], ["Pt."]),
    # Format tags removed
    ("Dune [MP3 320kbps].mp3", ["Dune"], ["MP3", "320kbps"]),
    ("Dune (EPUB Retail)", ["Dune"], ["EPUB", "Retail"]),
    # Year removed
    ("The Martian (2011)", ["Martian"], ["2011"]),
    # Unabridged/Abridged removed
    ("Dune Unabridged", ["Dune"], ["Unabridged"]),
    ("Dune Abridged", ["Dune"], ["Abridged"]),
    # Underscores become spaces
    ("Project_Hail_Mary", ["Project Hail Mary"], ["_"]),
    # Disc/CD/Volume markers
    ("Lord of the Rings Disc 2", ["Lord of the Rings"], ["Disc 2"]),
    ("Lord of the Rings CD 1", ["Lord of the Rings"], ["CD 1"]),
    # Multiple noise patterns combined
    (
        "Project Hail Mary (Unabridged) [MP3 320kbps] Part 1 of 2 (2021).mp3",
        ["Project Hail Mary"],
        ["Unabridged", "MP3", "320kbps", "Part", "2021", ".mp3"],
    ),
])
def test_clean_query(raw, expected_contains, expected_not_contains):
    result = clean_query(raw)
    for token in expected_contains:
        assert token in result, f"Expected {token!r} in cleaned result {result!r}"
    for token in expected_not_contains:
        assert token not in result, f"Did not expect {token!r} in cleaned result {result!r}"


def test_clean_query_lowercase_suffix_stripped():
    # Lowercase single-word suffixes after a dash are noise, not author names
    assert "sometag" not in clean_query("Caliban and the Witch - sometag")
    assert "noisesuffix" not in clean_query("Dune - noisesuffix.epub")
    assert "Caliban" in clean_query("Caliban and the Witch - sometag")


def test_clean_query_real_author_preserved():
    # A Capitalized name after " - " should NOT be stripped
    result = clean_query("Caliban and the Witch - Silvia Federici")
    assert "Silvia" in result or "Federici" in result  # at least part survives


def test_clean_query_empty_input():
    assert clean_query("") == ""


def test_clean_query_all_noise():
    result = clean_query("[MP3 320kbps] (2021) Part 1 of 1.mp3")
    # Should produce something (or empty) without crashing
    assert isinstance(result, str)


@pytest.mark.parametrize("raw,expected_isbn", [
    ("Book 9780593135204.epub", "9780593135204"),
    ("Book ISBN-13: 978-0-593-13520-4.epub", "9780593135204"),
    ("Book 0441013591.epub", "0441013591"),        # ISBN-10
    ("Book with no isbn.epub", None),
    ("Just numbers 12345 here", None),
])
def test_extract_isbn(raw, expected_isbn):
    result = extract_isbn(raw)
    if expected_isbn is None:
        assert result is None
    else:
        assert result is not None
        # Compare digits only (hyphens stripped)
        import re
        assert re.sub(r"[^\dX]", "", result) == re.sub(r"[^\dX]", "", expected_isbn)
