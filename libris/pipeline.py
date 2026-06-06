"""Pipeline orchestrator: wires all modules together and owns the event loop.

This is the only module that coordinates cross-cutting concerns:
  - State transitions (INCOMING → PROCESSING → IMPORTED / REVIEW / FAILED)
  - Logging at pipeline boundaries
  - Notifications
  - Error handling and recovery

Each sub-module (audio, metadata, calibre) knows only its own domain.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from .audio import converter as audio_conv
from .audio import tagger as audio_tag
from .calibre import get_calibre
from .calibre.base import CalibreBackend
from .classifier import Classifier, MediaType
from .cleaner import clean_query, extract_part, strip_part_marker
from .config import Config
from .ebook import converter as ebook_conv
from .exceptions import BookPipelineError, ClassificationError
from .metadata import resolve_metadata
from .metadata.base import MetadataResult, SearchQuery
from .notifier import Notifier
from .state import FileRecord, FileState, StateStore
from .watcher import FileEvent, get_watcher

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metadata serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_candidate(scored: "ScoredCandidate") -> str:
    """Serialise a ScoredCandidate to a JSON string for storage.

    raw_response is intentionally omitted — it can be large and we only need
    the fields required to reconstruct a MetadataResult for import.
    """
    from .metadata.base import ScoredCandidate  # local import avoids circular
    c = scored.candidate
    return json.dumps({
        "title": c.title,
        "authors": c.authors,
        "isbn_13": c.isbn_13,
        "isbn_10": c.isbn_10,
        "published_year": c.published_year,
        "publisher": c.publisher,
        "description": c.description,
        "language": c.language,
        "series": c.series,
        "series_index": c.series_index,
        "cover_url": c.cover_url,
        "categories": c.categories,
        "source": c.source,
        "confidence": scored.confidence,
        "score_breakdown": scored.score_breakdown,
    }, ensure_ascii=False)


def _deserialize_candidate(blob: str) -> "ScoredCandidate":
    """Reconstruct a ScoredCandidate from a stored JSON string."""
    from .metadata.base import BookCandidate, ScoredCandidate
    d = json.loads(blob)
    candidate = BookCandidate(
        title=d["title"],
        authors=d.get("authors", []),
        isbn_13=d.get("isbn_13"),
        isbn_10=d.get("isbn_10"),
        published_year=d.get("published_year"),
        publisher=d.get("publisher"),
        description=d.get("description"),
        language=d.get("language"),
        series=d.get("series"),
        series_index=d.get("series_index"),
        cover_url=d.get("cover_url"),
        categories=d.get("categories", []),
        source=d.get("source", ""),
    )
    return ScoredCandidate(
        candidate=candidate,
        confidence=d.get("confidence", 0.0),
        score_breakdown=d.get("score_breakdown", {}),
    )


class Pipeline:
    """Main pipeline: watches for files, processes them, tracks state."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._store = StateStore(config.paths.state_db)
        self._calibre: CalibreBackend = get_calibre(config.calibre)
        self._classifier = Classifier()
        self._notifier = Notifier(config.ntfy)
        self._watcher = get_watcher(config.watcher)
        # Ensures watcher events and periodic scans don't process the same
        # file concurrently.  One file at a time is fine for a library tool.
        self._process_lock = threading.Lock()

        # Ensure required directories exist
        for d in [
            config.watcher.incoming_dir,
            config.paths.staging_dir,
            config.paths.staging_dir / "pending",
            config.paths.review_dir,
            config.paths.failed_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Daemon entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the watcher daemon. Blocks indefinitely."""
        interval = self.config.watcher.scan_interval_hours
        log.info(
            "pipeline.starting",
            extra={
                "incoming": str(self.config.watcher.incoming_dir),
                "calibre_mode": self.config.calibre.mode,
                "threshold": self.config.metadata.confidence_threshold,
                "scan_interval_hours": interval,
            },
        )

        # Escalate any timed-out pending parts before doing anything else
        self._check_pending_timeouts()

        # Startup scan: pick up files that arrived while the daemon was offline
        self._scan_incoming(reason="startup")

        # Periodic re-scan in a background daemon thread
        if interval > 0:
            t = threading.Thread(
                target=self._periodic_scan_loop,
                args=(interval * 3600,),
                daemon=True,
                name="libris-scanner",
            )
            t.start()

        try:
            for event in self._watcher.events():
                with self._process_lock:
                    self._handle_event(event)
        except KeyboardInterrupt:
            log.info("pipeline.stopping")
        finally:
            self._watcher.stop()
            self._store.close()

    # ------------------------------------------------------------------
    # Single-file entry point (used by CLI import-one)
    # ------------------------------------------------------------------

    def process_file(self, path: Path) -> FileRecord:
        """Process a single file immediately (no watcher). Returns the final FileRecord."""
        event = FileEvent(path=path, event_type="created")
        return self._handle_event(event)

    def force_import(self, path: Path, result: MetadataResult) -> FileRecord:
        """Import a file using pre-resolved metadata, bypassing API lookup and threshold.

        Used by the interactive `rematch` command after the user manually selects
        a metadata candidate.  The file is imported into Calibre immediately.
        """
        media_type = self._classifier.classify(path)
        if media_type == MediaType.UNKNOWN:
            log.warning("pipeline.force_import_unknown", extra={"path": str(path)})
            return self._make_record(path, "unknown", FileState.FAILED)

        record = self._get_or_create_record(path, media_type.value)
        record.state = FileState.PROCESSING
        record.matched_title = result.title
        record.matched_author = result.author
        record.confidence = result.confidence
        record.matched_year = int(result.year) if result.year else None
        record.matched_publisher = result.publisher or None
        record.matched_isbn = result.isbn
        record.matched_cover_url = result.best.candidate.cover_url if result.best else None
        self._store.upsert(record)

        try:
            # Embed audio tags before adding to Calibre (cover set via set_cover instead)
            if media_type == MediaType.AUDIOBOOK:
                audio_tag.embed_metadata(path, result, overwrite=True)

            book_id = self._calibre.add_book(path)
            record.calibre_book_id = book_id
            self._calibre.set_metadata(book_id, result)
            if self.config.output.embed_cover_art and result.cover_path:
                self._calibre.set_cover(book_id, result.cover_path)

            # original_path == processed_path here (file is already in final format)
            return self._mark_imported(record, path, path, result)

        except BookPipelineError as exc:
            log.exception("pipeline.force_import_failed", extra={"path": str(path)})
            return self._mark_failed(record, exc)

    def import_from_record(self, record: "FileRecord") -> "FileRecord":
        """Import a review-queue file using its persisted metadata (no API call).

        Reconstructs the MetadataResult from the JSON stored when the file
        first entered review, re-downloads the cover if configured, then
        delegates to force_import.

        Falls back to process_file (threshold=0) if no cached metadata exists
        (e.g. records created before this feature was added).
        """
        if not record.matched_metadata_json:
            log.info(
                "pipeline.import_from_record.no_cache",
                extra={"path": record.current_path},
            )
            self.config.metadata.confidence_threshold = 0.0
            return self.process_file(Path(record.current_path))

        scored = _deserialize_candidate(record.matched_metadata_json)

        # Re-download cover from stored URL — one HTTP request, no API quota used
        cover_path = None
        if self.config.output.embed_cover_art and scored.candidate.cover_url:
            import httpx
            from .metadata.resolver import _download_cover
            with httpx.Client(timeout=12.0) as client:
                cover_path = _download_cover(scored.candidate.cover_url, client)

        query = SearchQuery(
            clean_title=record.matched_title or scored.candidate.title,
            author_hint=record.matched_author,
        )
        result = MetadataResult(
            query=query,
            best=scored,
            all_candidates=[scored],
            above_threshold=True,
            cover_path=cover_path,
        )
        return self.force_import(Path(record.current_path), result)

    # ------------------------------------------------------------------
    # Core event handler
    # ------------------------------------------------------------------

    def _handle_event(self, event: FileEvent) -> FileRecord:
        path = event.path
        log.info("pipeline.event", extra={"path": str(path), "type": event.event_type})

        # ── Classify ──────────────────────────────────────────────────
        media_type = self._classifier.classify(path)
        if media_type == MediaType.UNKNOWN:
            log.info("pipeline.skip_unknown", extra={"path": str(path)})
            return self._make_record(path, "unknown", FileState.FAILED)

        # ── Dedup check ───────────────────────────────────────────────
        record = self._get_or_create_record(path, media_type.value)
        if record.state in (FileState.IMPORTED, FileState.PROCESSING, FileState.PENDING_PARTS):
            log.info(
                "pipeline.skip_duplicate",
                extra={"path": str(path), "state": record.state.value},
            )
            return record

        # ── Process ───────────────────────────────────────────────────
        record.state = FileState.PROCESSING
        self._store.upsert(record)

        try:
            if media_type == MediaType.AUDIOBOOK:
                record = self._process_audiobook(path, record)
            else:
                record = self._process_ebook(path, record)
        except BookPipelineError as exc:
            log.exception("pipeline.failed", extra={"path": str(path)})
            record = self._mark_failed(record, exc)

        return record

    # ------------------------------------------------------------------
    # Audiobook pipeline
    # ------------------------------------------------------------------

    def _process_audiobook(self, path: Path, record: FileRecord) -> FileRecord:
        """Convert, tag, and import a single audio file or folder of parts."""

        if path.is_dir():
            return self._process_audiobook_folder(path, record)

        # ── Multi-part detection ──────────────────────────────────────
        part_num, total_parts = extract_part(path.stem)
        if part_num is not None:
            return self._handle_pending_part(path, record, part_num, total_parts)

        # Single audio file
        ext = path.suffix.lstrip(".").lower()

        # Convert to M4B if needed
        if ext != "m4b":
            m4b_path = self.config.paths.staging_dir / (path.stem + ".m4b")
            log.info("pipeline.audio.converting", extra={"file": str(path)})
            audio_conv.convert_to_m4b(path, m4b_path)
        else:
            m4b_path = path

        return self._resolve_tag_and_import_audio(m4b_path, record, original_path=path)

    # ------------------------------------------------------------------
    # Multi-part audiobook pipeline
    # ------------------------------------------------------------------

    def _handle_pending_part(
        self,
        path: Path,
        record: FileRecord,
        part_num: int,
        total_parts: Optional[int],
    ) -> FileRecord:
        """Stage a single part file and combine+import when the set is complete.

        Non-M4B parts (e.g. MP3, M4A) are converted to M4B before staging so
        that combine_parts can always stream-copy homogeneous AAC input.
        """
        # Build stable group key: clean title with part marker stripped
        stripped_stem = strip_part_marker(path.stem)
        group_key = (clean_query(stripped_stem) or stripped_stem).lower().strip()

        pending_dir = self.config.paths.staging_dir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)

        # Convert to M4B before staging if not already — ensures combine_parts
        # can stream-copy (-c copy) without mismatched codec errors.
        ext = path.suffix.lstrip(".").lower()
        if ext != "m4b":
            log.info("pipeline.audio.converting_part", extra={"file": path.name})
            dest = pending_dir / (path.stem + ".m4b")
            audio_conv.convert_to_m4b(path, dest)
            path.unlink(missing_ok=True)
        else:
            dest = pending_dir / path.name
            _safe_move(path, dest)

        record.part_num = part_num
        record.total_parts = total_parts
        record.part_group_key = group_key
        record.current_path = str(dest)
        record.state = FileState.PENDING_PARTS
        self._store.upsert(record)

        log.info(
            "pipeline.audio.pending_part",
            extra={
                "file": path.name,
                "part": part_num,
                "total": total_parts,
                "group": group_key,
            },
        )

        # Only attempt auto-combine when we know the total and it's now complete
        if total_parts is not None:
            group_records = self._store.list_pending_group(group_key)
            received = {r.part_num for r in group_records if r.part_num is not None}
            expected = set(range(1, total_parts + 1))
            if received >= expected:
                return self._combine_pending_group(group_key, group_records)

        self._notifier.send_pending_parts_alert(record)
        return record

    def _combine_pending_group(
        self,
        group_key: str,
        group_records: list[FileRecord],
    ) -> FileRecord:
        """Combine a complete (or force-combined) set of parts into one M4B."""
        parts = sorted(group_records, key=lambda r: r.part_num or 0)
        part_files = [Path(r.current_path) for r in parts]

        # Output filename: stripped stem of the first part
        stripped_stem = strip_part_marker(Path(part_files[0]).stem)
        out_path = self.config.paths.staging_dir / f"{stripped_stem}.m4b"

        log.info(
            "pipeline.audio.combining_parts",
            extra={"group": group_key, "parts": len(parts), "output": str(out_path)},
        )
        audio_conv.combine_parts(part_files, out_path)

        # Primary record is the first part; mark remaining parts as consumed
        primary = parts[0]
        for r in parts[1:]:
            r.state = FileState.IMPORTED
            r.error_msg = f"Combined into {out_path.name}"
            self._store.upsert(r)
            # Remove the individual part file — it's now in the combined M4B
            Path(r.current_path).unlink(missing_ok=True)

        # Clear part fields on the primary record before continuing
        primary.part_num = None
        primary.total_parts = None
        primary.current_path = str(out_path)

        return self._resolve_tag_and_import_audio(out_path, primary, original_path=part_files[0])

    def _check_pending_timeouts(self) -> None:
        """Escalate overdue pending-part groups to review/ on daemon startup.

        Groups that have been waiting longer than config.multipart.timeout_hours
        are moved to review/ with an error message so the user can decide whether
        to force-combine or wait for the missing parts.
        """
        from datetime import timedelta, datetime, timezone as _tz
        timeout = timedelta(hours=self.config.multipart.timeout_hours)
        now = datetime.now(_tz.utc)

        groups = self._store.list_pending_groups()
        for group_key, records in groups.items():
            oldest = min(records, key=lambda r: r.created_at)
            if now - oldest.created_at <= timeout:
                continue

            received = len(records)
            total = records[0].total_parts or "?"
            log.warning(
                "pipeline.audio.parts_timeout",
                extra={"group": group_key, "received": received, "total": total},
            )
            for r in records:
                current = Path(r.current_path)
                dest = self.config.paths.review_dir / current.name
                _safe_move(current, dest)
                r.state = FileState.REVIEW
                r.current_path = str(dest)
                r.error_msg = (
                    f"Partial: {received} of {total} parts after "
                    f"{self.config.multipart.timeout_hours:.0f}h — "
                    "run 'libris combine-parts' to import what's available"
                )
                self._store.upsert(r)

    def _process_audiobook_folder(self, folder: Path, record: FileRecord) -> FileRecord:
        """Combine a folder of audio parts into a single M4B and import."""
        audio_files = audio_conv.find_audio_files(folder)
        if not audio_files:
            from .exceptions import ConversionError
            raise ConversionError(f"No audio files found in {folder}")

        out_name = folder.name + ".m4b"
        m4b_path = self.config.paths.staging_dir / out_name

        log.info(
            "pipeline.audio.combining",
            extra={"parts": len(audio_files), "output": str(m4b_path)},
        )
        audio_conv.combine_parts(audio_files, m4b_path)

        return self._resolve_tag_and_import_audio(m4b_path, record, original_path=folder)

    def _resolve_tag_and_import_audio(
        self,
        m4b_path: Path,
        record: FileRecord,
        original_path: Path,
    ) -> FileRecord:
        """Resolve metadata, embed tags, and import to Calibre."""
        # ── Metadata ──────────────────────────────────────────────────
        result = resolve_metadata(
            m4b_path.stem,
            self.config.metadata,
            embed_cover=self.config.output.embed_cover_art,
        )
        record.matched_title = result.title
        record.matched_author = result.author
        record.confidence = result.confidence

        if not result.above_threshold:
            log.info(
                "pipeline.audio.low_confidence",
                extra={"confidence": result.confidence, "title": result.title},
            )
            return self._mark_review(record, result, m4b_path)

        # ── Tag ───────────────────────────────────────────────────────
        audio_tag.embed_metadata(
            m4b_path,
            result,
            overwrite=self.config.metadata.overwrite_existing,
        )

        # ── Import ────────────────────────────────────────────────────
        book_id = self._calibre.add_book(m4b_path)
        record.calibre_book_id = book_id
        log.info("pipeline.audio.imported", extra={"book_id": book_id, "title": result.title})

        # ── Full metadata + cover in Calibre ──────────────────────────
        self._calibre.set_metadata(book_id, result)
        if self.config.output.embed_cover_art and result.cover_path:
            self._calibre.set_cover(book_id, result.cover_path)

        return self._mark_imported(record, m4b_path, original_path, result)

    # ------------------------------------------------------------------
    # Ebook pipeline
    # ------------------------------------------------------------------

    def _process_ebook(self, path: Path, record: FileRecord) -> FileRecord:
        """Convert (if needed) and import an ebook to Calibre."""
        ext = path.suffix.lstrip(".").lower()

        if ext == "epub":
            epub_path = path
        else:
            log.info("pipeline.ebook.converting", extra={"from": ext, "file": str(path)})
            epub_path = ebook_conv.to_epub(path, self._calibre)

        # ── Metadata lookup for ebooks ────────────────────────────────
        result = resolve_metadata(
            epub_path.stem,
            self.config.metadata,
            embed_cover=self.config.output.embed_cover_art,
        )
        record.matched_title = result.title
        record.matched_author = result.author
        record.confidence = result.confidence

        if not result.above_threshold:
            log.info("pipeline.ebook.low_confidence",
                     extra={"confidence": result.confidence, "title": result.title})
            return self._mark_review(record, result, epub_path)

        book_id = self._calibre.add_book(epub_path)
        record.calibre_book_id = book_id
        log.info("pipeline.ebook.imported", extra={"book_id": book_id, "file": str(epub_path)})

        # ── Full metadata + cover in Calibre ──────────────────────────
        self._calibre.set_metadata(book_id, result)
        if self.config.output.embed_cover_art and result.cover_path:
            self._calibre.set_cover(book_id, result.cover_path)

        return self._mark_imported(record, epub_path, path, result)

    # ------------------------------------------------------------------
    # State transition helpers
    # ------------------------------------------------------------------

    def _mark_imported(
        self,
        record: FileRecord,
        processed_path: Path,
        original_path: Path,
        result: Optional[MetadataResult],
    ) -> FileRecord:
        """Mark IMPORTED and delete the original source file."""
        # Delete staging copy if different from original
        if processed_path != original_path and processed_path.exists():
            processed_path.unlink(missing_ok=True)

        # Delete original
        if original_path.exists():
            if original_path.is_dir():
                shutil.rmtree(original_path, ignore_errors=True)
            else:
                original_path.unlink(missing_ok=True)

        # Clean up temp cover file
        if result and result.cover_path and result.cover_path.exists():
            result.cover_path.unlink(missing_ok=True)

        record.state = FileState.IMPORTED
        self._store.upsert(record)
        return record

    def _mark_review(
        self,
        record: FileRecord,
        result: MetadataResult,
        file_to_move: Path,
    ) -> FileRecord:
        """Move file to review/ and send notification."""
        dest = self.config.paths.review_dir / file_to_move.name
        _safe_move(file_to_move, dest)

        record.current_path = str(dest)
        record.state = FileState.REVIEW
        record.confidence = result.confidence
        record.matched_title = result.title
        record.matched_author = result.author
        record.matched_year = int(result.year) if result.year else None
        record.matched_publisher = result.publisher or None
        record.matched_isbn = result.isbn
        record.matched_cover_url = result.best.candidate.cover_url if result.best else None
        record.matched_metadata_json = _serialize_candidate(result.best) if result.best else None
        self._store.upsert(record)
        self._notifier.send_review_alert(record, result)
        return record

    def _mark_failed(self, record: FileRecord, error: Exception) -> FileRecord:
        """Move file to failed/ and send notification."""
        current = Path(record.current_path)
        if current.exists():
            dest = self.config.paths.failed_dir / current.name
            _safe_move(current, dest)
            record.current_path = str(dest)

        record.state = FileState.FAILED
        record.error_msg = f"{type(error).__name__}: {str(error)[:500]}"
        self._store.upsert(record)
        self._notifier.send_error_alert(record, error)
        return record

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _scan_incoming(self, reason: str = "periodic") -> None:
        """Process every file/directory currently in incoming_dir.

        Safe to call at any time — the dedup check in _handle_event skips
        anything already in IMPORTED, PROCESSING, or PENDING_PARTS state.
        Hidden files (dot-prefixed) are ignored.
        """
        incoming = self.config.watcher.incoming_dir
        try:
            entries = sorted(
                p for p in incoming.iterdir()
                if not p.name.startswith(".")
            )
        except OSError as exc:
            log.warning("pipeline.scan_failed: %s", exc)
            return

        if entries:
            log.info(
                "pipeline.scan",
                extra={"reason": reason, "dir": str(incoming), "count": len(entries)},
            )
        else:
            log.debug(
                "pipeline.scan",
                extra={"reason": reason, "dir": str(incoming), "count": 0},
            )

        for path in entries:
            with self._process_lock:
                self._handle_event(FileEvent(path=path, event_type="created"))

    def _periodic_scan_loop(self, interval_seconds: float) -> None:
        """Background thread: sleep, then re-scan incoming_dir, repeat."""
        while True:
            time.sleep(interval_seconds)
            log.info(
                "pipeline.periodic_scan",
                extra={"interval_hours": interval_seconds / 3600},
            )
            self._scan_incoming(reason="periodic")

    def _get_or_create_record(self, path: Path, media_type: str) -> FileRecord:
        # Primary lookup: exact ID match (path + mtime hash)
        record_id = FileRecord.make_id(path)
        existing = self._store.get(record_id)
        if existing:
            return existing
        # Fallback: same file, different mtime (e.g. file moved to review/ and
        # re-processed — avoids creating a duplicate record)
        existing_by_path = self._store.get_by_current_path(str(path.resolve()))
        if existing_by_path:
            return existing_by_path
        return self._make_record(path, media_type, FileState.INCOMING)

    def _make_record(self, path: Path, media_type: str, state: FileState) -> FileRecord:
        record = FileRecord(
            id=FileRecord.make_id(path),
            original_path=str(path),
            current_path=str(path),
            media_type=media_type,
            state=state,
        )
        self._store.upsert(record)
        return record


def _safe_move(src: Path, dst: Path) -> None:
    """Move src to dst, ensuring parent dirs exist. No-op if src missing."""
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
