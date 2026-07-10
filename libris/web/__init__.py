"""Libris web UI — FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent


def create_app(config_path: Path):
    """Create and configure the FastAPI application."""
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="Libris", docs_url=None, redoc_url=None)
    app.state.config_path = config_path

    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    from . import log_buffer

    try:
        from ..config import load_config

        log_level = load_config(config_path).log_level
    except Exception:
        log_level = "INFO"
    log_buffer.install(log_level)

    from .routes import api_v1
    from .routes import config as config_routes
    from .routes import logs as log_routes
    from .routes import queues as queue_routes

    app.include_router(config_routes.router)
    app.include_router(queue_routes.router)
    app.include_router(log_routes.router)
    app.include_router(api_v1.router)

    return app
