"""Logs view routes — browse the in-process ring-buffer of recent log
records without needing journald or a log file path (see ``log_buffer``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..log_buffer import get_records
from ._common import base_ctx, templates

router = APIRouter()

_VALID_LEVELS = {"INFO", "WARNING", "ERROR"}
_ROW_LIMIT = 500


def _clean_level(level: str | None) -> str | None:
    if level and level.upper() in _VALID_LEVELS:
        return level.upper()
    return None


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, level: str | None = None, q: str | None = None):
    ctx = base_ctx(request, "logs")
    ctx["level"] = _clean_level(level) or ""
    ctx["q"] = q or ""
    ctx["records"] = get_records(level=ctx["level"] or None, search=q or None, limit=_ROW_LIMIT)
    return templates.TemplateResponse(request, "logs.html", ctx)


@router.get("/logs/rows", response_class=HTMLResponse)
def logs_rows(request: Request, level: str | None = None, q: str | None = None):
    clean_level = _clean_level(level)
    ctx = {
        "records": get_records(level=clean_level, search=q or None, limit=_ROW_LIMIT),
        "level": clean_level or "",
        "q": q or "",
    }
    return templates.TemplateResponse(request, "partials/log_rows.html", ctx)
