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

    from .routes import config as config_routes
    from .routes import queues as queue_routes

    app.include_router(config_routes.router)
    app.include_router(queue_routes.router)

    return app
