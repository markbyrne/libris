"""Mirror of librarr's LIBRIS_GRAMMAR_VECTORS (tests/test_libris_naming.py).

SHARED CROSS-REPO CONTRACT: these exact vectors are mirrored from librarr's
test suite (librarr/tests/test_libris_naming.py). librarr's
build_libris_structured_filename emits libris's " -- " structured filename
convention; this test verifies libris's parse_double_dash round-trips each
vector's expected_filename back into the same title/author/year/isbn.
Change the vectors in BOTH repos or not at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libris.cleaner import parse_double_dash

# ---------------------------------------------------------------------------
# SHARED CROSS-REPO CONTRACT — copied verbatim from
# librarr/tests/test_libris_naming.py::LIBRIS_GRAMMAR_VECTORS.
# ---------------------------------------------------------------------------
LIBRIS_GRAMMAR_VECTORS = [
    dict(
        title="Mistborn", author="Brandon Sanderson", year=2006,
        isbn="9780765311788", ext=".epub",
        expected_filename="Mistborn -- Brandon Sanderson -- 2006 -- isbn13 9780765311788.epub",
    ),
    dict(
        title="Dune", author="Frank Herbert", year=1965,
        isbn=None, ext=".epub",
        expected_filename="Dune -- Frank Herbert -- 1965.epub",
    ),
    dict(
        title="The Vegetarian", author="Han Kang", year=None,
        isbn=None, ext=".epub",
        expected_filename="The Vegetarian -- Han Kang -- " + "0" * 32 + ".epub",
    ),
    dict(
        title="Solo Book", author=None, year=2020,
        isbn="9780765311788", ext=".epub",
        expected_filename="Solo Book -- " + "0" * 32 + " -- 2020 -- isbn13 9780765311788.epub",
    ),
    dict(
        title="Weird -- Title: Sub", author="A B", year=2011,
        isbn=None, ext=".epub",
        expected_filename="Weird - Title - Sub -- A B -- 2011.epub",
    ),
    dict(
        title='Book "Title" Here', author="Some|Author", year=None,
        isbn=None, ext=".epub",
        expected_filename="Book _Title_ Here -- Some_Author -- " + "0" * 32 + ".epub",
    ),
    dict(
        title="Project Hail Mary", author="Andy Weir", year=2021,
        isbn="9780593135204", ext=".m4b",
        expected_filename="Project Hail Mary -- Andy Weir -- 2021 -- isbn13 9780593135204.m4b",
    ),
    dict(
        title="Sōseki Kokoro", author="Natsume Sōseki", year=1914,
        isbn=None, ext=".epub",
        expected_filename="Sōseki Kokoro -- Natsume Sōseki -- 1914.epub",
    ),
]

# The "0"*32 padding is librarr's placeholder for a missing author/title
# field — it's a hex-hash-shaped field, so parse_double_dash silently
# consumes it (see _HEX_HASH_FIELD) rather than reporting it as author.
_PAD = "0" * 32


@pytest.mark.parametrize(
    "vector", LIBRIS_GRAMMAR_VECTORS, ids=[v["title"] for v in LIBRIS_GRAMMAR_VECTORS]
)
def test_parse_double_dash_round_trips_grammar_vectors(vector):
    """parse_double_dash must recover the title/author/year/isbn FIELDS that
    are actually embedded in the structured filename librarr emits.

    Two vectors (illegal-char title/author) are sanitised by librarr's
    build_libris_structured_filename BEFORE embedding — e.g. ':' -> '-',
    '"'/'|' -> '_' — so the ground truth for those is the sanitised field
    text taken from expected_filename itself, not vector["title"]/["author"].
    This still exercises the real cross-repo contract: whatever librarr
    writes into the " -- " fields, libris's parser must read back exactly.
    """
    stem = Path(vector["expected_filename"]).stem
    result = parse_double_dash(stem)
    assert result is not None, f"parse_double_dash returned None for stem: {stem!r}"

    fields = stem.split(" -- ")
    expected_title = fields[0]
    expected_author = fields[1] if len(fields) > 1 and fields[1] != _PAD else None

    assert result["title"] == expected_title
    assert result["author"] == expected_author
    assert result["year"] == vector["year"]
    assert result["isbn"] == vector["isbn"]
