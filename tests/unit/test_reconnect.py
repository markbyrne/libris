"""Tests for the calibre-web /reconnect notification (corruption prevention).

calibre-web holds metadata.db open continuously while libris writes through
calibredb — Calibre does not support two programs on one metadata.db, and a
stale calibre-web connection can desync into "database disk image is
malformed" (June 2026 production incident).  notify_reconnect pings
calibre-web's /reconnect endpoint after calibredb writes so it reopens its
connection instead.

The call must be strictly best-effort: a missing endpoint (404 — calibre-web
started without -r), a down server, or a timeout must never fail the import
that already succeeded.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from libris.calibre.base import notify_reconnect


def _response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


# ---------------------------------------------------------------------------
# notify_reconnect unit behaviour
# ---------------------------------------------------------------------------

class TestNotifyReconnect:
    def test_disabled_when_url_unset(self):
        with patch("httpx.get") as mock_get:
            notify_reconnect(None)
            notify_reconnect("")
        mock_get.assert_not_called()

    def test_pings_configured_url(self):
        with patch("httpx.get", return_value=_response(200)) as mock_get:
            notify_reconnect("http://server:8083/reconnect")
        mock_get.assert_called_once_with("http://server:8083/reconnect", timeout=5.0)

    def test_404_is_swallowed_and_warned(self, caplog):
        """404 = calibre-web running without -r; warn but never raise."""
        with patch("httpx.get", return_value=_response(404)):
            with caplog.at_level("WARNING"):
                notify_reconnect("http://server:8083/reconnect")
        assert any("-r flag" in r.message for r in caplog.records)

    def test_connection_error_is_swallowed(self):
        """calibre-web being down must not fail the completed import."""
        with patch("httpx.get", side_effect=ConnectionError("refused")):
            notify_reconnect("http://server:8083/reconnect")  # must not raise

    def test_unexpected_status_is_swallowed(self):
        with patch("httpx.get", return_value=_response(500)):
            notify_reconnect("http://server:8083/reconnect")  # must not raise


# ---------------------------------------------------------------------------
# Pipeline integration: _mark_imported triggers the ping
# ---------------------------------------------------------------------------

class TestMarkImportedNotifies:
    def _make_pipeline(self, tmp_path, reconnect_url):
        from libris.pipeline import Pipeline

        cfg = MagicMock()
        cfg.calibre.reconnect_url = reconnect_url
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.config = cfg
        pipeline._store = MagicMock()
        return pipeline

    def _make_record(self, tmp_path):
        from libris.state import FileRecord, FileState

        f = tmp_path / "book.m4b"
        f.write_bytes(b"audio")
        return f, FileRecord(
            id="r1",
            original_path=str(f),
            current_path=str(f),
            media_type="audiobook",
            state=FileState.PROCESSING,
        )

    def test_mark_imported_pings_when_configured(self, tmp_path):
        pipeline = self._make_pipeline(tmp_path, "http://server:8083/reconnect")
        f, record = self._make_record(tmp_path)

        with patch("libris.pipeline.notify_reconnect") as mock_notify:
            pipeline._mark_imported(record, f, f, None)

        mock_notify.assert_called_once_with("http://server:8083/reconnect")

    def test_mark_imported_noop_when_unset(self, tmp_path):
        """Default config (no reconnect_url) → notify called with None → no HTTP."""
        pipeline = self._make_pipeline(tmp_path, None)
        f, record = self._make_record(tmp_path)

        with patch("httpx.get") as mock_get:
            pipeline._mark_imported(record, f, f, None)

        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestReconnectConfig:
    def _load(self, tmp_path, calibre_extra=""):
        import textwrap

        from libris.config import load_config

        cfg = tmp_path / "config.yaml"
        cfg.write_text(textwrap.dedent(f"""
            watcher:
              incoming_dir: {tmp_path}/incoming
            paths:
              staging_dir: {tmp_path}/staging
              review_dir:  {tmp_path}/review
              failed_dir:  {tmp_path}/failed
              state_db:    {tmp_path}/libris.db
            calibre:
              mode: local
              library_path: {tmp_path}/library
              {calibre_extra}
            metadata:
              confidence_threshold: 0.75
            ntfy:
              topic: test
        """))
        return load_config(cfg)

    def test_default_is_none(self, tmp_path):
        config = self._load(tmp_path)
        assert config.calibre.reconnect_url is None

    def test_yaml_value_parsed(self, tmp_path):
        config = self._load(
            tmp_path, "reconnect_url: http://192.168.1.10:8083/reconnect"
        )
        assert config.calibre.reconnect_url == "http://192.168.1.10:8083/reconnect"
