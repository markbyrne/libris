"""Tests for Pipeline._find_fuzzy_duplicates — near-match dup detection."""

from unittest.mock import MagicMock, patch

import pytest


def _make_pipeline(library_books: list[dict]):
    """Return a Pipeline-like object with a mocked calibre backend."""
    from libris.pipeline import Pipeline

    mock_calibre = MagicMock()
    mock_calibre.list_books.return_value = library_books

    with patch("libris.pipeline.get_calibre", return_value=mock_calibre), \
         patch("libris.pipeline.StateStore"), \
         patch("libris.pipeline.Classifier"):
        cfg = MagicMock()
        cfg.calibre = MagicMock()
        cfg.paths.state_db = "/tmp/fake.db"
        p = Pipeline.__new__(Pipeline)
        p._calibre = mock_calibre
        p.config = cfg
        return p


LIBRARY = [
    {"id": 1, "title": "Project Hail Mary", "authors": ["Andy Weir"], "formats": ["EPUB"]},
    {"id": 2, "title": "The Martian", "authors": ["Andy Weir"], "formats": ["EPUB", "M4B"]},
    {"id": 3, "title": "Dune", "authors": ["Frank Herbert"], "formats": ["EPUB"]},
]


@pytest.mark.parametrize("title,author,expect_ids", [
    # Subtitle variant — should match (> 85%)
    ("Project Hail Mary: A Novel", "Andy Weir", [1]),
    # Exact match — should NOT appear (score == 100 excluded)
    ("Project Hail Mary", "Andy Weir", []),
    # Completely different book — no match
    ("The Name of the Wind", "Patrick Rothfuss", []),
    # Another author's book with similar title fragment
    ("The Martian Chronicles", "Ray Bradbury", []),
    # Close author match on different book
    ("Dune Messiah", "Frank Herbert", []),
])
def test_find_fuzzy_duplicates(title, author, expect_ids):
    pipeline = _make_pipeline(LIBRARY)
    results = pipeline._find_fuzzy_duplicates(title, author)
    result_ids = [r["id"] for r in results]
    assert result_ids == expect_ids, (
        f"_find_fuzzy_duplicates({title!r}, {author!r}) → IDs {result_ids}, expected {expect_ids}"
    )


def test_find_fuzzy_duplicates_empty_title():
    """Empty title returns no results without error."""
    pipeline = _make_pipeline(LIBRARY)
    assert pipeline._find_fuzzy_duplicates("", "Andy Weir") == []


def test_find_fuzzy_duplicates_calibre_error():
    """Backend errors are swallowed — returns [] without crashing."""
    pipeline = _make_pipeline([])
    pipeline._calibre.list_books.side_effect = RuntimeError("calibredb not found")
    assert pipeline._find_fuzzy_duplicates("Project Hail Mary", "Andy Weir") == []


def test_find_fuzzy_duplicates_sorted_by_similarity():
    """Results are sorted highest similarity first."""
    library = [
        {"id": 10, "title": "Project Hail Mary", "authors": ["Andy Weir"], "formats": []},
        {"id": 11, "title": "Project Hail Mary: A Novel", "authors": ["Andy Weir"], "formats": []},
    ]
    pipeline = _make_pipeline(library)
    # Search for a variant — both IDs 10 and 11 may be near-matches; result is ordered
    results = pipeline._find_fuzzy_duplicates("Project Hail Mary — Special Edition", "Andy Weir")
    if len(results) > 1:
        assert results[0]["similarity"] >= results[1]["similarity"]


def test_similarity_field_present():
    """Each result dict includes a 'similarity' key with a numeric value."""
    pipeline = _make_pipeline(LIBRARY)
    results = pipeline._find_fuzzy_duplicates("Project Hail Mary: A Novel", "Andy Weir")
    for r in results:
        assert "similarity" in r
        assert isinstance(r["similarity"], (int, float))
        assert 85 <= r["similarity"] < 100
