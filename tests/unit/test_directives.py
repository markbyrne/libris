"""Tests for the `directives` table — StateStore CRUD used by the directive API.

A directive is an external tool's (e.g. Librarr's) pre-registered match for
a filename the pipeline hasn't seen yet. add_directive/find_directive/
mark_directive_consumed/purge_stale_directives are the full surface the
pipeline seam and API routes need.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from libris.state import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


def _meta(title="Dune") -> str:
    return json.dumps({"title": title, "authors": ["Frank Herbert"]})


class TestDirectiveCRUD:
    def test_add_and_find(self, store):
        store.add_directive("d1", "dune.epub", _meta(), source="librarr", confidence=0.9)
        row = store.find_directive("dune.epub")
        assert row is not None
        assert row["id"] == "d1"
        assert row["source"] == "librarr"
        assert row["confidence"] == 0.9
        assert json.loads(row["metadata_json"])["title"] == "Dune"

    def test_find_missing_returns_none(self, store):
        assert store.find_directive("nonexistent.epub") is None

    def test_consumed_directive_still_matches(self, store):
        """Behavior change: find_directive matches regardless of consumed_at.

        A directive is marked consumed as soon as the pipeline looks it up
        (for observability), before the import actually completes. If the
        pipeline crashes between "mark consumed" and "import done", the next
        startup's orphan-reprocess must still be able to match this
        directive — otherwise the file falls back to weak Google/OpenLibrary
        resolution and the directive's intended metadata is lost forever.
        Directives are idempotent by filename+metadata, so re-matching an
        already-consumed row is safe.
        """
        store.add_directive("d1", "dune.epub", _meta(), source="librarr", confidence=1.0)
        store.mark_directive_consumed("d1")
        row = store.find_directive("dune.epub")
        assert row is not None
        assert row["id"] == "d1"
        assert row["consumed_at"] is not None

    def test_supersede_newest_wins(self, store):
        """A second directive for the same filename replaces the first."""
        store.add_directive("d1", "dune.epub", _meta("Old Title"), source="librarr", confidence=0.5)
        store.add_directive("d2", "dune.epub", _meta("New Title"), source="librarr", confidence=0.9)

        row = store.find_directive("dune.epub")
        assert row["id"] == "d2"
        assert json.loads(row["metadata_json"])["title"] == "New Title"

        # The superseded directive is gone, not just shadowed.
        cur = store._conn.execute("SELECT COUNT(*) AS n FROM directives WHERE filename=?", ("dune.epub",))
        assert cur.fetchone()["n"] == 1

    def test_newest_unconsumed_first_when_multiple_filenames(self, store):
        store.add_directive("d1", "a.epub", _meta(), source="librarr", confidence=1.0)
        store.add_directive("d2", "b.epub", _meta(), source="librarr", confidence=1.0)
        assert store.find_directive("a.epub")["id"] == "d1"
        assert store.find_directive("b.epub")["id"] == "d2"


class TestPurgeStaleDirectives:
    def test_purge_removes_old_unconsumed(self, store):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        store._conn.execute(
            "INSERT INTO directives (id, filename, metadata_json, source, confidence, created_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            ("old1", "stale.epub", _meta(), "librarr", 1.0, old_time),
        )
        count = store.purge_stale_directives(older_than_hours=48)
        assert count == 1
        assert store.find_directive("stale.epub") is None

    def test_purge_keeps_recent(self, store):
        store.add_directive("d1", "fresh.epub", _meta(), source="librarr", confidence=1.0)
        count = store.purge_stale_directives(older_than_hours=48)
        assert count == 0
        assert store.find_directive("fresh.epub") is not None

    def test_purge_ttl_boundary(self, store):
        """A directive just inside the TTL boundary is NOT purged (strict '<' cutoff)."""
        boundary_time = (datetime.now(timezone.utc) - timedelta(hours=47, minutes=58)).isoformat()
        store._conn.execute(
            "INSERT INTO directives (id, filename, metadata_json, source, confidence, created_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            ("boundary1", "boundary.epub", _meta(), "librarr", 1.0, boundary_time),
        )
        # Slightly older than the boundary should be purged.
        older_time = (datetime.now(timezone.utc) - timedelta(hours=48, minutes=1)).isoformat()
        store._conn.execute(
            "INSERT INTO directives (id, filename, metadata_json, source, confidence, created_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            ("old2", "older.epub", _meta(), "librarr", 1.0, older_time),
        )
        count = store.purge_stale_directives(older_than_hours=48)
        assert count == 1
        assert store.find_directive("older.epub") is None

    def test_purge_ignores_consumed(self, store):
        """purge_stale_directives only sweeps unconsumed rows (consumed_at IS
        NULL) — it must not touch consumed directives, since find_directive
        now matches consumed rows too (crash-safety) and a stray purge of a
        recently-consumed row would just be silent data loss with no
        upside. Old, already-matched directives are harmless to keep; they
        cost one row each and are never orphaned like unconsumed ones can
        be."""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        store._conn.execute(
            "INSERT INTO directives (id, filename, metadata_json, source, confidence, created_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("consumed1", "done.epub", _meta(), "librarr", 1.0, old_time, old_time),
        )
        # consumed_at IS NOT NULL -> purge query (which filters consumed_at IS NULL) skips it
        count = store.purge_stale_directives(older_than_hours=48)
        assert count == 0
        # And it's still findable afterwards — purge didn't touch it.
        row = store.find_directive("done.epub")
        assert row is not None
        assert row["id"] == "consumed1"
