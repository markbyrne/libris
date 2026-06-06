"""FileRecord dataclass and SQLite-backed state store.

The state store is the crash-recovery backbone of the pipeline:
- Every file gets a record before processing begins.
- Source files are deleted only after calibredb confirms success AND the record
  is marked IMPORTED in the DB.
- A file that is still on disk but marked IMPORTED in the DB was already
  successfully imported — safe to skip on re-run.
- A file stuck in PROCESSING state (e.g. after a crash) can be reset to
  INCOMING via:  UPDATE files SET state='incoming' WHERE state='processing';
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class FileState(str, Enum):
    INCOMING      = "incoming"       # detected by watcher, not yet started
    PROCESSING    = "processing"     # actively being converted / looked up / imported
    IMPORTED      = "imported"       # calibredb rc=0; source file deleted
    REVIEW        = "review"         # confidence < threshold; moved to review/ dir
    FAILED        = "failed"         # unrecoverable error; moved to failed/ dir
    PENDING_PARTS = "pending_parts"  # part N of M; waiting for siblings before combine


# ---------------------------------------------------------------------------
# FileRecord dataclass
# ---------------------------------------------------------------------------

@dataclass
class FileRecord:
    id: str                           # SHA-256 of original_path + str(mtime)
    original_path: str                # absolute path when first detected
    current_path: str                 # may change if file is moved to review/failed
    media_type: str                   # "ebook" | "audiobook" | "unknown"
    state: FileState
    confidence: Optional[float] = None
    matched_title: Optional[str] = None
    matched_author: Optional[str] = None
    error_msg: Optional[str] = None
    calibre_book_id: Optional[int] = None   # Calibre library book ID after import
    # Extra match detail — populated when a file enters review so list-review
    # can show the user enough context to decide whether the match is correct.
    matched_year: Optional[int] = None
    matched_publisher: Optional[str] = None
    matched_isbn: Optional[str] = None
    matched_cover_url: Optional[str] = None
    # Full serialised ScoredCandidate JSON — used by review-accept to import
    # without hitting the API again.  Set when file enters review.
    matched_metadata_json: Optional[str] = None
    # Multi-part audiobook tracking — set when state == PENDING_PARTS.
    # part_group_key groups sibling parts together (normalised clean title).
    part_num: Optional[int] = None
    total_parts: Optional[int] = None
    part_group_key: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def make_id(path: Path) -> str:
        """Stable ID based on file path + mtime. Deduplicates retries for the same file."""
        try:
            mtime = str(path.stat().st_mtime)
        except OSError:
            mtime = "0"
        raw = f"{path.resolve()}\x00{mtime}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# SQLite state store
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS files (
    id                    TEXT PRIMARY KEY,
    original_path         TEXT NOT NULL,
    current_path          TEXT NOT NULL,
    media_type            TEXT NOT NULL,
    state                 TEXT NOT NULL,
    confidence            REAL,
    matched_title         TEXT,
    matched_author        TEXT,
    error_msg             TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    calibre_book_id       INTEGER,
    matched_year          INTEGER,
    matched_publisher     TEXT,
    matched_isbn          TEXT,
    matched_cover_url     TEXT,
    matched_metadata_json TEXT,
    part_num              INTEGER,
    total_parts           INTEGER,
    part_group_key        TEXT
);
"""

_UPSERT = """
INSERT INTO files
    (id, original_path, current_path, media_type, state,
     confidence, matched_title, matched_author, error_msg, calibre_book_id,
     matched_year, matched_publisher, matched_isbn, matched_cover_url,
     matched_metadata_json,
     part_num, total_parts, part_group_key,
     created_at, updated_at)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    current_path          = excluded.current_path,
    media_type            = excluded.media_type,
    state                 = excluded.state,
    confidence            = excluded.confidence,
    matched_title         = excluded.matched_title,
    matched_author        = excluded.matched_author,
    error_msg             = excluded.error_msg,
    calibre_book_id       = excluded.calibre_book_id,
    matched_year          = excluded.matched_year,
    matched_publisher     = excluded.matched_publisher,
    matched_isbn          = excluded.matched_isbn,
    matched_cover_url     = excluded.matched_cover_url,
    matched_metadata_json = excluded.matched_metadata_json,
    part_num              = excluded.part_num,
    total_parts           = excluded.total_parts,
    part_group_key        = excluded.part_group_key,
    updated_at            = excluded.updated_at;
"""


