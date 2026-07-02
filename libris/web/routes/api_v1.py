"""Directive API — lets an external tool (e.g. Librarr) pre-register the
correct metadata match for an incoming file, so the pipeline skips its own
Google Books / OpenLibrary / DDG lookups and imports with the directed
metadata instead. Also exposes GET /api/v1/config so a tool like Librarr can
auto-detect Libris's incoming_dir/state_db/review_dir instead of the user
hand-typing paths from another machine.

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
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

router = APIRouter(prefix="/api/v1")

_ISBN_RE = re.compile(r"^[\d-]{10,13}$")


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
