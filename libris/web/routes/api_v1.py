"""Directive API — lets an external tool (e.g. Librarr) pre-register the
correct metadata match for an incoming file, so the pipeline skips its own
Google Books / OpenLibrary / DDG lookups and imports with the directed
metadata instead. Also exposes GET /api/v1/config so a tool like Librarr can
auto-detect Libris's incoming_dir/state_db/review_dir instead of the user
hand-typing paths from another machine, and POST /api/v1/imports so a tool
that already has files on disk (in Libris's view) can drive an import
directly instead of dropping them into incoming_dir for the watcher to find.

Security posture: disabled by default (config.api.enabled must be True AND
config.api.api_key must be non-empty) — fails closed. Every route requires
a matching ``X-Api-Key`` header, compared with hmac.compare_digest.

The stored ``metadata_json`` blob uses the same field shape as
``pipeline._serialize_candidate`` (title/authors/isbn_13/isbn_10/
published_year/publisher/description/language/series/series_index/
cover_url/categories/source/confidence) so the pipeline seam can build a
ScoredCandidate/MetadataResult the same way review-accept reconstructs one
from ``matched_metadata_json``.
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from ...classifier import AUDIO_EXTENSIONS, EBOOK_EXTENSIONS

router = APIRouter(prefix="/api/v1")

_ISBN_RE = re.compile(r"^[\d-]{10,13}$")

# Serializes concurrent /imports requests — Pipeline itself is not meant to
# run more than one import at a time from independent, short-lived instances
# (unlike the daemon's single long-lived Pipeline, each API import builds
# its own). One lock, held for the duration of one import_file_list() call,
# keeps two concurrent API-driven imports from racing on the same Calibre
# library / state DB.
#
# ponytail: process-wide lock, not per-library — fine for a single Libris
# process serving one library. Multi-library or multi-worker deployments
# would need a lock keyed on state_db path instead.
_import_lock = threading.Lock()


class DirectiveIn(BaseModel):
    filename: str
    title: str
    author: str | None = None
    isbn: str | None = None
    year: int | None = None
    media_type: str | None = None
    series: str | None = None
    series_index: float | None = None
    cover_url: str | None = None
    description: str | None = None
    publisher: str | None = None
    language: str | None = None
    source: str = "external"
    confidence: float = 1.0

    @field_validator("filename")
    @classmethod
    def _filename_is_bare_basename(cls, v: str) -> str:
        if not v or "/" in v or "\\" in v or v in (".", "..") :
            raise ValueError("filename must be a bare basename (no path separators)")
        return v

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must be non-empty")
        return v

    @field_validator("isbn")
    @classmethod
    def _isbn_format(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _ISBN_RE.match(v):
            raise ValueError("isbn must be 10-13 digits (hyphens allowed)")
        return v

    @field_validator("media_type")
    @classmethod
    def _media_type_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in ("ebook", "audiobook"):
            raise ValueError("media_type must be 'ebook' or 'audiobook'")
        return v


class ImportMetadataIn(BaseModel):
    """Metadata for a direct import — same field set as DirectiveIn minus
    ``filename`` (imports are keyed on the posted ``files`` list instead),
    plus series/publisher/description/language which a caller with richer
    metadata (e.g. Librarr, which already resolved a match) can supply.
    """

    title: str
    author: str | None = None
    isbn: str | None = None
    year: int | None = None
    series: str | None = None
    series_index: float | None = None
    publisher: str | None = None
    description: str | None = None
    language: str | None = None
    cover_url: str | None = None
    media_type: str = "ebook"

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must be non-empty")
        return v

    @field_validator("isbn")
    @classmethod
    def _isbn_format(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _ISBN_RE.match(v):
            raise ValueError("isbn must be 10-13 digits (hyphens allowed)")
        return v

    @field_validator("media_type")
    @classmethod
    def _media_type_valid(cls, v: str) -> str:
        if v not in ("ebook", "audiobook"):
            raise ValueError("media_type must be 'ebook' or 'audiobook'")
        return v


class ImportIn(BaseModel):
    """POST /api/v1/imports body — a direct, caller-driven import of one or
    more files already on disk (in Libris's view), skipping incoming_dir
    entirely. Mirrors Pipeline.import_file_list's contract: a single path is
    a complete book; multiple paths are sequential parts of ONE multi-part
    audiobook and must all be audio files, primary (files[0]) first.
    """

    files: list[str]
    metadata: ImportMetadataIn
    source: str = "external"
    confidence: float = 1.0

    @field_validator("files")
    @classmethod
    def _files_valid(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("files must be non-empty")

        paths = [Path(f) for f in v]
        for p in paths:
            if not p.is_absolute():
                raise ValueError(f"files entries must be absolute paths: {p}")
            if p.is_symlink():
                raise ValueError(f"files entries must not be symlinks: {p}")
            if not p.exists():
                raise ValueError(f"file does not exist: {p}")
            if not p.is_file():
                raise ValueError(f"files entries must be regular files: {p}")
            ext = p.suffix.lstrip(".").lower()
            if ext not in EBOOK_EXTENSIONS and ext not in AUDIO_EXTENSIONS:
                raise ValueError(f"unsupported file extension: {p}")

        if len(paths) > 1:
            for p in paths:
                ext = p.suffix.lstrip(".").lower()
                if ext not in AUDIO_EXTENSIONS:
                    raise ValueError(
                        f"multi-file import requires all-audio input, "
                        f"got non-audio file {p}"
                    )

        return v


def _check_auth(request: Request, x_api_key: str | None) -> JSONResponse | None:
    """Return an error JSONResponse if auth fails, else None."""
    from ...config import load_config

    config_path = request.app.state.config_path
    cfg = load_config(config_path)
    if not cfg.api.enabled or not cfg.api.api_key:
        return JSONResponse({"detail": "Directive API is disabled"}, status_code=403)
    if not x_api_key or not hmac.compare_digest(x_api_key, cfg.api.api_key):
        return JSONResponse({"detail": "Invalid or missing X-Api-Key"}, status_code=401)
    return None


@router.get("/ping")
def ping(request: Request, x_api_key: str | None = Header(default=None)):
    from ... import __version__

    err = _check_auth(request, x_api_key)
    if err:
        return err
    return {"ok": True, "version": __version__}


@router.get("/config")
def get_config(request: Request, x_api_key: str | None = Header(default=None)):
    """Expose the paths Librarr needs to auto-detect its own settings
    (incoming_dir/state_db) instead of the user hand-typing them from
    another machine.
    """
    from ... import __version__
    from ...config import load_config

    err = _check_auth(request, x_api_key)
    if err:
        return err

    config_path = request.app.state.config_path
    cfg = load_config(config_path)
    return {
        "incoming_dir": str(cfg.watcher.incoming_dir),
        "state_db": str(cfg.paths.state_db),
        "review_dir": str(cfg.paths.review_dir),
        "version": __version__,
    }


@router.post("/directives", status_code=201)
async def create_directive(
    request: Request, x_api_key: str | None = Header(default=None)
):
    from ...config import load_config
    from ...state import StateStore

    err = _check_auth(request, x_api_key)
    if err:
        return err

    try:
        body = await request.json()
        directive = DirectiveIn(**body)
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    metadata = {
        "title": directive.title,
        "authors": [directive.author] if directive.author else [],
        "isbn_13": directive.isbn if directive.isbn and len(re.sub(r"-", "", directive.isbn)) == 13 else None,
        "isbn_10": directive.isbn if directive.isbn and len(re.sub(r"-", "", directive.isbn)) == 10 else None,
        "published_year": directive.year,
        "publisher": directive.publisher,
        "description": directive.description,
        "language": directive.language,
        "series": directive.series,
        "series_index": directive.series_index,
        "cover_url": directive.cover_url,
        "categories": [],
        "source": directive.source,
        "confidence": directive.confidence,
        "score_breakdown": {},
        "media_type": directive.media_type,
    }

    config_path = request.app.state.config_path
    cfg = load_config(config_path)
    store = StateStore(cfg.paths.state_db)
    try:
        directive_id = str(uuid.uuid4())
        store.add_directive(
            directive_id=directive_id,
            filename=directive.filename,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            source=directive.source,
            confidence=directive.confidence,
        )
    finally:
        store.close()

    return JSONResponse({"id": directive_id, "status": "registered"}, status_code=201)


def _run_import(config_path, files: list[str]) -> None:
    """Background task body for POST /imports — runs after the 202 response
    has already been sent. Never lets an exception escape: a failed import
    just leaves the pre-created FileRecord in whatever state Pipeline left
    it (or its pre-created INCOMING state, if the crash happened before
    Pipeline touched it at all) — poll_timeout/orphan handling on the next
    daemon pass picks it up like any other stuck file.
    """
    import logging

    from ...config import load_config
    from ...pipeline import Pipeline

    log = logging.getLogger("libris.api")

    with _import_lock:
        pipeline = None
        try:
            pipeline = Pipeline(load_config(config_path))
            pipeline.import_file_list([Path(p) for p in files])
        except Exception:
            log.exception("api.import_failed", extra={"files": files})
        finally:
            if pipeline is not None:
                try:
                    pipeline._store.close()
                except Exception:
                    pass


@router.post("/imports", status_code=202)
async def create_import(
    request: Request,
    background_tasks: BackgroundTasks,
    x_api_key: str | None = Header(default=None),
):
    """Direct-import endpoint: a caller (e.g. Librarr) that already has files
    on disk — in Libris's view — and already knows the correct metadata
    posts them here instead of dropping them into incoming_dir for the
    watcher to discover blind. Always returns 202 immediately; the actual
    import (Pipeline.import_file_list) runs in a background task after the
    response is sent, exactly like /directives lets the *next* watcher pass
    pick up the metadata — except here Libris also does the importing.
    """
    from ...config import load_config
    from ...state import FileRecord, FileState, StateStore

    err = _check_auth(request, x_api_key)
    if err:
        return err

    try:
        body = await request.json()
        imp = ImportIn(**body)
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    metadata = {
        "title": imp.metadata.title,
        "authors": [imp.metadata.author] if imp.metadata.author else [],
        "isbn_13": imp.metadata.isbn if imp.metadata.isbn and len(re.sub(r"-", "", imp.metadata.isbn)) == 13 else None,
        "isbn_10": imp.metadata.isbn if imp.metadata.isbn and len(re.sub(r"-", "", imp.metadata.isbn)) == 10 else None,
        "published_year": imp.metadata.year,
        "publisher": imp.metadata.publisher,
        "description": imp.metadata.description,
        "language": imp.metadata.language,
        "series": imp.metadata.series,
        "series_index": imp.metadata.series_index,
        "cover_url": imp.metadata.cover_url,
        "categories": [],
        "source": imp.source,
        "confidence": imp.confidence,
        "score_breakdown": {},
        "media_type": imp.metadata.media_type,
    }

    primary = Path(imp.files[0])
    filename = primary.name

    config_path = request.app.state.config_path
    cfg = load_config(config_path)
    store = StateStore(cfg.paths.state_db)
    try:
        directive_id = str(uuid.uuid4())
        store.add_directive(
            directive_id=directive_id,
            filename=filename,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
            source=imp.source,
            confidence=imp.confidence,
        )

        record_id = FileRecord.make_id(primary)
        record = store.get(record_id) or store.get_by_current_path(str(primary))
        if record is None:
            record = FileRecord(
                id=record_id,
                original_path=str(primary),
                current_path=str(primary),
                media_type=imp.metadata.media_type,
                state=FileState.INCOMING,
            )
            store.upsert(record)
    finally:
        store.close()

    background_tasks.add_task(_run_import, config_path, imp.files)

    return JSONResponse({"id": record.id, "status": "accepted"}, status_code=202)


@router.get("/files")
def get_file(
    request: Request,
    filename: str,
    x_api_key: str | None = Header(default=None),
):
    from ...config import load_config
    from ...state import StateStore

    err = _check_auth(request, x_api_key)
    if err:
        return err

    config_path = request.app.state.config_path
    cfg = load_config(config_path)
    store = StateStore(cfg.paths.state_db)
    try:
        record = store.get_by_current_path(filename) or store.get_by_path(filename)
    finally:
        store.close()

    if not record:
        return JSONResponse({"detail": "No matching file record"}, status_code=404)

    return {
        "id": record.id,
        "original_path": record.original_path,
        "current_path": record.current_path,
        "media_type": record.media_type,
        "state": record.state.value,
        "confidence": record.confidence,
        "matched_title": record.matched_title,
        "matched_author": record.matched_author,
        "matched_year": record.matched_year,
        "matched_isbn": record.matched_isbn,
        "calibre_book_id": record.calibre_book_id,
    }
