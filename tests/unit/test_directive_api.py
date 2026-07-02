"""Tests for the directive API (libris/web/routes/api_v1.py).

The API is off by default (fail closed): config.api.enabled must be True AND
config.api.api_key must be non-empty, and every request must carry a matching
X-Api-Key header.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
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


@pytest.fixture
def no_key_client(tmp_path):
    config_path = _write_config(tmp_path, api_key="")
    app = create_app(config_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_wrong_key_401(self, client):
        r = client.get("/api/v1/ping", headers={"X-Api-Key": "wrong"})
        assert r.status_code == 401

    def test_missing_key_401(self, client):
        r = client.get("/api/v1/ping")
        assert r.status_code == 401

    def test_disabled_rejected(self, disabled_client):
        r = disabled_client.get("/api/v1/ping", headers={"X-Api-Key": API_KEY})
        assert r.status_code == 403

    def test_no_key_configured_rejected_even_with_enabled(self, no_key_client):
        r = no_key_client.get("/api/v1/ping", headers={"X-Api-Key": ""})
        assert r.status_code in (401, 403)

    def test_correct_key_ok(self, client):
        r = client.get("/api/v1/ping", headers={"X-Api-Key": API_KEY})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

class TestPing:
    def test_ping_shape(self, client):
        r = client.get("/api/v1/ping", headers={"X-Api-Key": API_KEY})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "version" in body


# ---------------------------------------------------------------------------
# POST /api/v1/directives
# ---------------------------------------------------------------------------

class TestCreateDirective:
    def _headers(self):
        return {"X-Api-Key": API_KEY}

    def test_happy_path_201(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "dune.epub", "title": "Dune", "author": "Frank Herbert"},
            headers=self._headers(),
        )
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "registered"
        assert "id" in body

    def test_persisted_and_findable(self, client, tmp_path):
        from libris.config import load_config
        from libris.state import StateStore

        r = client.post(
            "/api/v1/directives",
            json={"filename": "dune.epub", "title": "Dune", "author": "Frank Herbert",
                  "isbn": "9780441013593", "year": 1965},
            headers=self._headers(),
        )
        assert r.status_code == 201
        directive_id = r.json()["id"]

        config_path = next(tmp_path.glob("config.yaml"))
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        row = store.find_directive("dune.epub")
        store.close()
        assert row is not None
        assert row["id"] == directive_id
        import json as _json
        meta = _json.loads(row["metadata_json"])
        assert meta["title"] == "Dune"
        assert meta["authors"] == ["Frank Herbert"]
        assert meta["isbn_13"] == "9780441013593"
        assert meta["published_year"] == 1965

    def test_missing_title_422(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "book.epub", "title": ""},
            headers=self._headers(),
        )
        assert r.status_code == 422

    def test_path_in_filename_422(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "../etc/passwd", "title": "Whatever"},
            headers=self._headers(),
        )
        assert r.status_code == 422

    def test_path_separator_in_filename_422(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "sub/dir/book.epub", "title": "Whatever"},
            headers=self._headers(),
        )
        assert r.status_code == 422

    def test_junk_isbn_422(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "book.epub", "title": "Whatever", "isbn": "not-an-isbn!"},
            headers=self._headers(),
        )
        assert r.status_code == 422

    def test_duplicate_supersedes(self, client, tmp_path):
        from libris.config import load_config
        from libris.state import StateStore

        client.post(
            "/api/v1/directives",
            json={"filename": "dune.epub", "title": "Old Title"},
            headers=self._headers(),
        )
        r2 = client.post(
            "/api/v1/directives",
            json={"filename": "dune.epub", "title": "New Title"},
            headers=self._headers(),
        )
        assert r2.status_code == 201

        config_path = next(tmp_path.glob("config.yaml"))
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        row = store.find_directive("dune.epub")
        store.close()
        import json as _json
        assert _json.loads(row["metadata_json"])["title"] == "New Title"

    def test_requires_auth(self, client):
        r = client.post(
            "/api/v1/directives",
            json={"filename": "book.epub", "title": "Whatever"},
        )
        assert r.status_code == 401
