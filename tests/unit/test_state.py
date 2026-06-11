"""Tests for libris.state — FileRecord and SQLite StateStore."""

from datetime import datetime, timezone

import pytest

from libris.state import FileRecord, FileState, StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def record(tmp_path):
    p = tmp_path / "book.epub"
    return FileRecord(
        id="test_id_001",
        original_path=str(p),
        current_path=str(p),
        media_type="ebook",
        state=FileState.INCOMING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


class TestStateStore:
    def test_upsert_and_get(self, store, record):
        store.upsert(record)
        fetched = store.get(record.id)
        assert fetched is not None
        assert fetched.id == record.id
        assert fetched.state == FileState.INCOMING
        assert fetched.media_type == "ebook"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("nonexistent_id") is None

    def test_upsert_updates_existing(self, store, record):
        store.upsert(record)
        record.state = FileState.IMPORTED
        record.matched_title = "Project Hail Mary"
        store.upsert(record)

        fetched = store.get(record.id)
        assert fetched.state == FileState.IMPORTED
        assert fetched.matched_title == "Project Hail Mary"

    def test_get_by_path(self, store, record):
        store.upsert(record)
        fetched = store.get_by_path(record.original_path)
        assert fetched is not None
        assert fetched.id == record.id

    def test_get_by_path_nonexistent_returns_none(self, store):
        assert store.get_by_path("/nonexistent/path.epub") is None

    def test_list_by_state(self, store, tmp_path):
        r1 = FileRecord(
            id="r1", original_path="/a.epub", current_path="/a.epub",
            media_type="ebook", state=FileState.REVIEW,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        r2 = FileRecord(
            id="r2", original_path="/b.epub", current_path="/review/b.epub",
            media_type="ebook", state=FileState.REVIEW,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        r3 = FileRecord(
            id="r3", original_path="/c.epub", current_path="/c.epub",
            media_type="ebook", state=FileState.IMPORTED,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        for r in [r1, r2, r3]:
            store.upsert(r)

        review_records = store.list_by_state(FileState.REVIEW)
        assert len(review_records) == 2
        ids = {r.id for r in review_records}
        assert ids == {"r1", "r2"}

        imported = store.list_by_state(FileState.IMPORTED)
        assert len(imported) == 1

        failed = store.list_by_state(FileState.FAILED)
        assert len(failed) == 0

    def test_all_states_roundtrip(self, store, tmp_path):
        for state in FileState:
            r = FileRecord(
                id=f"id_{state.value}",
                original_path=f"/{state.value}.epub",
                current_path=f"/{state.value}.epub",
                media_type="ebook",
                state=state,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            store.upsert(r)
            fetched = store.get(r.id)
            assert fetched.state == state

    def test_optional_fields_null(self, store, record):
        store.upsert(record)
        fetched = store.get(record.id)
        assert fetched.confidence is None
        assert fetched.matched_title is None
        assert fetched.matched_author is None
        assert fetched.error_msg is None

    def test_optional_fields_populated(self, store, record):
        record.confidence = 0.82
        record.matched_title = "Project Hail Mary"
        record.matched_author = "Andy Weir"
        record.error_msg = "test error"
        store.upsert(record)

        fetched = store.get(record.id)
        assert fetched.confidence == pytest.approx(0.82)
        assert fetched.matched_title == "Project Hail Mary"
        assert fetched.matched_author == "Andy Weir"
        assert fetched.error_msg == "test error"


class TestFileRecordMakeId:
    def test_same_path_same_id(self, tmp_path):
        p = tmp_path / "book.epub"
        p.touch()
        id1 = FileRecord.make_id(p)
        id2 = FileRecord.make_id(p)
        assert id1 == id2

    def test_different_paths_different_ids(self, tmp_path):
        p1 = tmp_path / "book1.epub"
        p2 = tmp_path / "book2.epub"
        p1.touch()
        p2.touch()
        assert FileRecord.make_id(p1) != FileRecord.make_id(p2)

    def test_nonexistent_path_does_not_raise(self, tmp_path):
        p = tmp_path / "nonexistent.epub"
        # Should not raise even if file doesn't exist
        result = FileRecord.make_id(p)
        assert isinstance(result, str)
        assert len(result) == 16
