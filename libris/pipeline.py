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
import threading
import time
from pathlib import Path

from rapidfuzz import fuzz as _fuzz

from .audio import converter as audio_conv
from .audio import tagger as audio_tag
from .calibre import get_calibre
from .calibre.base import CalibreBackend, format_authors, notify_reconnect
from .classifier import EBOOK_EXTENSIONS, Classifier, MediaType
from .cleaner import clean_query, extract_part, is_chaff, strip_part_marker
from .config import Config
from .ebook import converter as ebook_conv
from .exceptions import BookPipelineError, CalibreImportError
from .metadata import resolve_metadata
from .metadata.base import BookCandidate, MetadataResult, ScoredCandidate, SearchQuery
from .notifier import Notifier
from .state import FileRecord, FileState, StateStore
from .watcher import FileEvent, get_watcher

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metadata serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_candidate(scored: ScoredCandidate) -> str:
    """Serialise a ScoredCandidate to a JSON string for storage.

    raw_response is intentionally omitted — it can be large and we only need
    the fields required to reconstruct a MetadataResult for import.
    """
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


def _deserialize_candidate(blob: str) -> ScoredCandidate:
    """Reconstruct a ScoredCandidate from a stored JSON string."""
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


def _result_from_directive(row) -> MetadataResult:
    """Build a pre-resolved MetadataResult from a `directives` row.

    Mirrors _deserialize_candidate's field shape (the directive API stores
    metadata_json in the same shape as _serialize_candidate output) so a
    directive is just a pre-resolved ScoredCandidate entering the same
    downstream machinery as an API-resolved match — above_threshold=True,
    confidence taken from the directive.
    """
    scored = _deserialize_candidate(row["metadata_json"])
    query = SearchQuery(clean_title=scored.candidate.title)
    return MetadataResult(
        query=query,
        best=scored,
        all_candidates=[scored],
        above_threshold=True,
    )


def _apply_metadata_to_record(record: FileRecord, result: MetadataResult) -> None:
    """Write all matched-metadata fields from a MetadataResult onto a FileRecord."""
    record.matched_title = result.title
    record.matched_author = result.author
    record.confidence = result.confidence
    record.matched_year = int(result.year) if result.year else None
    record.matched_publisher = result.publisher or None
    record.matched_isbn = result.isbn
    record.matched_cover_url = result.best.candidate.cover_url if result.best else None
    record.matched_metadata_json = _serialize_candidate(result.best) if result.best else None


