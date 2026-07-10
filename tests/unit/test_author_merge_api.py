"""Tests for POST /api/v1/authors/merge (libris/web/routes/api_v1.py).

Consolidates duplicate author name spellings (e.g. "D. J. MacHale" +
"D.J. MacHale") into one canonical name so Calibre moves those books into a
single author folder. Same fail-closed X-Api-Key auth as the other api_v1
routes (see test_directive_api.py).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from libris.web import create_app

API_KEY = "s3cr3t-test-key"


def _write_config(tmp_path: Path, *, api_enabled: bool = True, api_key: str = API_KEY) -> Path:
    lib = tmp_path / "calibre-library"
    lib.mkdir()
    content = textwrap.dedent(f"""
        watcher:
          incoming_dir: {tmp_path}/incoming
        paths:
          staging_dir: {tmp_path}/staging
          review_dir: {tmp_path}/review
          failed_dir: {tmp_path}/failed
          state_db: {tmp_path}/state.db
        calibre:
          mode: local
          library_path: {lib}
        metadata:
          confidence_threshold: 0.75
          mock_mode: true
          overwrite_existing: true
        output:
          preferred_ebook_format: epub
          preferred_audio_format: m4b
          embed_cover_art: false
        ntfy:
          topic: test
          enabled: false
        api:
          enabled: {str(api_enabled).lower()}
          api_key: "{api_key}"
        log_level: DEBUG
    """)
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def client(tmp_path):
    config_path = _write_config(tmp_path)
    app = create_app(config_path)
    return TestClient(app)


@pytest.fixture
def disabled_client(tmp_path):
    config_path = _write_config(tmp_path, api_enabled=False)
    app = create_app(config_path)
    return TestClient(app)


def _headers():
    return {"X-Api-Key": API_KEY}


def _mock_calibre_client(library: list[dict]):
    """A MagicMock standing in for a CalibreBackend, with search()/list_books()
    driven by *library* (list of {"id", "authors"} dicts) and merge_authors
    wired to the REAL CalibreBackend.merge_authors implementation so these
    tests exercise the actual token-replace/co-author-preserving logic.
    """
    from libris.calibre.base import CalibreBackend

    mock = MagicMock()
    mock.list_books.return_value = [
        {"id": b["id"], "title": b.get("title", ""), "authors": b["authors"]}
        for b in library
    ]

    def _search(query: str) -> list[int]:
        # query looks like: authors:"<name>" — match books whose any author
        # contains the quoted name as a substring (mirrors calibredb's
        # contains-search semantics closely enough for these tests).
        import re as _re

        m = _re.search(r'authors:"([^"]*)"', query)
        name = m.group(1) if m else ""
        return [
            b["id"] for b in library
            if any(name.lower() in a.lower() for a in b["authors"])
        ]

    mock.search.side_effect = _search

    def _set_authors(book_id: int, authors: list[str]) -> bool:
        for b in library:
            if b["id"] == book_id:
                b["authors"] = authors
                mock.list_books.return_value = [
                    {"id": x["id"], "title": x.get("title", ""), "authors": x["authors"]}
                    for x in library
                ]
                return True
        return False

    mock.set_authors.side_effect = _set_authors
    # Bind the real merge_authors implementation to this mock so the
    # endpoint test exercises actual production logic, not a re-implemented
    # test double.
    mock.merge_authors = CalibreBackend.merge_authors.__get__(mock, CalibreBackend)
    return mock


# ---------------------------------------------------------------------------
# Auth / validation
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_key_401(self, client):
        r = client.post(
            "/api/v1/authors/merge",
            json={"from_names": ["D. J. MacHale"], "to_name": "D.J. MacHale"},
        )
        assert r.status_code == 401

    def test_wrong_key_401(self, client):
        r = client.post(
            "/api/v1/authors/merge",
            json={"from_names": ["D. J. MacHale"], "to_name": "D.J. MacHale"},
            headers={"X-Api-Key": "wrong"},
        )
        assert r.status_code == 401

    def test_disabled_403(self, disabled_client):
        r = disabled_client.post(
            "/api/v1/authors/merge",
            json={"from_names": ["D. J. MacHale"], "to_name": "D.J. MacHale"},
            headers=_headers(),
        )
        assert r.status_code == 403


class TestValidation:
    def test_empty_to_name_422(self, client):
        r = client.post(
            "/api/v1/authors/merge",
            json={"from_names": ["D. J. MacHale"], "to_name": ""},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_empty_from_names_422(self, client):
        r = client.post(
            "/api/v1/authors/merge",
            json={"from_names": [], "to_name": "D.J. MacHale"},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_blank_from_names_422(self, client):
        r = client.post(
            "/api/v1/authors/merge",
            json={"from_names": ["   "], "to_name": "D.J. MacHale"},
            headers=_headers(),
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestMerge:
    def test_happy_path_renames_matching_books(self, client):
        library = [
            {"id": 1, "title": "The Merchant of Death", "authors": ["D. J. MacHale"]},
            {"id": 2, "title": "The Lost City of Faar", "authors": ["D.J. MacHale"]},
            {"id": 3, "title": "Dune", "authors": ["Frank Herbert"]},
        ]
        mock_calibre = _mock_calibre_client(library)

        with patch("libris.calibre.get_calibre", return_value=mock_calibre):
            r = client.post(
                "/api/v1/authors/merge",
                json={
                    "from_names": ["D. J. MacHale", "D.J. MacHale"],
                    "to_name": "D.J. MacHale",
                },
                headers=_headers(),
            )

        assert r.status_code == 200
        body = r.json()
        assert body["to_name"] == "D.J. MacHale"
        # Only book 1 actually changes; book 2 already has the canonical spelling.
        assert body["renamed"] == 1
        assert library[0]["authors"] == ["D.J. MacHale"]
        assert library[1]["authors"] == ["D.J. MacHale"]
        assert library[2]["authors"] == ["Frank Herbert"]

        # calibredb set_metadata --field authors:"..." was invoked with the
        # canonical name for the book that changed.
        mock_calibre.set_authors.assert_called_once_with(1, ["D.J. MacHale"])

    def test_multi_author_book_preserves_coauthors(self, client):
        library = [
            {"id": 5, "title": "Anthology", "authors": ["A. Author", "D. J. MacHale"]},
        ]
        mock_calibre = _mock_calibre_client(library)

        with patch("libris.calibre.get_calibre", return_value=mock_calibre):
            r = client.post(
                "/api/v1/authors/merge",
                json={"from_names": ["D. J. MacHale"], "to_name": "D.J. MacHale"},
                headers=_headers(),
            )

        assert r.status_code == 200
        assert r.json()["renamed"] == 1
        assert library[0]["authors"] == ["A. Author", "D.J. MacHale"]
        mock_calibre.set_authors.assert_called_once_with(5, ["A. Author", "D.J. MacHale"])

    def test_idempotent_second_call_is_noop(self, client):
        library = [
            {"id": 1, "title": "The Merchant of Death", "authors": ["D. J. MacHale"]},
        ]
        mock_calibre = _mock_calibre_client(library)
        payload = {
            "from_names": ["D. J. MacHale", "D.J. MacHale"],
            "to_name": "D.J. MacHale",
        }

        with patch("libris.calibre.get_calibre", return_value=mock_calibre):
            r1 = client.post("/api/v1/authors/merge", json=payload, headers=_headers())
            assert r1.status_code == 200
            assert r1.json()["renamed"] == 1
            assert library[0]["authors"] == ["D.J. MacHale"]

            mock_calibre.set_authors.reset_mock()
            r2 = client.post("/api/v1/authors/merge", json=payload, headers=_headers())

        assert r2.status_code == 200
        assert r2.json()["renamed"] == 0
        mock_calibre.set_authors.assert_not_called()

    def test_no_matching_books_renamed_zero(self, client):
        library = [
            {"id": 3, "title": "Dune", "authors": ["Frank Herbert"]},
        ]
        mock_calibre = _mock_calibre_client(library)

        with patch("libris.calibre.get_calibre", return_value=mock_calibre):
            r = client.post(
                "/api/v1/authors/merge",
                json={"from_names": ["Nobody Here"], "to_name": "Somebody Else"},
                headers=_headers(),
            )

        assert r.status_code == 200
        assert r.json() == {"renamed": 0, "to_name": "Somebody Else"}
