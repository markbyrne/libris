"""Tests for the ntfy Notifier — focused on the import alert wired into the
pipeline's _mark_imported (previously built but never called)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from libris.config import NtfyConfig
from libris.notifier import Notifier
from libris.state import FileRecord, FileState


def _record(tmp_path) -> FileRecord:
    p = tmp_path / "The Vegetarian.epub"
    return FileRecord(
        id="rec1",
        original_path=str(p),
        current_path=str(p),
        media_type="ebook",
        state=FileState.IMPORTED,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _notifier(enabled: bool = True, topic: str = "my-topic") -> Notifier:
    n = Notifier(NtfyConfig(topic=topic, enabled=enabled))
    n._client = MagicMock()  # never hit the network
    return n


def test_imported_alert_posts_when_enabled(tmp_path):
    n = _notifier()
    n.send_imported_alert(_record(tmp_path), None)
    assert n._client.post.call_count == 1
    # Title header carries the "Imported" line (encoded utf-8 bytes).
    _, kwargs = n._client.post.call_args
    assert b"Imported" in kwargs["headers"]["Title"]


def test_imported_alert_noop_when_disabled(tmp_path):
    n = _notifier(enabled=False)
    n.send_imported_alert(_record(tmp_path), None)
    n._client.post.assert_not_called()


def test_imported_alert_noop_when_no_topic(tmp_path):
    n = _notifier(topic="")
    n.send_imported_alert(_record(tmp_path), None)
    n._client.post.assert_not_called()


def test_import_finalization_pings_notifier(tmp_path, monkeypatch):
    """_mark_imported must call send_imported_alert (the wiring this release adds)."""
    from libris import pipeline as pl

    called = {}

    def _fake_alert(record, result):
        called["hit"] = True

    # Build a Pipeline-like object cheaply: we only exercise _mark_imported's
    # notifier call, so stub the store + notifier and bypass __init__.
    p = pl.Pipeline.__new__(pl.Pipeline)
    p._store = MagicMock()
    p._notifier = MagicMock()
    p._notifier.send_imported_alert.side_effect = _fake_alert
    p.config = MagicMock()
    p.config.calibre.reconnect_url = None
    monkeypatch.setattr(pl, "notify_reconnect", lambda *_a, **_k: None)

    src = tmp_path / "gone.epub"  # non-existent → skips unlink branches
    p._mark_imported(_record(tmp_path), src, src, None)
    assert called.get("hit") is True
