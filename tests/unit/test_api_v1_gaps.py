"""Coverage-gap tests for libris/web/routes/api_v1.py, filling in what
test_directive_api.py / test_imports_api.py / test_author_merge_api.py don't
already exercise:

  - GET /api/v1/files (never tested at all before this file)
  - The isbn=="" / invalid media_type validator branches on DirectiveIn and
    ImportMetadataIn (only the "junk isbn" and default-omitted-field cases
    were covered previously)
  - _run_import's best-effort `pipeline._store.close()` exception swallow
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


def _headers():
    return {"X-Api-Key": API_KEY}


# ---------------------------------------------------------------------------
# GET /api/v1/files
# ---------------------------------------------------------------------------

class TestGetFile:
    def test_requires_auth(self, client):
        r = client.get("/api/v1/files", params={"filename": "dune.epub"})
        assert r.status_code == 401

    def test_not_found_404(self, client):
        r = client.get(
            "/api/v1/files", params={"filename": "/nowhere/dune.epub"}, headers=_headers()
        )
        assert r.status_code == 404
        assert "No matching file record" in r.json()["detail"]

    def test_found_by_current_path(self, client, tmp_path):
        from libris.config import load_config
        from libris.state import FileRecord, FileState, StateStore

        config_path = next(tmp_path.glob("config.yaml"))
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        book_path = str(tmp_path / "dune.epub")
        record = FileRecord(
            id="abc123",
            original_path=book_path,
            current_path=book_path,
            media_type="ebook",
            state=FileState.IMPORTED,
            matched_title="Dune",
            matched_author="Frank Herbert",
            calibre_book_id=42,
        )
        store.upsert(record)
        store.close()

        r = client.get("/api/v1/files", params={"filename": book_path}, headers=_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "abc123"
        assert body["matched_title"] == "Dune"
        assert body["matched_author"] == "Frank Herbert"
        assert body["calibre_book_id"] == 42
        assert body["state"] == "imported"

    def test_disabled_403(self, tmp_path):
        config_path = _write_config(tmp_path, api_enabled=False)
        app = create_app(config_path)
        c = TestClient(app)
        r = c.get("/api/v1/files", params={"filename": "x.epub"}, headers=_headers())
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# DirectiveIn validator edge branches
# ---------------------------------------------------------------------------

class TestDirectiveValidatorEdges:
    def test_empty_isbn_string_is_treated_as_none(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "book.epub", "title": "Whatever", "isbn": ""},
            headers=_headers(),
        )
        assert r.status_code == 201

    def test_invalid_media_type_422(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "book.epub", "title": "Whatever", "media_type": "audio-cd"},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_valid_media_type_accepted(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "book.epub", "title": "Whatever", "media_type": "audiobook"},
            headers=_headers(),
        )
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# ImportMetadataIn validator edge branches
# ---------------------------------------------------------------------------

class TestImportMetadataValidatorEdges:
    def _make_ebook(self, tmp_path: Path) -> Path:
        landing = tmp_path / "librarr-landing"
        landing.mkdir(exist_ok=True)
        book = landing / "dune.epub"
        book.write_bytes(b"fake epub")
        return book

    def test_empty_isbn_string_is_treated_as_none(self, client, tmp_path):
        book = self._make_ebook(tmp_path)
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(book)], "metadata": {"title": "Dune", "isbn": ""}},
            headers=_headers(),
        )
        assert r.status_code == 202

    def test_invalid_media_type_422(self, client, tmp_path):
        book = self._make_ebook(tmp_path)
        r = client.post(
            "/api/v1/imports",
            json={
                "files": [str(book)],
                "metadata": {"title": "Dune", "media_type": "audio-cd"},
            },
            headers=_headers(),
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# _run_import — pipeline._store.close() best-effort swallow
# ---------------------------------------------------------------------------

class TestRunImportStoreCloseSwallowsException:
    def test_store_close_exception_does_not_propagate(self, client, tmp_path):
        """Regression guard: _run_import's finally block wraps
        pipeline._store.close() in its own try/except specifically because a
        close() failure must never mask (or crash on top of) whatever the
        import itself did. Patch Pipeline so the constructed instance's
        _store.close() raises, and confirm the whole background task still
        completes without the TestClient's synchronous BackgroundTasks
        execution raising.
        """
        from libris.pipeline import Pipeline as RealPipeline

        landing = tmp_path / "librarr-landing"
        landing.mkdir(exist_ok=True)
        book = landing / "dune.epub"
        book.write_bytes(b"fake epub")

        mock_calibre = MagicMock()
        mock_calibre.search.return_value = []
        mock_calibre.add_book.return_value = 1

        def _factory(config):
            p = RealPipeline(config)
            p._calibre = mock_calibre
            close_mock = MagicMock(side_effect=RuntimeError("db already closed"))
            p._store.close = close_mock
            return p

        with patch("libris.pipeline.Pipeline", side_effect=_factory):
            r = client.post(
                "/api/v1/imports",
                json={"files": [str(book)], "metadata": {"title": "Dune"}},
                headers=_headers(),
            )

        # The endpoint itself must still respond 202 — the close() failure is
        # confined to the background task's finally block.
        assert r.status_code == 202
