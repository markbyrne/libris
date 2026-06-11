"""Tests for libris.cleaner — filename noise stripping."""

import pytest

from libris.cleaner import clean_query, extract_isbn, extract_part, is_chaff, strip_part_marker


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



# ---------------------------------------------------------------------------
# extract_part — multi-part detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # ── Existing keyword-based patterns ──────────────────────────────────
    ("Brisingr (part 1 of 3)",       (1, 3)),
    ("Brisingr (part 1.3)",          (1, 3)),
    ("Brisingr (part 1/3)",          (1, 3)),
    ("Brisingr Disc 1 of 2",         (1, 2)),
    ("Brisingr Part 1",              (1, None)),
    ("Eragon",                       (None, None)),
    # ── NEW: bare "N of M" / "N/M" in parens (no keyword) ────────────────
    ("Book Title (1 of 3)",          (1, 3)),
    ("Book Title (2 of 3)",          (2, 3)),
    ("Book Title (1/3)",             (1, 3)),
    ("Book Title (2/3)",             (2, 3)),
    # ── NEW: bare trailing number in parens (no keyword) ─────────────────
    ("Book Title (1)",               (1, None)),
    ("Book Title (2)",               (2, None)),
    ("Book Title (12)",              (12, None)),
    # ── Year false-positives must NOT match ──────────────────────────────
    ("Some Book (2021)",             (None, None)),   # 4-digit year
    ("Title Part One (2021)",        (None, None)),   # year mid-stem
    ("A Book (999) Extra Text",      (None, None)),   # not end-anchored
    # ── NEW: bare trailing "-NN-NN" pair (issue #59) ─────────────────────
    ("Title-01-46",                  (1, 46)),
    ("Title-46-46",                  (46, 46)),
    ("Title-2-12",                   (2, 12)),        # unpadded
    ("Merchant of Death-01-46",      (1, 46)),
    # plausibility guards — must NOT match
    ("Title-46-01",                  (None, None)),   # part > total
    ("Title-1-1",                    (None, None)),   # total < 2
    ("Show-2024-12-25",              (None, None)),   # trailing date
    ("Catch-22-01-46",               (None, None)),   # digit before pair (conservative)
    ("Title-01-46 Extra",            (None, None)),   # not end-anchored
])
def test_extract_part(raw, expected):
    assert extract_part(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Brisingr (part 1 of 3)",        "Brisingr"),
    ("Name of the Wind Disc 1 of 2",  "Name of the Wind"),
    # NEW bare patterns
    ("Book Title (1)",                "Book Title"),
    ("Book Title (1 of 3)",           "Book Title"),
    ("Book Title (2/3)",              "Book Title"),
    ("Eragon",                        "Eragon"),
    # NEW: bare trailing "-NN-NN" pair (issue #59)
    ("Title-01-46",                   "Title"),
    ("Merchant of Death-13-46",       "Merchant of Death"),
    # implausible pair is part of the title — must survive
    ("Title-46-01",                   "Title-46-01"),
])
def test_strip_part_marker(raw, expected):
    assert strip_part_marker(raw) == expected


# ---------------------------------------------------------------------------
# is_chaff
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename,expected", [
    # ── Should be flagged as chaff ────────────────────────────────────────
    ("Read Me!.epub",                 True),
    ("readme.epub",                   True),
    ("README.epub",                   True),
    ("Downloaded from piracy.epub",   True),
    ("www.example.com.epub",          True),
    ("license.epub",                  True),
    ("sample.m4b",                    True),
    ("a.epub",                        True),   # stem too short
    ("1.m4b",                         True),   # stem too short
    ("cover.jpg",                     True),   # chaff extension
    ("nfo.epub",                      True),   # exact stem match
    ("info.txt",                      True),   # txt extension
    # ── Should NOT be flagged ─────────────────────────────────────────────
    ("Project Hail Mary.epub",        False),
    ("Dune.m4b",                      False),
    ("The Martian.mp3",               False),
    ("Blood River.epub",              False),
    ("readme-notes.epub",             False),   # stem starts with readme but longer
])
def test_is_chaff(filename, expected):
    assert is_chaff(filename) == expected


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
