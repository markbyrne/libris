"""Tests for the web dashboard's Logs view:

  - RingBufferHandler captures emitted records and get_records() filters
    by level (at-or-above threshold) / search / limit.
  - GET /logs renders the page (nav + table) and includes buffered records.
  - GET /logs/rows returns just the row fragment, reflecting new records
    emitted since the page loaded (the piece that makes htmx auto-refresh
    and the filter controls work).

No real network/subprocess is used — TestClient only.
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from libris.web import create_app, log_buffer
from libris.web.log_buffer import RingBufferHandler


@pytest.fixture(autouse=True)
def _clear_log_buffer():
    """Keep the process-wide singleton buffer isolated between tests."""
    log_buffer.clear()
    yield
    log_buffer.clear()


@pytest.fixture
def client(config_yaml):
    app = create_app(config_yaml)
    return TestClient(app)


# ---------------------------------------------------------------------------
# RingBufferHandler / get_records
# ---------------------------------------------------------------------------

class TestRingBufferHandler:
    def test_captures_emitted_records(self):
        handler = RingBufferHandler()
        logger = logging.getLogger("libris.tests.ring_buffer")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("hello world")
        finally:
            logger.removeHandler(handler)

        records = handler.get_records()
        assert len(records) == 1
        assert records[0].level == "INFO"
        assert records[0].logger == "libris.tests.ring_buffer"
        assert "hello world" in records[0].message

    def test_maxlen_drops_oldest(self):
        handler = RingBufferHandler(maxlen=3)
        logger = logging.getLogger("libris.tests.ring_buffer_maxlen")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for i in range(5):
                logger.info("msg-%d", i)
        finally:
            logger.removeHandler(handler)

        records = handler.get_records()
        assert len(records) == 3
        # newest-first; oldest two (msg-0, msg-1) were evicted
        assert [r.message for r in records] == ["msg-4", "msg-3", "msg-2"]

    def test_get_records_filters_by_level_at_or_above(self):
        handler = RingBufferHandler()
        logger = logging.getLogger("libris.tests.ring_buffer_level")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("info msg")
            logger.warning("warn msg")
            logger.error("error msg")
        finally:
            logger.removeHandler(handler)

        warn_and_above = handler.get_records(level="WARNING")
        assert {r.level for r in warn_and_above} == {"WARNING", "ERROR"}

        errors_only = handler.get_records(level="ERROR")
        assert [r.level for r in errors_only] == ["ERROR"]

        everything = handler.get_records()
        assert len(everything) == 3

    def test_get_records_filters_by_search(self):
        handler = RingBufferHandler()
        logger = logging.getLogger("libris.tests.ring_buffer_search")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("importing book_a.epub")
            logger.info("importing book_b.epub")
            logger.warning("could not match author")
        finally:
            logger.removeHandler(handler)

        matches = handler.get_records(search="book_a")
        assert len(matches) == 1
        assert "book_a" in matches[0].message

        matches_logger_name = handler.get_records(search="ring_buffer_search")
        assert len(matches_logger_name) == 3

        no_matches = handler.get_records(search="nonexistent-needle")
        assert no_matches == []

    def test_get_records_respects_limit(self):
        handler = RingBufferHandler()
        logger = logging.getLogger("libris.tests.ring_buffer_limit")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for i in range(10):
                logger.info("msg-%d", i)
        finally:
            logger.removeHandler(handler)

        assert len(handler.get_records(limit=4)) == 4


# ---------------------------------------------------------------------------
# GET /logs — full page
# ---------------------------------------------------------------------------

class TestLogsPage:
    def test_returns_200_with_nav_and_table(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200
        body = resp.text
        assert "Logs" in body
        assert 'href="/logs"' in body
        assert "nav-link active" in body
        assert "<table" in body

    def test_shows_emitted_record(self, client):
        logging.getLogger("libris.pipeline").info("import succeeded: totally-unique-marker-1")
        resp = client.get("/logs")
        assert resp.status_code == 200
        assert "totally-unique-marker-1" in resp.text
        assert "libris.pipeline" in resp.text

    def test_level_filter_excludes_lower_severity(self, client):
        logging.getLogger("libris.pipeline").info("plain info marker-2")
        logging.getLogger("libris.pipeline").error("boom marker-2-error")

        resp = client.get("/logs", params={"level": "ERROR"})
        assert resp.status_code == 200
        assert "marker-2-error" in resp.text
        assert "plain info marker-2" not in resp.text

    def test_search_filters_records(self, client):
        logging.getLogger("libris.pipeline").info("needle-marker-3")
        logging.getLogger("libris.pipeline").info("unrelated-marker-3")

        resp = client.get("/logs", params={"q": "needle-marker-3"})
        assert resp.status_code == 200
        assert "needle-marker-3" in resp.text
        assert "unrelated-marker-3" not in resp.text

    def test_invalid_level_param_is_ignored(self, client):
        logging.getLogger("libris.pipeline").info("marker-4-visible")
        resp = client.get("/logs", params={"level": "NOT_A_LEVEL"})
        assert resp.status_code == 200
        assert "marker-4-visible" in resp.text


# ---------------------------------------------------------------------------
# GET /logs/rows — htmx fragment
# ---------------------------------------------------------------------------

class TestLogsRowsFragment:
    def test_returns_only_rows_fragment(self, client):
        resp = client.get("/logs/rows")
        assert resp.status_code == 200
        assert "<html" not in resp.text
        assert "nav-link" not in resp.text

    def test_reflects_newly_emitted_record(self, client):
        # Nothing yet for this marker.
        resp = client.get("/logs/rows", params={"q": "fresh-marker-5"})
        assert "fresh-marker-5" not in resp.text

        logging.getLogger("libris.pipeline").info("fresh-marker-5 arrived")

        resp = client.get("/logs/rows", params={"q": "fresh-marker-5"})
        assert resp.status_code == 200
        assert "fresh-marker-5 arrived" in resp.text

    def test_empty_state_message(self, client):
        resp = client.get("/logs/rows", params={"q": "no-such-record-anywhere"})
        assert resp.status_code == 200
        assert "No log records" in resp.text