def _add_book_args(result: MetadataResult) -> dict[str, str | None]:
    """Resolved title/authors kwargs for CalibreBackend.add_book.

    These become calibredb add's --title/--authors flags and determine the
    directory Calibre creates.  Authors must come from the candidate list
    joined with " & " — result.author joins with ", ", which Calibre would
    parse as a single inverted "Surname, Given" name.
    """
    authors = (
        format_authors(result.best.candidate.authors)
        if result.best and result.best.candidate.authors
        else None
    )
    return {"title": result.title or None, "authors": authors or None}


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
        _apply_metadata_to_record(record, result)
        self._store.upsert(record)

        try:
            # ── Duplicate / format-merge check ────────────────────────────
            # force_import bypasses _handle_duplicate, but we still need to
            # detect existing Calibre entries so we don't create a second one.
            #
            # Different format:  always add_format to the existing record.
            # Same format + --overwrite (duplicate_action=="import"):  replace.
            # Same format + no --overwrite:  block — the CLI should have caught
            #   this via the is_duplicate flag, but raise here as a safety net.
            if not self.config.metadata.mock_mode:
                dup_ids = self._find_calibre_duplicates(result.title, result.author)
                if dup_ids:
                    incoming_fmt = path.suffix.lstrip(".").lower()
                    try:
                        existing_formats = self._calibre.get_formats(dup_ids[0])
                    except Exception:
                        existing_formats = set()

                    if incoming_fmt not in existing_formats:
                        # Different format: merge into existing record + update metadata.
                        # Audio formats (M4B, MP3, …) are not supported by
                        # calibredb add_format — skip straight to add_book so the
                        # audiobook gets its own separate Calibre entry.
                        _is_audio = incoming_fmt in audio_conv.AUDIO_EXTENSIONS
                        if incoming_fmt and not _is_audio:
                            try:
                                self._calibre.add_format(dup_ids[0], path)
                                path.unlink(missing_ok=True)
                                if result.cover_path:
                                    self._calibre.set_cover(dup_ids[0], result.cover_path)
                                self._calibre.set_metadata(dup_ids[0], result)
                                if result.cover_path:
                                    result.cover_path.unlink(missing_ok=True)
                                record.state = FileState.IMPORTED
                                record.calibre_book_id = dup_ids[0]
                                record.error_msg = (
                                    f"Added {incoming_fmt.upper()} format to "
                                    f"Calibre book {dup_ids[0]}"
                                )
                                self._store.upsert(record)
                                log.info(
                                    "pipeline.force_import_format_merged",
                                    extra={
                                        "book_id": dup_ids[0],
                                        "format": incoming_fmt,
                                        "title": result.title,
                                    },
                                )
                                return record
                            except Exception as exc:
                                log.warning(
                                    "pipeline.force_import_add_format_failed: %s", exc
                                )
                                # fall through — add_book will create a new record
                        else:
                            log.debug(
                                "pipeline.force_import_skip_add_format",
                                extra={
                                    "fmt": incoming_fmt or "(none)",
                                    "reason": "audio format — will create separate Calibre entry",
                                },
                            )

                    elif self.config.metadata.duplicate_action == "import":
                        # Same format + --overwrite: replace existing format
                        # Audio formats cannot use add_format — treat like same-format
                        # without overwrite and return to review so user can choose
                        _is_audio = incoming_fmt in audio_conv.AUDIO_EXTENSIONS
                        if _is_audio:
                            id_str = ", ".join(str(i) for i in dup_ids[:3])
                            id_suffix = (
                                f" (and {len(dup_ids) - 3} more)" if len(dup_ids) > 3 else ""
                            )
                            record.state = FileState.REVIEW
                            _apply_metadata_to_record(record, result)
                            record.error_msg = (
                                f"Duplicate: already in Calibre as {incoming_fmt.upper()} "
                                f"(ID{'s' if len(dup_ids) > 1 else ''}: "
                                f"{id_str}{id_suffix})"
                            )
                            self._store.upsert(record)
                            log.info(
                                "pipeline.force_import_duplicate_blocked",
                                extra={"book_id": dup_ids[0], "title": result.title},
                            )
                            if result.cover_path and result.cover_path.exists():
                                result.cover_path.unlink(missing_ok=True)
                            return record
                        try:
                            if media_type == MediaType.AUDIOBOOK:
                                audio_tag.embed_metadata(path, result, overwrite=True)
                            self._calibre.add_format(dup_ids[0], path)
                            path.unlink(missing_ok=True)
                            if result.cover_path:
                                self._calibre.set_cover(dup_ids[0], result.cover_path)
                            self._calibre.set_metadata(dup_ids[0], result)
                            if result.cover_path:
                                result.cover_path.unlink(missing_ok=True)
                            record.state = FileState.IMPORTED
                            record.calibre_book_id = dup_ids[0]
                            record.error_msg = (
                                f"Replaced {incoming_fmt.upper()} format in "
                                f"Calibre book {dup_ids[0]}"
                            )
                            self._store.upsert(record)
                            log.info(
                                "pipeline.force_import_format_replaced",
                                extra={
                                    "book_id": dup_ids[0],
                                    "format": incoming_fmt,
                                    "title": result.title,
                                },
                            )
                            return record
                        except Exception as exc:
                            log.warning(
                                "pipeline.force_import_replace_failed: %s", exc
                            )
                            # fall through — add_book creates a new entry as last resort

                    else:
                        # Same format, no --overwrite: flag as duplicate and stay in
                        # REVIEW.  Do NOT raise BookPipelineError — that would move
                        # the file to failed/ via _mark_failed.  The CLI detects this
                        # REVIEW return and offers [o]verwrite / [d]iscard / [r]keep.
                        id_str = ", ".join(str(i) for i in dup_ids[:3])
                        id_suffix = (
                            f" (and {len(dup_ids) - 3} more)" if len(dup_ids) > 3 else ""
                        )
                        record.state = FileState.REVIEW
                        _apply_metadata_to_record(record, result)
                        record.error_msg = (
                            f"Duplicate: already in Calibre as {incoming_fmt.upper()} "
                            f"(ID{'s' if len(dup_ids) > 1 else ''}: "
                            f"{id_str}{id_suffix})"
                        )
                        self._store.upsert(record)
                        log.info(
                            "pipeline.force_import_duplicate_blocked",
                            extra={"book_id": dup_ids[0], "title": result.title},
                        )
                        # Clean up cover temp file; CLI retry re-downloads via
                        # import_from_record using the stored matched_cover_url.
                        if result.cover_path and result.cover_path.exists():
                            result.cover_path.unlink(missing_ok=True)
                        return record

            # Embed audio tags before adding to Calibre (cover set via set_cover instead)
            if media_type == MediaType.AUDIOBOOK:
                audio_tag.embed_metadata(path, result, overwrite=True)

            try:
                book_id = self._calibre.add_book(path, **_add_book_args(result))
            except CalibreImportError as exc:
                # calibredb's own duplicate detection matches on TITLE ALONE
                # (ignoring author), so it can reject a file that our
                # _find_calibre_duplicates() missed because the author differs.
                # Re-route to review/ with a duplicate flag so the CLI prompts
                # the user, instead of marking the file FAILED.
                if "already exist" not in str(exc) and "not added" not in str(exc):
                    raise
                dup_ids = self._find_calibre_duplicates_by_title(result.title)
                incoming_fmt = path.suffix.lstrip(".").lower()
                record.state = FileState.REVIEW
                _apply_metadata_to_record(record, result)
                if dup_ids:
                    id_str = ", ".join(str(i) for i in dup_ids[:3])
                    id_suffix = f" (and {len(dup_ids) - 3} more)" if len(dup_ids) > 3 else ""
                    record.error_msg = (
                        f"Duplicate: already in Calibre as {incoming_fmt.upper()} "
                        f"(ID{'s' if len(dup_ids) > 1 else ''}: {id_str}{id_suffix})"
                    )
                else:
                    record.error_msg = (
                        f"Duplicate: Calibre already has a book titled "
                        f"\"{result.title}\""
                    )
                self._store.upsert(record)
                log.info(
                    "pipeline.force_import_duplicate_blocked_by_calibredb",
                    extra={"title": result.title, "dup_ids": dup_ids},
                )
                if result.cover_path and result.cover_path.exists():
                    result.cover_path.unlink(missing_ok=True)
                return record

            record.calibre_book_id = book_id
            if result.cover_path:
                self._calibre.set_cover(book_id, result.cover_path)
            self._calibre.set_metadata(book_id, result)

            # original_path == processed_path here (file is already in final format)
            return self._mark_imported(record, path, path, result)

        except BookPipelineError as exc:
            log.exception("pipeline.force_import_failed", extra={"path": str(path)})
            return self._mark_failed(record, exc)

    def import_from_record(self, record: FileRecord) -> FileRecord:
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
        if scored.candidate.cover_url:
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

        # ── Chaff guard ───────────────────────────────────────────────
        # Reject known-clutter files before any processing so they never
        # consume API quota or clutter list-review.  False positives can be
        # recovered with 'libris recover --id N'.
        if is_chaff(path.name):
            log.info("pipeline.skip_chaff", extra={"path": str(path)})
            record = self._make_record(path, "chaff", FileState.FAILED)
            record.error_msg = "Chaff: filename matches known non-book pattern"
            failed_dest = self.config.paths.failed_dir / path.name
            if path.exists():
                _safe_move(path, failed_dest)
                record.current_path = str(failed_dest)
            self._store.upsert(record)
            return record

        # ── Symlink guard ─────────────────────────────────────────────
        # Reject symlinks before any processing.  A symlink in the incoming
        # directory could point to an arbitrary host path, giving an attacker
        # a primitive for reading files outside the configured tree.
        if path.is_symlink():
            log.warning("pipeline.skip_symlink", extra={"path": str(path)})
            return self._make_record(path, "symlink", FileState.FAILED)

        # ── Classify ──────────────────────────────────────────────────
        media_type = self._classifier.classify(path)
        if media_type == MediaType.UNKNOWN:
            log.info("pipeline.skip_unknown", extra={"path": str(path)})
            return self._make_record(path, "unknown", FileState.FAILED)

        # ── Dedup check ───────────────────────────────────────────────
        record = self._get_or_create_record(path, media_type.value)

        if record.state in (FileState.IMPORTED, FileState.PROCESSING):
            # Directories: always re-dispatch — individual file records handle
            # their own dedup, so this is safe and necessary to pick up files
            # inside a directory whose container record was previously marked done.
            if path.is_dir():
                log.info("pipeline.directory_redispatch", extra={"path": str(path)})
                # fall through to re-dispatch
            # File: a successfully completed import always moves or deletes the
            # source.  If the file is still at its original incoming location the
            # previous run didn't finish cleanly — reset and re-process it.
            elif Path(record.original_path).resolve() == path.resolve():
                log.info(
                    "pipeline.reprocess_orphaned",
                    extra={"path": str(path), "state": record.state.value},
                )
                record.state = FileState.INCOMING
                self._store.upsert(record)
                # fall through to re-process
            else:
                log.info(
                    "pipeline.skip_duplicate",
                    extra={"path": str(path), "state": record.state.value},
                )
                return record

        if record.state == FileState.PENDING_PARTS:
            # Only skip if the staged file is still where the DB says it is.
            # If the user moved a part back to incoming/ after a failed combine,
            # current_path will point to a non-existent staging/pending path —
            # fall through so the file is re-staged and the path is corrected.
            if Path(record.current_path).exists():
                log.info(
                    "pipeline.skip_duplicate",
                    extra={"path": str(path), "state": record.state.value},
                )
                return record
            log.info(
                "pipeline.pending_part_restage",
                extra={"path": str(path), "stale_path": record.current_path},
            )

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
        total_parts: int | None,
        group_key: str | None = None,
    ) -> FileRecord:
        """Stage a single part file and combine+import when the set is complete.

        Non-M4B parts (e.g. MP3, M4A) are converted to M4B before staging so
        that combine_parts can always stream-copy homogeneous AAC input.

        ``part_num``/``total_parts`` are always honored as passed — callers
        (e.g. ``import_file_list``) may supply explicit part numbers that
        don't come from filename parsing.

        ``group_key``: when provided, used verbatim instead of the
        stem-derived key below. This lets a caller group files whose
        filenames don't share a common stem (e.g. an API-driven import where
        the caller already knows which files belong together). Default None
        preserves the exact stem-derived behaviour for the daemon/watcher path.
        """
        if group_key is None:
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

        # Only attempt auto-combine when we know the total and it's now complete.
        # Count only parts whose files are actually present on disk — stale DB
        # records from a previous failed combine should not block or trigger a
        # combine with missing files.
        if total_parts is not None:
            group_records = self._store.list_pending_group(group_key)
            received = {
                r.part_num for r in group_records
                if r.part_num is not None and Path(r.current_path).exists()
            }
            expected = set(range(1, total_parts + 1))
            if received >= expected:
                # Filter to only records with present files before combining
                live_records = [
                    r for r in group_records if Path(r.current_path).exists()
                ]
                return self._combine_pending_group(group_key, live_records)

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

        # Delete ALL original part files now that they're in the combined output.
        # Do this immediately — before the combined file continues through the
        # pipeline — so cleanup happens whether the result ends up IMPORTED or
        # REVIEW.  (If the file goes to review, _mark_review never sees the
        # part paths, so delaying cleanup until _mark_imported would leave
        # part 1 orphaned in staging/pending/ forever.)
        for part_file in part_files:
            part_file.unlink(missing_ok=True)

        # Mark non-primary part records as consumed
        primary = parts[0]
        for r in parts[1:]:
            r.state = FileState.IMPORTED
            r.error_msg = f"Combined into {out_path.name}"
            self._store.upsert(r)

        # Clear part fields on the primary record before continuing
        primary.part_num = None
        primary.total_parts = None
        primary.current_path = str(out_path)

        # original_path=part_files[0] tells _mark_imported to clean up the
        # source path if it still exists; unlink(missing_ok=True) above means
        # this is a safe no-op when already deleted.
        return self._resolve_tag_and_import_audio(out_path, primary, original_path=part_files[0])

    def _check_pending_timeouts(self) -> None:
        """Escalate overdue pending-part groups to review/ on daemon startup.

        Groups that have been waiting longer than config.multipart.timeout_hours
        are moved to review/ with an error message so the user can decide whether
        to force-combine or wait for the missing parts.
        """
        from datetime import datetime, timedelta
        from datetime import timezone as _tz
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
        """Recursively extract and dispatch all book files from a dropped directory.

        Walk the entire directory tree and collect every audio and ebook file,
        regardless of nesting depth.  Audio files are grouped by the directory
        that directly contains them:

        - **One audio file** in a directory → dispatched as a standalone audiobook
          through the normal single-file pipeline (metadata lookup, Calibre import).
        - **Multiple audio files** in the same directory → treated as parts of one
          audiobook and handed to :meth:`import_directory_combined`, which assigns
          sequential part numbers and auto-combines when all parts are staged.
        - **Ebook files** at any depth → each dispatched individually through the
          normal ebook pipeline regardless of how many share a directory.

        After all files have been extracted the original directory tree is removed.

        Example — dropping a "Christopher Paolini" folder::

            Christopher Paolini/
              Eragon.m4b                         → standalone audiobook import
              Eldest/
                Eldest.m4b                       → standalone audiobook import
              Brisingr/
                Brisingr - Part 1.m4b  ┐
                Brisingr - Part 2.m4b  ├─ import_directory_combined → 1 M4B
                Brisingr - Part 3.m4b  ┘
              Inheritance Cycle/
                Inheritance - Part 1.m4b  ┐
                Inheritance - Part 2.m4b  ├─ separate group → combined
                Inheritance - Part 3.m4b  ┘
              Eragon.epub                        → ebook import (alongside audiobook)
        """
        import os
        from collections import defaultdict

        # ── Walk the full tree ────────────────────────────────────────
        # audio_by_dir maps each directory to the audio files directly in it.
        # ebook_files collects every ebook file found anywhere in the tree.
        audio_by_dir: dict[Path, list[Path]] = defaultdict(list)
        ebook_files: list[Path] = []

        for root, dirs, files in os.walk(str(folder)):
            # Deterministic traversal order; skip hidden directories.
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            root_path = Path(root)
            for fname in sorted(files):
                if fname.startswith("."):
                    continue
                p = root_path / fname
                suffix = p.suffix.lstrip(".").lower()
                if suffix in audio_conv.AUDIO_EXTENSIONS:
                    audio_by_dir[root_path].append(p)
                elif suffix in EBOOK_EXTENSIONS:
                    ebook_files.append(p)

        total_audio = sum(len(v) for v in audio_by_dir.values())
        audio_groups = len(audio_by_dir)

        if total_audio == 0 and not ebook_files:
            from .exceptions import ConversionError
            raise ConversionError(f"No supported files found in {folder}")

        log.info(
            "pipeline.folder_dispatch",
            extra={
                "folder": folder.name,
                "audio": total_audio,
                "audio_groups": audio_groups,
                "ebooks": len(ebook_files),
            },
        )

        last_record = record

        # ── Audio groups ──────────────────────────────────────────────
        for dir_path in sorted(audio_by_dir):
            group = audio_by_dir[dir_path]  # already sorted by filename from walk

            if len(group) == 1:
                # ── Single file in this directory → standalone audiobook ──
                audio_file = group[0]
                file_record = self._get_or_create_record(audio_file, "audiobook")

                if file_record.state in (FileState.IMPORTED, FileState.PROCESSING):
                    if Path(file_record.original_path).resolve() == audio_file.resolve():
                        log.info("pipeline.audio.folder_reprocess",
                                 extra={"file": audio_file.name})
                        file_record.state = FileState.INCOMING
                        self._store.upsert(file_record)
                    else:
                        log.info("pipeline.audio.folder_skip",
                                 extra={"file": audio_file.name,
                                        "state": file_record.state.value})
                        continue

                if (file_record.state == FileState.PENDING_PARTS
                        and Path(file_record.current_path).exists()):
                    continue  # already staged, awaiting sibling parts

                file_record.state = FileState.PROCESSING
                self._store.upsert(file_record)
                try:
                    last_record = self._process_audiobook(audio_file, file_record)
                except BookPipelineError as exc:
                    log.exception("pipeline.audio.folder_file_failed",
                                  extra={"file": str(audio_file)})
                    last_record = self._mark_failed(file_record, exc)

            else:
                # ── Multiple files in this directory → treat as parts ──
                # import_directory_combined assigns sequential part numbers
                # (1 … N, sorted by filename) and auto-combines when all parts
                # are staged.  Idempotent: already-staged parts are skipped.
                log.info(
                    "pipeline.audio.folder_parts_group",
                    extra={"dir": dir_path.name, "count": len(group)},
                )
                try:
                    last_record = self.import_directory_combined(dir_path)
                except (BookPipelineError, ValueError):
                    log.exception("pipeline.audio.folder_parts_failed",
                                  extra={"dir": str(dir_path)})

        # ── Ebook files ───────────────────────────────────────────────
        for ebook_file in ebook_files:
            file_record = self._get_or_create_record(ebook_file, "ebook")

            if file_record.state in (FileState.IMPORTED, FileState.PROCESSING):
                if Path(file_record.original_path).resolve() == ebook_file.resolve():
                    log.info("pipeline.ebook.folder_reprocess",
                             extra={"file": ebook_file.name})
                    file_record.state = FileState.INCOMING
                    self._store.upsert(file_record)
                else:
                    log.info("pipeline.ebook.folder_skip",
                             extra={"file": ebook_file.name,
                                    "state": file_record.state.value})
                    continue

            file_record.state = FileState.PROCESSING
            self._store.upsert(file_record)
            try:
                last_record = self._process_ebook(ebook_file, file_record)
            except BookPipelineError as exc:
                log.exception("pipeline.ebook.folder_file_failed",
                              extra={"file": str(ebook_file)})
                last_record = self._mark_failed(file_record, exc)

        # ── Cleanup ───────────────────────────────────────────────────
        # All book files have been moved out by their processing steps.
        # Remove whatever remains (.DS_Store, cover art, NFO, etc.).
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            log.info("pipeline.folder_cleaned", extra={"folder": str(folder)})
        else:
            log.warning("pipeline.folder_cleanup_failed", extra={"folder": str(folder)})

        record.state = FileState.IMPORTED
        record.error_msg = (
            f"Directory: extracted {total_audio} audio "
            f"({audio_groups} group(s)), {len(ebook_files)} ebook file(s)"
        )
        self._store.upsert(record)

        return last_record

    # ------------------------------------------------------------------
    # Forced directory combine — public API
    # ------------------------------------------------------------------

    def import_directory_combined(self, folder: Path) -> FileRecord:
        """Treat every audio file in *folder* as a part of one audiobook.

        All audio files (non-recursive, sorted by name) are assigned sequential
        part numbers (1 … N) regardless of their individual filenames, then
        staged via :meth:`_handle_pending_part`.  When the last file is staged
        the set is complete and the auto-combine logic in ``_handle_pending_part``
        fires immediately, producing a single M4B that continues through the
        normal metadata + Calibre import pipeline.

        The group key is derived from the directory name (part markers stripped),
        so calling this method twice on the same folder is idempotent — the
        second call will find the parts already staged and skip them.

        Args:
            folder: Directory containing the audio files to combine.

        Returns:
            The :class:`~libris.state.FileRecord` for the combined (or last
            staged) file.

        Raises:
            ValueError: If no audio files are found in *folder*.
        """
        audio_files = audio_conv.find_audio_files(folder, recursive=False)
        if not audio_files:
            raise ValueError(f"No audio files found in {folder}")

        total_parts = len(audio_files)
        # Stable group key based on the folder name, not individual filenames
        stripped_stem = strip_part_marker(folder.name)
        group_key = (clean_query(stripped_stem) or stripped_stem).lower().strip()

        log.info(
            "pipeline.audio.import_dir_combined",
            extra={"folder": folder.name, "parts": total_parts, "group": group_key},
        )

        last_record: FileRecord | None = None
        for idx, audio_path in enumerate(audio_files, start=1):
            file_record = self._get_or_create_record(audio_path, "audiobook")

            # Skip parts that are already correctly staged (idempotency)
            if (
                file_record.state == FileState.PENDING_PARTS
                and file_record.part_group_key == group_key
                and Path(file_record.current_path).exists()
            ):
                log.info(
                    "pipeline.audio.import_dir_skip_staged",
                    extra={"file": audio_path.name, "part": idx},
                )
                last_record = file_record
                continue

            file_record.state = FileState.PROCESSING
            self._store.upsert(file_record)

            last_record = self._handle_pending_part(
                audio_path, file_record, part_num=idx, total_parts=total_parts
            )

        # last_record is always set: audio_files is non-empty (checked above)
        assert last_record is not None
        return last_record

    # ------------------------------------------------------------------
    # Explicit file-list import — public API
    # ------------------------------------------------------------------

    def import_file_list(self, paths: list[Path]) -> FileRecord:
        """Import an explicit, caller-supplied list of files as one book.

        This is the seam for API-driven imports (a future endpoint will
        expose this over HTTP): unlike :meth:`process_file` or
        :meth:`import_directory_combined`, the files here are not discovered
        by watching or scanning a directory — the caller (e.g. Librarr)
        already knows exactly which files make up one book and passes them
        explicitly. The files may live OUTSIDE ``config.watcher.incoming_dir``
        entirely, e.g. in another tool's own landing/download folder.

        Every downstream outcome (imported / duplicate / review / failed)
        removes or relocates the INPUT file per existing pipeline semantics
        (see ``_mark_imported``, ``_mark_review``, ``_mark_failed``,
        ``_combine_pending_group``) — exactly as it already does for
        watcher- and directory-driven imports. A caller that passes a
        hardlink rather than the original file is unaffected: only the
        supplied link is removed/moved, any other links to the same inode
        are untouched.

        Args:
            paths: The files that make up one book. A single path is a
                complete book (ebook or single-file audiobook). Multiple
                paths are treated as sequential parts of one multi-part
                audiobook — the caller guarantees they are all audio files;
                mixed ebook+audio or non-audio multi-file lists are rejected.

        Returns:
            The :class:`~libris.state.FileRecord` for the imported (or
            combined) file. For multi-part input this is the record keyed
            on ``paths[0]`` — the surviving primary record after combine
            (see ``_combine_pending_group``), so callers can poll state by
            ``paths[0]``'s basename.

        Raises:
            ValueError: If *paths* is empty, or if more than one path is
                given and any of them is not an audio file.
        """
        if not paths:
            raise ValueError("import_file_list requires at least one path")

        if len(paths) == 1:
            return self.process_file(paths[0])

        # n > 1: caller guarantees these are all parts of one audiobook.
        for p in paths:
            ext = p.suffix.lstrip(".").lower()
            if ext not in audio_conv.AUDIO_EXTENSIONS:
                raise ValueError(
                    f"import_file_list: multi-file import requires all-audio "
                    f"input, got non-audio file {p.name}"
                )

        total_parts = len(paths)
        # Stable group key derived once from paths[0] — the same
        # clean_query/strip-part-marker helpers import_directory_combined
        # uses, so mismatched stems across the other parts don't matter.
        stripped_stem = strip_part_marker(paths[0].stem)
        group_key = (clean_query(stripped_stem) or stripped_stem).lower().strip()

        log.info(
            "pipeline.audio.import_file_list",
            extra={"primary": paths[0].name, "parts": total_parts, "group": group_key},
        )

        last_record: FileRecord | None = None
        for idx, audio_path in enumerate(paths, start=1):
            file_record = self._get_or_create_record(audio_path, "audiobook")
            file_record.state = FileState.PROCESSING
            self._store.upsert(file_record)

            last_record = self._handle_pending_part(
                audio_path,
                file_record,
                part_num=idx,
                total_parts=total_parts,
                group_key=group_key,
            )

        # last_record is always set: paths is non-empty (len(paths) > 1 here)
        assert last_record is not None
        return last_record

    def _resolve_tag_and_import_audio(
        self,
        m4b_path: Path,
        record: FileRecord,
        original_path: Path,
    ) -> FileRecord:
        """Resolve metadata, embed tags, and import to Calibre."""
        # ── Metadata ──────────────────────────────────────────────────
        # Directive check first, keyed on record.original_path's basename —
        # the ORIGINAL incoming filename, set once before conversion to M4B
        # or multi-part staging could rename the on-disk file. If found,
        # skip resolve_metadata entirely — no Google Books / OpenLibrary /
        # DDG calls.
        result = self._check_directive(Path(record.original_path).name)
        if result is None:
            result = resolve_metadata(
                m4b_path.stem,
                self.config.metadata,
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

        # ── Duplicate check ───────────────────────────────────────────
        dup_record = self._handle_duplicate(record, result, m4b_path)
        if dup_record is not None:
            return dup_record

        # ── Tag ───────────────────────────────────────────────────────
        # Always overwrite here so the final library file carries the resolved
        # metadata for audiobook players (Audiobookshelf, Apple Books).  Note:
        # these tags do NOT affect Calibre's directory structure — calibredb
        # never reads M4B audio tags; the directory comes from the
        # --title/--authors flags passed to add_book below.
        audio_tag.embed_metadata(
            m4b_path,
            result,
            overwrite=True,
            cover_path=result.cover_path if self.config.output.embed_cover_art else None,
        )

        # ── Import ────────────────────────────────────────────────────
        book_id = self._calibre.add_book(m4b_path, **_add_book_args(result))
        record.calibre_book_id = book_id
        log.info("pipeline.audio.imported", extra={"book_id": book_id, "title": result.title})

        # ── Full metadata + cover in Calibre ──────────────────────────
        if result.cover_path:
            self._calibre.set_cover(book_id, result.cover_path)
        self._calibre.set_metadata(book_id, result)

        return self._mark_imported(record, m4b_path, original_path, result)

    # ------------------------------------------------------------------
    # Ebook pipeline
    # ------------------------------------------------------------------

    def _process_ebook(self, path: Path, record: FileRecord) -> FileRecord:
        """Convert (if needed) and import an ebook to Calibre.

        Behaviour is controlled by two config settings:

        ``output.preferred_ebook_format`` (epub | mobi)
            Target format for conversion.

        ``output.ebook_format_policy`` (preferred | all)
            preferred — Convert files not already in the preferred format,
                        import only the converted copy, then delete the
                        original source file.
            all       — Import the file in whatever format it arrived.
                        No conversion is performed; Calibre stores the
                        native format as-is.
        """
        if path.is_dir():
            # Ebook-only directory (no audio files — classifier routed it here).
            # Delegate to the same recursive dispatcher used for audiobook folders
            # so ebook files at any depth are extracted and processed individually.
            return self._process_audiobook_folder(path, record)

        ext = path.suffix.lstrip(".").lower()
        preferred = self.config.output.preferred_ebook_format   # "epub" | "mobi"
        policy = self.config.output.ebook_format_policy         # "preferred" | "all"

        if policy == "all":
            # Import in whatever format arrived — no conversion.
            book_path = path
            log.info(
                "pipeline.ebook.import_as_is",
                extra={"format": ext, "file": str(path)},
            )
        elif ext == preferred:
            # Already in the preferred format — nothing to convert.
            book_path = path
        else:
            # Convert to preferred format; write to staging/ so incoming/ stays clean.
            log.info(
                "pipeline.ebook.converting",
                extra={"from": ext, "to": preferred, "file": str(path)},
            )
            book_path = ebook_conv.to_format(
                path,
                preferred,
                self.config.paths.staging_dir,
                self._calibre,
            )

        # ── Metadata lookup ───────────────────────────────────────────
        # Directive check first: an external tool (e.g. Librarr) may have
        # pre-registered a match for this file under its ORIGINAL incoming
        # basename (record.original_path — set once, before any format
        # conversion above may have renamed/staged book_path). If found,
        # skip resolve_metadata entirely — no Google Books / OpenLibrary /
        # DDG calls.
        result = self._check_directive(Path(record.original_path).name)
        if result is None:
            result = resolve_metadata(
                book_path.stem,
                self.config.metadata,
            )
        record.matched_title = result.title
        record.matched_author = result.author
        record.confidence = result.confidence

        # Whether we created a staging copy (book_path ≠ path).
        # _mark_imported handles both; _mark_review only moves book_path, so
        # we must clean up the original source ourselves in the non-import paths.
        converted = book_path != path

        if not result.above_threshold:
            log.info(
                "pipeline.ebook.low_confidence",
                extra={"confidence": result.confidence, "title": result.title},
            )
            record = self._mark_review(record, result, book_path)
            if converted:
                path.unlink(missing_ok=True)  # delete original PDF/MOBI/etc. from incoming/
            return record

        # ── Duplicate check ───────────────────────────────────────────
        dup_record = self._handle_duplicate(record, result, book_path)
        if dup_record is not None:
            if converted:
                path.unlink(missing_ok=True)  # same cleanup for duplicate path
            return dup_record

        book_id = self._calibre.add_book(book_path, **_add_book_args(result))
        record.calibre_book_id = book_id
        log.info(
            "pipeline.ebook.imported",
            extra={"book_id": book_id, "file": str(book_path), "format": book_path.suffix},
        )

        # ── Full metadata + cover in Calibre ──────────────────────────
        if result.cover_path:
            self._calibre.set_cover(book_id, result.cover_path)
        self._calibre.set_metadata(book_id, result)

        # _mark_imported deletes book_path (staging converted copy) if it
        # differs from path (source), then deletes path (source).  In
        # "preferred" mode this cleans up both.  In "all" mode book_path==path
        # so only the source is removed (which is normal post-import cleanup).
        return self._mark_imported(record, book_path, path, result)

    # ------------------------------------------------------------------
    # State transition helpers
    # ------------------------------------------------------------------

    def _mark_imported(
        self,
        record: FileRecord,
        processed_path: Path,
        original_path: Path,
        result: MetadataResult | None,
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
        # Low-priority ntfy ping on successful import (Notifier is a no-op when
        # notifications are disabled or no topic is configured).
        self._notifier.send_imported_alert(record, result)
        # Tell calibre-web to reopen its DB connection (no-op when unset)
        notify_reconnect(self.config.calibre.reconnect_url)
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
        _apply_metadata_to_record(record, result)
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

    def _check_directive(self, original_filename: str) -> MetadataResult | None:
        """Look up a directive keyed on the ORIGINAL incoming basename.

        Matching is keyed on the basename the external tool wrote into
        incoming_dir — record.original_path, set once in _make_record at
        first sight of the file — NOT on any later staged/converted/renamed
        filename, since conversion (ebook format conversion, audio-to-M4B,
        multi-part staging) can change the on-disk name before _process_*
        gets to call resolve_metadata.

        Returns a pre-resolved MetadataResult (above_threshold=True) and
        marks the directive consumed, or None if no directive matches.
        """
        row = self._store.find_directive(original_filename)
        if row is None:
            return None
        result = _result_from_directive(row)
        self._store.mark_directive_consumed(row["id"])
        log.info(
            "pipeline.directive_match",
            extra={
                "incoming_filename": original_filename,
                "source": row["source"],
                "title": result.title,
            },
        )
        log.info("directive match from %s: %s", row["source"], result.title)

        # Best-effort cover download — same helper resolve_metadata uses.
        cover_url = result.best.candidate.cover_url if result.best else None
        if cover_url and self.config.output.embed_cover_art and not self.config.metadata.mock_mode:
            import httpx

            from ._constants import HTTP_TIMEOUT_API
            from .metadata.base import USER_AGENT
            from .metadata.resolver import _download_cover
            try:
                with httpx.Client(timeout=HTTP_TIMEOUT_API, headers={"User-Agent": USER_AGENT}) as client:
                    result.cover_path = _download_cover(cover_url, client)
            except Exception:
                log.warning("pipeline.directive_cover_failed", extra={"url": cover_url})

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_calibre_duplicates(self, title: str, author: str) -> list[int]:
        """Search Calibre for existing books matching title and author.

        Uses calibredb's exact-title + author-surname search.  Returns a list
        of matching Calibre book IDs, or [] when no duplicates are found or if
        the search fails for any reason (errors are swallowed — dedup is best-
        effort and should never crash the pipeline).
        """
        if not title:
            return []
        # Escape double-quotes so the query is valid for calibredb
        safe_title = title.replace('"', '\\"')
        # "=" prefix = exact match (case-insensitive) in calibredb search
        parts = [f'title:"={safe_title}"']
        if author:
            # Match on the last token of the author name (surname) for flexibility.
            # e.g. "Christopher Paolini" → searches authors:"Paolini"
            surname = author.strip().split()[-1].replace('"', '\\"')
            if surname:
                parts.append(f'authors:"{surname}"')
        query = " and ".join(parts)
        try:
            ids: list[int] = list(self._calibre.search(query))
        except Exception as exc:
            log.warning("pipeline.duplicate_check_failed: %s", exc)
            return []

        if ids:
            return ids

        # Secondary: contains-mode title search (no "=" prefix).
        # Catches series-prefix mismatches in both directions:
        #   • library has "Pendragon: The Merchant Of Death", incoming is
        #     "The Merchant of Death" — the bare title is a substring of the
        #     library entry so the contains search finds it.
        #   • library has "The Merchant of Death", incoming is "Pendragon: …"
        #     — the stripped bare title matches exactly.
        # Strip "Series: " prefix first so the contains query is as specific
        # as possible and avoids spurious matches.
        bare = (title.split(": ", 1)[1] if ": " in title else title).replace('"', '\\"')
        parts2 = [f'title:"{bare}"']   # no "=" → calibredb contains search
        if author and surname:
            parts2.append(f'authors:"{surname}"')
        try:
            ids = list(self._calibre.search(" and ".join(parts2)))
        except Exception as exc:
            log.warning("pipeline.duplicate_check_series_strip_failed: %s", exc)

        return ids

    def _find_calibre_duplicates_by_title(self, title: str) -> list[int]:
        """Search Calibre for existing books by exact title only (no author).

        Mirrors calibredb's own add-time duplicate detection, which matches on
        title alone.  Used as a fallback after calibredb rejects an add as a
        duplicate that the author-aware _find_calibre_duplicates() missed.
        Returns [] on any failure — best-effort, never crashes the pipeline.
        """
        if not title:
            return []
        safe_title = title.replace('"', '\\"')
        try:
            return list(self._calibre.search(f'title:"={safe_title}"'))
        except Exception as exc:
            log.warning("pipeline.duplicate_check_by_title_failed: %s", exc)
            return []

    def _find_fuzzy_duplicates(self, title: str, author: str) -> list[dict]:
        """Return Calibre books that are near-matches (85–99% similarity) but not exact.

        Uses rapidfuzz token_sort_ratio on the combined title+author string.
        Scores of 100 are exact matches already handled by _find_calibre_duplicates;
        scores below 85 are too dissimilar to show. Returns [] on any error —
        fuzzy check is best-effort and should never crash the pipeline.
        """
        if not title:
            return []
        try:
            query = f"{title} {author or ''}".lower().strip()
            results = []
            for book in self._calibre.list_books():
                authors_str = " ".join(book.get("authors") or [])
                lib_q = f"{book.get('title', '')} {authors_str}".lower().strip()
                score = _fuzz.token_sort_ratio(query, lib_q)
                if 85 <= score < 100:
                    results.append({**book, "similarity": score})
            return sorted(results, key=lambda b: b["similarity"], reverse=True)
        except Exception as exc:
            log.warning("pipeline.fuzzy_check_failed: %s", exc)
            return []

    def _handle_duplicate(
        self,
        record: FileRecord,
        result: MetadataResult,
        file_path: Path,
    ) -> FileRecord | None:
        """Check for duplicates and act on them per config.

        If the incoming file's format is not already stored in the matching
        Calibre book, it is added as a new format to that record regardless
        of duplicate_action — so an EPUB and M4B of the same book end up
        in one Calibre entry.

        When the same format already exists:
          import  → replace the existing format + update metadata (never creates
                    a second Calibre entry)
          skip    → discard the file silently
          review  → move to review/ so the user can decide

        Returns a FileRecord if the duplicate was handled (caller should return
        it immediately); returns None to continue with normal import.
        """
        if self.config.metadata.mock_mode:
            return None

        action = self.config.metadata.duplicate_action
        dup_ids = self._find_calibre_duplicates(result.title, result.author)
        if not dup_ids:
            return None

        # ── Format-merge check ────────────────────────────────────────
        # If the incoming format isn't already in the matched Calibre book,
        # add it there rather than treating it as a duplicate.
        incoming_fmt = file_path.suffix.lstrip(".").lower()
        try:
            existing_formats = self._calibre.get_formats(dup_ids[0])
        except Exception as exc:
            log.warning("pipeline.get_formats_failed: %s", exc)
            existing_formats = set()

        if incoming_fmt not in existing_formats:
            # Different format: merge into the existing record regardless of
            # duplicate_action.  Also refresh cover + metadata on the existing
            # record so it benefits from the freshly resolved API data.
            # Audio formats are not supported by calibredb add_format — return
            # None so the caller proceeds with add_book and creates a separate
            # Calibre entry (e.g. EPUB already in library, M4B comes in).
            _is_audio = incoming_fmt in audio_conv.AUDIO_EXTENSIONS
            if incoming_fmt and not _is_audio:
                try:
                    self._calibre.add_format(dup_ids[0], file_path)
                    file_path.unlink(missing_ok=True)
                    if result.cover_path:
                        self._calibre.set_cover(dup_ids[0], result.cover_path)
                    self._calibre.set_metadata(dup_ids[0], result)
                    if result.cover_path:
                        result.cover_path.unlink(missing_ok=True)
                    record.state = FileState.IMPORTED
                    record.calibre_book_id = dup_ids[0]
                    record.matched_title = result.title
                    record.matched_author = result.author
                    record.confidence = result.confidence
                    record.error_msg = f"Added {incoming_fmt.upper()} format to Calibre book {dup_ids[0]}"
                    self._store.upsert(record)
                    log.info(
                        "pipeline.format_merged",
                        extra={
                            "title": result.title,
                            "format": incoming_fmt,
                            "book_id": dup_ids[0],
                        },
                    )
                    return record
                except Exception as exc:
                    log.warning("pipeline.add_format_failed: %s", exc)
                    # Fall through to normal duplicate handling
            else:
                # Audio format or no extension: can't merge via add_format.
                # Let the caller use add_book to create a separate Calibre entry.
                log.debug(
                    "pipeline.skip_add_format_audio",
                    extra={"fmt": incoming_fmt or "(none)", "dup_id": dup_ids[0]},
                )
                return None

        # ── Same format already in Calibre — apply duplicate_action ───
        id_str = ", ".join(str(i) for i in dup_ids[:3])
        suffix = f" (and {len(dup_ids) - 3} more)" if len(dup_ids) > 3 else ""
        dup_msg = f"Duplicate: already in Calibre as {incoming_fmt.upper()} (ID{'s' if len(dup_ids) > 1 else ''}: {id_str}{suffix})"

        log.info(
            "pipeline.duplicate_detected",
            extra={
                "title": result.title,
                "calibre_ids": dup_ids,
                "action": action,
            },
        )

        if action == "import":
            # Replace the existing format and refresh metadata.  "import" means
            # never skip/review — always merge into the existing record.
            try:
                self._calibre.add_format(dup_ids[0], file_path)
                file_path.unlink(missing_ok=True)
                if result.cover_path:
                    self._calibre.set_cover(dup_ids[0], result.cover_path)
                self._calibre.set_metadata(dup_ids[0], result)
                if result.cover_path:
                    result.cover_path.unlink(missing_ok=True)
                record.state = FileState.IMPORTED
                record.calibre_book_id = dup_ids[0]
                record.matched_title = result.title
                record.matched_author = result.author
                record.confidence = result.confidence
                record.error_msg = f"Replaced {incoming_fmt.upper()} format in Calibre book {dup_ids[0]}"
                self._store.upsert(record)
                log.info(
                    "pipeline.format_replaced",
                    extra={"title": result.title, "book_id": dup_ids[0]},
                )
                return record
            except Exception as exc:
                log.warning("pipeline.replace_format_failed: %s", exc)
                # Fall through — add_book will create a new entry as last resort

        if action == "skip":
            # Discard the file; mark IMPORTED so it won't be re-processed
            file_path.unlink(missing_ok=True)
            if result.cover_path:
                result.cover_path.unlink(missing_ok=True)
            record.state = FileState.IMPORTED
            record.matched_title = result.title
            record.matched_author = result.author
            record.confidence = result.confidence
            record.error_msg = dup_msg
            self._store.upsert(record)
            log.info("pipeline.duplicate_skipped", extra={"title": result.title})
            return record

        # action == "review": send to review/ so the user can decide
        dest = self.config.paths.review_dir / file_path.name
        _safe_move(file_path, dest)
        record.current_path = str(dest)
        record.state = FileState.REVIEW
        record.matched_title = result.title
        record.matched_author = result.author
        record.confidence = result.confidence
        record.matched_year = int(result.year) if result.year else None
        record.matched_publisher = result.publisher or None
        record.matched_isbn = result.isbn
        record.matched_cover_url = result.best.candidate.cover_url if result.best else None
        record.matched_metadata_json = _serialize_candidate(result.best) if result.best else None
        record.error_msg = dup_msg
        if result.cover_path:
            result.cover_path.unlink(missing_ok=True)
        self._store.upsert(record)
        self._notifier.send_review_alert(record, result)
        return record

    def _scan_incoming(self, reason: str = "periodic") -> None:
        """Process every file/directory currently in incoming_dir.

        Safe to call at any time — the dedup check in _handle_event skips
        anything already in IMPORTED, PROCESSING, or PENDING_PARTS state.
        Hidden files (dot-prefixed) are ignored.
        """
        purged = self._store.purge_stale_directives(48)
        if purged:
            log.info("pipeline.directives_purged", extra={"count": purged})

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
