"""Pipeline orchestrator: wires all modules together and owns the event loop.

This is the only module that coordinates cross-cutting concerns:
  - State transitions (INCOMING → PROCESSING → IMPORTED / REVIEW / FAILED)
  - Logging at pipeline boundaries
  - Notifications
  - Error handling and recovery

Each sub-module (audio, metadata, calibre) knows only its own domain.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from .audio import converter as audio_conv
from .audio import tagger as audio_tag
from .calibre import get_calibre
from .calibre.base import CalibreBackend
from .classifier import Classifier, MediaType
from .config import Config
from .ebook import converter as ebook_conv
from .exceptions import BookPipelineError, ClassificationError
from .metadata import resolve_metadata
from .metadata.base import MetadataResult
from .notifier import Notifier
from .state import FileRecord, FileState, StateStore
from .watcher import FileEvent, get_watcher

log = logging.getLogger(__name__)


class Pipeline:
    """Main pipeline: watches for files, processes them, tracks state."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._store = StateStore(config.paths.state_db)
        self._calibre: CalibreBackend = get_calibre(config.calibre)
        self._classifier = Classifier()
        self._notifier = Notifier(config.ntfy)
        self._watcher = get_watcher(config.watcher)

        # Ensure required directories exist
        for d in [
            config.watcher.incoming_dir,
            config.paths.staging_dir,
            config.paths.review_dir,
            config.paths.failed_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Daemon entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the watcher daemon. Blocks indefinitely."""
        log.info(
            "pipeline.starting",
            extra={
                "incoming": str(self.config.watcher.incoming_dir),
                "calibre_mode": self.config.calibre.mode,
                "threshold": self.config.metadata.confidence_threshold,
            },
        )
        try:
            for event in self._watcher.events():
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
        if record.state in (FileState.IMPORTED, FileState.PROCESSING):
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

    def _get_or_create_record(self, path: Path, media_type: str) -> FileRecord:
        record_id = FileRecord.make_id(path)
        existing = self._store.get(record_id)
        if existing:
            return existing
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