class StateStore:
    """Thin SQLite wrapper. Thread-safe via check_same_thread=False + WAL mode."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,   # autocommit
        )
        # Use Row factory so _row_to_record accesses columns by name, not position.
        # This makes the schema resilient to future column additions.
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_TABLE)
        # Migrate: add calibre_book_id column if this is an older DB.
        # ALTER TABLE always appends to the end — named access in _row_to_record
        # means column order doesn't matter.
        for _migration in [
            "ALTER TABLE files ADD COLUMN calibre_book_id       INTEGER;",
            "ALTER TABLE files ADD COLUMN matched_year          INTEGER;",
            "ALTER TABLE files ADD COLUMN matched_publisher     TEXT;",
            "ALTER TABLE files ADD COLUMN matched_isbn          TEXT;",
            "ALTER TABLE files ADD COLUMN matched_cover_url     TEXT;",
            "ALTER TABLE files ADD COLUMN matched_metadata_json TEXT;",
            "ALTER TABLE files ADD COLUMN part_num              INTEGER;",
            "ALTER TABLE files ADD COLUMN total_parts           INTEGER;",
            "ALTER TABLE files ADD COLUMN part_group_key        TEXT;",
        ]:
            try:
                self._conn.execute(_migration)
            except Exception:
                pass  # Column already exists — safe to ignore

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert(self, record: FileRecord) -> None:
        """Insert or update a FileRecord."""
        record.updated_at = datetime.now(timezone.utc)
        self._conn.execute(_UPSERT, (
            record.id,
            record.original_path,
            record.current_path,
            record.media_type,
            record.state.value,
            record.confidence,
            record.matched_title,
            record.matched_author,
            record.error_msg,
            record.calibre_book_id,
            record.matched_year,
            record.matched_publisher,
            record.matched_isbn,
            record.matched_cover_url,
            record.matched_metadata_json,
            record.part_num,
            record.total_parts,
            record.part_group_key,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
        ))

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, record_id: str) -> Optional[FileRecord]:
        """Fetch a record by ID. Returns None if not found."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE id = ?", (record_id,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_calibre_id(self, calibre_book_id: int) -> Optional[FileRecord]:
        """Fetch the most recent record for a given Calibre book ID."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE calibre_book_id = ? ORDER BY updated_at DESC LIMIT 1",
            (calibre_book_id,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_current_path(self, path: str) -> Optional[FileRecord]:
        """Fetch by current_path (most recent if duplicates exist)."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE current_path = ? ORDER BY updated_at DESC LIMIT 1",
            (path,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_path(self, path: str) -> Optional[FileRecord]:
        """Fetch by original_path (most recent if duplicates exist)."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE original_path = ? ORDER BY created_at DESC LIMIT 1",
            (path,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def list_by_state(self, state: FileState) -> list[FileRecord]:
        """Return records in a given state, ordered by updated_at desc.

        Deduplicated by current_path — if the same file produced more than one
        record (e.g. processed twice with different mtimes), only the most
        recent record is returned.
        """
        rows = self._conn.execute(
            "SELECT * FROM files WHERE state = ? ORDER BY updated_at DESC",
            (state.value,),
        ).fetchall()
        seen: set[str] = set()
        result: list[FileRecord] = []
        for row in rows:
            r = _row_to_record(row)
            if r.current_path not in seen:
                seen.add(r.current_path)
                result.append(r)
        return result

    def reset_processing(self) -> int:
        """Reset all PROCESSING records back to INCOMING (recover from crash).

        Returns the number of records reset.
        """
        result = self._conn.execute(
            "UPDATE files SET state='incoming', updated_at=? WHERE state='processing'",
            (datetime.now(timezone.utc).isoformat(),),
        )
        return result.rowcount

    def list_pending_group(self, group_key: str) -> list[FileRecord]:
        """Return all PENDING_PARTS records for a specific group key, ordered by part_num."""
        rows = self._conn.execute(
            "SELECT * FROM files WHERE state = 'pending_parts' AND part_group_key = ?"
            " ORDER BY part_num ASC",
            (group_key,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def list_pending_groups(self) -> dict[str, list[FileRecord]]:
        """Return all PENDING_PARTS records grouped by part_group_key.

        Groups are ordered by the oldest record's created_at (ascending).
        Within each group, records are ordered by part_num.
        """
        rows = self._conn.execute(
            "SELECT * FROM files WHERE state = 'pending_parts'"
            " ORDER BY part_group_key, part_num ASC",
        ).fetchall()
        groups: dict[str, list[FileRecord]] = {}
        for row in rows:
            r = _row_to_record(row)
            key = r.part_group_key or ""
            groups.setdefault(key, []).append(r)
        # Sort groups by their oldest member's created_at
        return dict(
            sorted(groups.items(), key=lambda kv: min(r.created_at for r in kv[1]))
        )

    def cleanup_stale_review(self, current_path: str, exclude_id: str = "") -> None:
        """After a force-accept import, mark any old REVIEW record at *current_path*
        as IMPORTED so it no longer appears in list-review output.
        """
        self._conn.execute(
            "UPDATE files SET state='imported', updated_at=? "
            "WHERE current_path=? AND state='review' AND id != ?",
            (datetime.now(timezone.utc).isoformat(), current_path, exclude_id),
        )

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Row → dataclass helper
# ---------------------------------------------------------------------------

def _row_to_record(row: sqlite3.Row) -> FileRecord:
    """Convert a sqlite3.Row to a FileRecord using named column access.

    Named access (row["column"]) is resilient to column order, so adding new
    columns via ALTER TABLE never breaks this function.
    """
    return FileRecord(
        id=row["id"],
        original_path=row["original_path"],
        current_path=row["current_path"],
        media_type=row["media_type"],
        state=FileState(row["state"]),
        confidence=row["confidence"],
        matched_title=row["matched_title"],
        matched_author=row["matched_author"],
        error_msg=row["error_msg"],
        calibre_book_id=row["calibre_book_id"],
        matched_year=row["matched_year"],
        matched_publisher=row["matched_publisher"],
        matched_isbn=row["matched_isbn"],
        matched_cover_url=row["matched_cover_url"],
        matched_metadata_json=row["matched_metadata_json"],
        part_num=row["part_num"],
        total_parts=row["total_parts"],
        part_group_key=row["part_group_key"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
