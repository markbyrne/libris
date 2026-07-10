"""Exception-path coverage for CalibreBackend.merge_authors
(libris/calibre/base.py), extending test_author_merge_client.py's
orchestration tests with the branches that weren't yet exercised:
list_books() raising, a candidate book with no/empty authors, and
set_authors() raising (as opposed to returning False).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from libris.calibre.base import CalibreBackend


def _mock_backend() -> MagicMock:
    mock = MagicMock()
    mock.merge_authors = CalibreBackend.merge_authors.__get__(mock, CalibreBackend)
    return mock


class TestMergeAuthorsListBooksFails:
    def test_list_books_exception_returns_zero(self):
        backend = _mock_backend()
        backend.search.return_value = [1]
        backend.list_books.side_effect = RuntimeError("calibredb list failed")
        assert backend.merge_authors(["D. J. MacHale"], "D.J. MacHale") == 0


class TestMergeAuthorsMissingAuthors:
    def test_candidate_with_no_authors_key_is_skipped(self):
        backend = _mock_backend()
        backend.search.return_value = [1]
        backend.list_books.return_value = [{"id": 1}]  # no "authors" key at all
        assert backend.merge_authors(["D. J. MacHale"], "D.J. MacHale") == 0
        backend.set_authors.assert_not_called()

    def test_candidate_with_empty_authors_list_is_skipped(self):
        backend = _mock_backend()
        backend.search.return_value = [1]
        backend.list_books.return_value = [{"id": 1, "authors": []}]
        assert backend.merge_authors(["D. J. MacHale"], "D.J. MacHale") == 0
        backend.set_authors.assert_not_called()


class TestMergeAuthorsSetAuthorsRaises:
    def test_set_authors_exception_is_swallowed_and_book_skipped(self):
        backend = _mock_backend()
        backend.search.return_value = [1, 2]
        backend.list_books.return_value = [
            {"id": 1, "authors": ["D. J. MacHale"]},
            {"id": 2, "authors": ["D. J. MacHale"]},
        ]

        def _set_authors(book_id, authors):
            if book_id == 1:
                raise RuntimeError("calibredb crashed")
            return True

        backend.set_authors.side_effect = _set_authors
        renamed = backend.merge_authors(["D. J. MacHale"], "D.J. MacHale")
        assert renamed == 1  # book 1's exception must not stop book 2 from renaming
