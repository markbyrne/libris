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
    INCOMING   = "incoming"    # detected by watcher, not yet started
    PROCESSING = "processing"  # actively being converted / looked up / imported
    IMPORTED   = "imported"    # calibredb rc=0; source file deleted
    REVIEW     = "review"      # confidence < threshold; moved to review/ dir
    FAILED     = "failed"      # unrecoverable error; moved to failed/ dir


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
    id             TEXT PRIMARY KEY,
    original_path  TEXT NOT NULL,
    current_path   TEXT NOT NULL,
    media_type     TEXT NOT NULL,
    state          TEXT NOT NULL,
    confidence     REAL,
    matched_title  TEXT,
    matched_author TEXT,
    error_msg      TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
"""

_UPSERT = """
INSERT INTO files
    (id, original_path, current_path, media_type, state,
     confidence, matched_title, matched_author, error_msg, created_at, updated_at)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    current_path   = excluded.current_path,
    media_type     = excluded.media_type,
    state          = excluded.state,
    confidence     = excluded.confidence,
    matched_title  = excluded.matched_title,
    matched_author = excluded.matched_author,
    error_msg      = excluded.error_msg,
    updated_at     = excluded.updated_at;
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
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_TABLE)

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

    def get_by_path(self, path: str) -> Optional[FileRecord]:
        """Fetch by original_path (most recent if duplicates exist)."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE original_path = ? ORDER BY created_at DESC LIMIT 1",
            (path,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def list_by_state(self, state: FileState) -> list[FileRecord]:
        """Return all records in a given state, ordered by updated_at desc."""
        rows = self._conn.execute(
            "SELECT * FROM files WHERE state = ? ORDER BY updated_at DESC",
            (state.value,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Row → dataclass helper
# ---------------------------------------------------------------------------

def _row_to_record(row: tuple) -> FileRecord:
    (id_, orig, curr, mtype, state, conf, title, author, err, created, updated) = row
    return FileRecord(
        id=id_,
        original_path=orig,
        current_path=curr,
        media_type=mtype,
        state=FileState(state),
        confidence=conf,
        matched_title=title,
        matched_author=author,
        error_msg=err,
        created_at=datetime.fromisoformat(created),
        updated_at=datetime.fromisoformat(updated),
    )
