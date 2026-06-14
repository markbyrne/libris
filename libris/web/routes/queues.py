"""Queue view routes — review, failed, pending — plus inline action endpoints."""

from __future__ import annotations

import html as _html
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._common import base_ctx, templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _row_removed() -> HTMLResponse:
    """Empty response — HTMX outerHTML swap removes the row from the DOM."""
    return HTMLResponse("")


def _row_error(record_id: str, msg: str, colspan: int = 7) -> HTMLResponse:
    """Replacement <tr> showing an inline error after a failed action."""
    safe = _html.escape(msg)
    html = (
        f'<tr id="row-{record_id}" class="bg-red-50">'
        f'<td colspan="{colspan}" class="px-4 py-2 text-xs text-red-600 font-medium">'
        f'&#x2717; {safe}</td></tr>'
    )
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

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
            r._has_match = bool(r.matched_metadata_json)  # type: ignore[attr-defined]
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


# ---------------------------------------------------------------------------
# Action routes — review queue
# ---------------------------------------------------------------------------

@router.post("/api/review/{record_id}/discard", response_class=HTMLResponse)
def api_review_discard(record_id: str, request: Request):
    """Delete a review-queue file and mark it as discarded."""
    from ...config import load_config
    from ...state import FileState, StateStore

    config_path = request.app.state.config_path
    try:
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(record_id)
        if not record:
            store.close()
            return _row_error(record_id, "Record not found")
        name = Path(record.current_path).name
        Path(record.current_path).unlink(missing_ok=True)
        record.state = FileState.IMPORTED
        record.error_msg = f"Discarded via web UI (was: review/{name})"
        store.upsert(record)
        store.close()
        return _row_removed()
    except Exception as exc:
        return _row_error(record_id, str(exc))


@router.post("/api/review/{record_id}/accept", response_class=HTMLResponse)
def api_review_accept(record_id: str, request: Request):
    """Force-import a review-queue file using its cached metadata."""
    from ...config import load_config
    from ...pipeline import Pipeline
    from ...state import FileState, StateStore

    config_path = request.app.state.config_path
    try:
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(record_id)
        store.close()
        if not record:
            return _row_error(record_id, "Record not found")
        if not record.matched_metadata_json:
            return _row_error(
                record_id,
                "No metadata match yet — rematch via CLI: libris rematch --id N",
            )
        # Force through any duplicate check — user explicitly chose to accept
        cfg.metadata.duplicate_action = "import"
        pipeline = Pipeline(cfg)
        try:
            result = pipeline.import_from_record(record)
        finally:
            try:
                pipeline._store.close()
            except Exception:
                pass
        if result.state == FileState.IMPORTED:
            return _row_removed()
        return _row_error(
            record_id,
            result.error_msg or f"Unexpected state after import: {result.state.value}",
        )
    except Exception as exc:
        return _row_error(record_id, str(exc))


# ---------------------------------------------------------------------------
# Action routes — failed queue
# ---------------------------------------------------------------------------

@router.post("/api/failed/{record_id}/recover", response_class=HTMLResponse)
def api_failed_recover(record_id: str, request: Request):
    """Move a failed file back to review/ for re-processing."""
    from ...config import load_config
    from ...state import FileState, StateStore

    config_path = request.app.state.config_path
    try:
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(record_id)
        if not record:
            store.close()
            return _row_error(record_id, "Record not found", colspan=6)
        current = Path(record.current_path)
        if not current.exists():
            store.close()
            return _row_error(record_id, "File missing from disk — use Remove", colspan=6)
        review_dir = cfg.paths.review_dir
        review_dir.mkdir(parents=True, exist_ok=True)
        dest = review_dir / current.name
        if dest.exists():
            dest = review_dir / f"{current.stem}_recovered{current.suffix}"
        shutil.move(str(current), str(dest))
        record.state = FileState.REVIEW
        record.current_path = str(dest)
        record.error_msg = None
        store.upsert(record)
        store.close()
        return _row_removed()
    except Exception as exc:
        return _row_error(record_id, str(exc), colspan=6)


@router.post("/api/failed/{record_id}/remove", response_class=HTMLResponse)
def api_failed_remove(record_id: str, request: Request):
    """Permanently delete a failed file and remove its record."""
    from ...config import load_config
    from ...state import FileState, StateStore

    config_path = request.app.state.config_path
    try:
        cfg = load_config(config_path)
        store = StateStore(cfg.paths.state_db)
        record = store.get(record_id)
        if not record:
            store.close()
            return _row_error(record_id, "Record not found", colspan=6)
        Path(record.current_path).unlink(missing_ok=True)
        record.state = FileState.IMPORTED
        record.error_msg = "Removed via web UI"
        store.upsert(record)
        store.close()
        return _row_removed()
    except Exception as exc:
        return _row_error(record_id, str(exc), colspan=6)
