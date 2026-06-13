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


# ---------------------------------------------------------------------------
# _find_calibre_duplicates — series-prefix regression (bug fix)
# ---------------------------------------------------------------------------

def _make_pipeline_with_search(search_side_effect):
    """Return a Pipeline-like object whose _calibre.search calls the given side_effect."""
    from libris.pipeline import Pipeline

    mock_calibre = MagicMock()
    mock_calibre.search.side_effect = search_side_effect

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


class TestFindCalibreDuplicatesSeriesPrefix:
    """Duplicate detection must catch series-prefix mismatches in both directions.

    The fix uses a secondary contains-mode search (no '=' prefix) when the
    primary exact search returns nothing, catching:
    - library "Pendragon: The Merchant Of Death" ↔ importing "The Merchant of Death"
    - library "The Merchant of Death" ↔ importing "Pendragon: The Merchant Of Death"
    """

    def test_bare_incoming_matches_prefixed_library_entry(self):
        """Primary exact search for "The Merchant of Death" misses ID 105
        (library has "Pendragon: The Merchant Of Death").  Secondary contains
        search finds it — this is the original bug."""
        calls = []

        def _search(query):
            calls.append(query)
            if '"=' in query:   # primary exact search: calibredb uses title:"=..." syntax
                return []
            return [105]        # secondary contains search finds it

        pipeline = _make_pipeline_with_search(_search)
        ids = pipeline._find_calibre_duplicates("The Merchant of Death", "D.J. MacHale")
        assert ids == [105], "Must detect prefixed library entry via contains fallback"
        assert len(calls) == 2, "Primary + secondary search"

    def test_prefixed_incoming_matches_bare_library_entry(self):
        """Primary exact search for "Pendragon: The Merchant Of Death" misses.
        Secondary strips the prefix and finds the bare-title entry."""
        calls = []

        def _search(query):
            calls.append(query)
            if '"=' in query:   # primary exact search: calibredb uses title:"=..." syntax
                return []
            return [124]

        pipeline = _make_pipeline_with_search(_search)
        ids = pipeline._find_calibre_duplicates("Pendragon: The Merchant Of Death", "D.J. MacHale")
        assert ids == [124]
        assert len(calls) == 2

    def test_primary_match_short_circuits(self):
        """When the primary search finds a match, the secondary is never run."""
        calls = []

        def _search(query):
            calls.append(query)
            return [124]  # always matches

        pipeline = _make_pipeline_with_search(_search)
        ids = pipeline._find_calibre_duplicates("The Merchant of Death", "D.J. MacHale")
        assert ids == [124]
        assert len(calls) == 1, "Only one search when primary matches"

    def test_secondary_always_runs_when_primary_empty(self):
        """Even for titles with no colon, a secondary contains search runs."""
        calls = []

        def _search(query):
            calls.append(query)
            return []

        pipeline = _make_pipeline_with_search(_search)
        pipeline._find_calibre_duplicates("Dune", "Frank Herbert")
        assert len(calls) == 2, "Secondary contains search always runs when primary returns []"
