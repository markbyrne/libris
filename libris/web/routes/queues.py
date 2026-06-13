"""Queue view routes — review, failed, pending."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._common import base_ctx, templates

router = APIRouter()


def _fmt_age(dt: datetime) -> str:
    """Human-readable age string for a record timestamp."""
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    total = int((datetime.now(timezone.utc) - dt).total_seconds())
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        h, m = divmod(total // 60, 60)
        return f"{h}h {m}m"
    d, rem = divmod(total, 86400)
    return f"{d}d {rem // 3600}h"


def _open_store(config_path: Path):
    from ...config import load_config
    from ...state import StateStore

    config = load_config(config_path)
    return StateStore(config.paths.state_db)


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request):
    from ...state import FileState

    config_path = request.app.state.config_path
    ctx = base_ctx(request, "review")
    try:
        store = _open_store(config_path)
        all_records = store.list_by_state(FileState.REVIEW)
        live = [r for r in all_records if Path(r.current_path).exists()]
        stale_count = len(all_records) - len(live)
        for r in live:
            r._age = _fmt_age(r.created_at)  # type: ignore[attr-defined]
            r._is_dup = bool(r.error_msg and r.error_msg.startswith("Duplicate:"))  # type: ignore[attr-defined]
        store.close()
        ctx["records"] = live
        ctx["stale_count"] = stale_count
        ctx["error"] = None
    except Exception as exc:
        ctx["records"] = []
        ctx["stale_count"] = 0
        ctx["error"] = str(exc)
    return templates.TemplateResponse(request, "review.html", ctx)


@router.get("/failed", response_class=HTMLResponse)
def failed_page(request: Request):
    from ...state import FileState

    config_path = request.app.state.config_path
    ctx = base_ctx(request, "failed")
    try:
        store = _open_store(config_path)
        records = store.list_by_state(FileState.FAILED)
        for r in records:
            r._age = _fmt_age(r.created_at)  # type: ignore[attr-defined]
            r._exists = Path(r.current_path).exists()  # type: ignore[attr-defined]
        store.close()
        ctx["records"] = records
        ctx["error"] = None
    except Exception as exc:
        ctx["records"] = []
        ctx["error"] = str(exc)
    return templates.TemplateResponse(request, "failed.html", ctx)


@router.get("/pending", response_class=HTMLResponse)
def pending_page(request: Request):
    config_path = request.app.state.config_path
    ctx = base_ctx(request, "pending")
    try:
        store = _open_store(config_path)
        groups = store.list_pending_groups()
        group_list = []
        for key, parts in groups.items():
            have = {p.part_num for p in parts if p.part_num}
            total = parts[0].total_parts if parts else None
            missing = (
                [n for n in range(1, total + 1) if n not in have]
                if total else []
            )
            oldest = min(p.created_at for p in parts)
            group_list.append({
                "key": key,
                "parts": parts,
                "have": sorted(have),
                "missing": missing,
                "total": total,
                "age": _fmt_age(oldest),
                "title": parts[0].matched_title or Path(parts[0].current_path).stem,
                "author": parts[0].matched_author,
            })
        store.close()
        ctx["groups"] = group_list
        ctx["error"] = None
    except Exception as exc:
        ctx["groups"] = []
        ctx["error"] = str(exc)
    return templates.TemplateResponse(request, "pending.html", ctx)
