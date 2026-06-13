"""Config editor routes."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ._common import base_ctx, templates

router = APIRouter()


@router.get("/", response_class=RedirectResponse)
def index():
    return RedirectResponse(url="/review")


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    from ...config import load_config

    config_path = request.app.state.config_path
    ctx = base_ctx(request, "config")
    try:
        ctx["config"] = load_config(config_path)
        ctx["config_error"] = None
    except Exception as exc:
        ctx["config"] = None
        ctx["config_error"] = str(exc)
    return templates.TemplateResponse(request, "config.html", ctx)
