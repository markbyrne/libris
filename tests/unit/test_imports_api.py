"""Tests for the direct-import API (POST /api/v1/imports in
libris/web/routes/api_v1.py).

Phase 2 of the API-driven-import plan: a caller (e.g. Librarr) that already
has files on disk (in Libris's view) and already knows the correct metadata
posts them here instead of dropping them into incoming_dir for the watcher
to discover blind. Always 202 immediately; the import runs in a
BackgroundTasks task after the response — TestClient runs BackgroundTasks
synchronously, so end-to-end assertions are straightforward here.
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


def _make_ebook(tmp_path: Path, name: str = "dune.epub") -> Path:
    landing = tmp_path / "librarr-landing"
    landing.mkdir(exist_ok=True)
    book = landing / name
    book.write_bytes(b"fake epub")
    return book


def _mock_calibre_pipeline():
    """Patch libris.pipeline.Pipeline (imported locally inside
    _run_import, so it must be patched at its definition site) so the
    constructed instance's ._calibre is a MagicMock — mirrors
    test_import_file_list.py's pipeline._calibre = MagicMock() pattern,
    applied post-construction since the endpoint builds its own Pipeline
    internally.
    """
    from libris.pipeline import Pipeline as RealPipeline

    mock_calibre = MagicMock()
    mock_calibre.search.return_value = []
    mock_calibre.add_book.return_value = 42

    def _factory(config):
        p = RealPipeline(config)
        p._calibre = mock_calibre
        return p

    return patch("libris.pipeline.Pipeline", side_effect=_factory), mock_calibre


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_missing_key_401(self, client, tmp_path):
        book = _make_ebook(tmp_path)
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(book)], "metadata": {"title": "Dune"}},
        )
        assert r.status_code == 401

    def test_wrong_key_401(self, client, tmp_path):
        book = _make_ebook(tmp_path)
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(book)], "metadata": {"title": "Dune"}},
            headers={"X-Api-Key": "wrong"},
        )
        assert r.status_code == 401

    def test_disabled_403(self, disabled_client, tmp_path):
        book = _make_ebook(tmp_path)
        r = disabled_client.post(
            "/api/v1/imports",
            json={"files": [str(book)], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# 422 validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_relative_path_422(self, client):
        r = client.post(
            "/api/v1/imports",
            json={"files": ["relative/dune.epub"], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_missing_file_422(self, client, tmp_path):
        missing = tmp_path / "nope.epub"
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(missing)], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_directory_422(self, client, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(d)], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_symlink_422(self, client, tmp_path):
        book = _make_ebook(tmp_path)
        link = tmp_path / "librarr-landing" / "link.epub"
        link.symlink_to(book)
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(link)], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_bad_extension_422(self, client, tmp_path):
        landing = tmp_path / "librarr-landing"
        landing.mkdir(exist_ok=True)
        junk = landing / "notes.txt.exe"
        junk.write_bytes(b"junk")
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(junk)], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_mixed_ebook_and_audio_list_422(self, client, tmp_path):
        landing = tmp_path / "librarr-landing"
        landing.mkdir(exist_ok=True)
        ebook = landing / "book.epub"
        audio = landing / "book part 1.m4b"
        ebook.write_bytes(b"fake epub")
        audio.write_bytes(b"fake audio")
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(ebook), str(audio)], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_missing_title_422(self, client, tmp_path):
        book = _make_ebook(tmp_path)
        r = client.post(
            "/api/v1/imports",
            json={"files": [str(book)], "metadata": {"title": ""}},
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_bad_isbn_422(self, client, tmp_path):
        book = _make_ebook(tmp_path)
        r = client.post(
            "/api/v1/imports",
            json={
                "files": [str(book)],
                "metadata": {"title": "Dune", "isbn": "not-an-isbn!"},
            },
            headers=_headers(),
        )
        assert r.status_code == 422

    def test_empty_files_422(self, client):
        r = client.post(
            "/api/v1/imports",
            json={"files": [], "metadata": {"title": "Dune"}},
            headers=_headers(),
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# 202 + record/directive bookkeeping
# ---------------------------------------------------------------------------

class TestAccepted:
    def test_202_and_record_and_directive_written(self, client, tmp_path):
        from libris.config import load_config
        from libris.state import StateStore

        book = _make_ebook(tmp_path)
        patcher, mock_calibre = _mock_calibre_pipeline()
        with patcher:
            r = client.post(
                "/api/v1/imports",
                json={
                    "files": [str(book)],
                    "metadata": {"title": "Dune", "author": "Frank Herbert"},
                    "source": "librarr",
                    "confidence": 0.95,
                },
                headers=_headers(),
            )

        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "accepted"
        assert "id" in body

        config_path = next(tmp_path.glob("config.yaml"))
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(body["id"])
        store.close()
        assert record is not None


# ---------------------------------------------------------------------------
# End-to-end: single ebook import with posted metadata
# ---------------------------------------------------------------------------

class TestEndToEndImport:
    def test_single_ebook_end_to_end(self, client, tmp_path):
        from libris.config import load_config
        from libris.state import FileState, StateStore

        book = _make_ebook(tmp_path)
        patcher, mock_calibre = _mock_calibre_pipeline()
        with patcher, patch("libris.pipeline.resolve_metadata") as mock_resolve:
            r = client.post(
                "/api/v1/imports",
                json={
                    "files": [str(book)],
                    "metadata": {
                        "title": "Dune",
                        "author": "Frank Herbert",
                        "isbn": "9780441013593",
                        "year": 1965,
                        "media_type": "ebook",
                    },
                    "source": "librarr",
                    "confidence": 1.0,
                },
                headers=_headers(),
            )

        assert r.status_code == 202
        record_id = r.json()["id"]

        mock_resolve.assert_not_called()
        mock_calibre.add_book.assert_called_once()

        config_path = next(tmp_path.glob("config.yaml"))
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(record_id)
        store.close()
        assert record.state == FileState.IMPORTED
        assert record.matched_title == "Dune"
        assert record.matched_author == "Frank Herbert"
        assert not book.exists()

    def test_multi_part_audio_combines(self, tmp_path):
        """Multi-part audio list (n>1, all audio, primary first) combines
        into one m4b import via Pipeline.import_file_list."""
        from libris.config import load_config
        from libris.state import FileState, StateStore

        config_path = _write_config(tmp_path)
        app = create_app(config_path)
        client = TestClient(app)

        landing = tmp_path / "librarr-landing"
        landing.mkdir()
        part1 = landing / "book part 1.m4b"
        part2 = landing / "book part 2.m4b"
        part1.write_bytes(b"fake audio part 1")
        part2.write_bytes(b"fake audio part 2")

        def fake_combine_parts(part_files, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"combined audio")

        patcher, mock_calibre = _mock_calibre_pipeline()
        with (
            patcher,
            patch("libris.pipeline.audio_conv.combine_parts", side_effect=fake_combine_parts) as mock_combine,
            patch("libris.pipeline.audio_conv.convert_to_m4b") as mock_convert,
            patch("libris.pipeline.audio_tag.embed_metadata"),
            patch("libris.pipeline.resolve_metadata") as mock_resolve,
        ):
            r = client.post(
                "/api/v1/imports",
                json={
                    "files": [str(part1), str(part2)],
                    "metadata": {"title": "Combined Book", "media_type": "audiobook"},
                    "source": "librarr",
                },
                headers=_headers(),
            )

        assert r.status_code == 202
        record_id = r.json()["id"]

        mock_convert.assert_not_called()
        mock_combine.assert_called_once()
        mock_resolve.assert_not_called()
        mock_calibre.add_book.assert_called_once()

        config_path = next(tmp_path.glob("config.yaml"))
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(record_id)
        store.close()
        assert record.state == FileState.IMPORTED
        assert record.matched_title == "Combined Book"


# ---------------------------------------------------------------------------
# Idempotency: replayed POST must not double-import
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_replay_does_not_double_import(self, tmp_path):
        """A replayed POST for the same book (second file dropped at a new
        path, since the first is gone after import — e.g. Librarr re-sending
        after a dropped response) must not create a second Calibre entry.

        With mock_mode=True, Pipeline._handle_duplicate is a no-op (see
        pipeline.py: `if self.config.metadata.mock_mode: return None`), so
        this test disables mock_mode and backs Calibre's search() with the
        title/author the first import used — this is the pipeline's actual
        idempotency mechanism for a POST replay (the FileRecord-id dedup
        only helps when the same on-disk path/mtime recurs, which doesn't
        apply once the first import has moved/deleted the source file).
        duplicate_action defaults to "review", so the second import is
        routed to review instead of add_book — either way, add_book is
        called exactly once.
        """
        config_path = _write_config(tmp_path)
        # Flip mock_mode off for this test only, so _handle_duplicate runs.
        text = config_path.read_text().replace("mock_mode: true", "mock_mode: false")
        config_path.write_text(text)
        app = create_app(config_path)
        client = TestClient(app)

        book1 = _make_ebook(tmp_path, "dune.epub")

        patcher, mock_calibre = _mock_calibre_pipeline()
        with patcher, patch("libris.pipeline.resolve_metadata"):
            r1 = client.post(
                "/api/v1/imports",
                json={
                    "files": [str(book1)],
                    "metadata": {"title": "Dune", "author": "Frank Herbert"},
                    "source": "librarr",
                },
                headers=_headers(),
            )
            assert r1.status_code == 202

            # First import created Calibre book id 42 with no formats yet
            # recorded on the mock — simulate Calibre now reporting it as an
            # existing match for a replay (search + get_formats).
            mock_calibre.search.return_value = [42]
            mock_calibre.get_formats.return_value = {"epub"}

            book2 = _make_ebook(tmp_path, "dune-replay.epub")
            r2 = client.post(
                "/api/v1/imports",
                json={
                    "files": [str(book2)],
                    "metadata": {"title": "Dune", "author": "Frank Herbert"},
                    "source": "librarr",
                },
                headers=_headers(),
            )
            assert r2.status_code == 202

        assert mock_calibre.add_book.call_count == 1


# ---------------------------------------------------------------------------
# Import exception -> 202 still returned, record left in a sane state
# ---------------------------------------------------------------------------

class TestImportException:
    def test_import_exception_still_returns_202(self, client, tmp_path):
        from libris.config import load_config
        from libris.state import FileState, StateStore

        book = _make_ebook(tmp_path)
        with patch(
            "libris.pipeline.Pipeline",
            side_effect=RuntimeError("boom"),
        ):
            r = client.post(
                "/api/v1/imports",
                json={"files": [str(book)], "metadata": {"title": "Dune"}},
                headers=_headers(),
            )

        assert r.status_code == 202
        record_id = r.json()["id"]

        config_path = next(tmp_path.glob("config.yaml"))
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(record_id)
        store.close()
        # Pre-created record is left as-is (INCOMING) — never crashes the
        # request, never left half-written.
        assert record is not None
        assert record.state == FileState.INCOMING
