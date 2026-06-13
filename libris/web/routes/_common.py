"""Shared helpers for web route handlers."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _queue_counts(config_path: Path) -> dict[str, int]:
    """Return live review/failed/pending counts for the nav badges."""
    from ...config import load_config
    from ...state import FileState, StateStore

    try:
        config = load_config(config_path)
        store = StateStore(config.paths.state_db)
        counts = {
            "review": len(store.list_by_state(FileState.REVIEW)),
            "failed": len(store.list_by_state(FileState.FAILED)),
            "pending": len(store.list_by_state(FileState.PENDING_PARTS)),
        }
        store.close()
    except Exception:
        counts = {"review": 0, "failed": 0, "pending": 0}
    return counts


def base_ctx(request: Request, active_page: str) -> dict:
    """Build the template context dict shared by every page."""
    config_path: Path = request.app.state.config_path
    return {
        "active_page": active_page,
        "counts": _queue_counts(config_path),
        "config_file": config_path.name,
    }
