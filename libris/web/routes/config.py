"""Config editor routes."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml
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
    ctx["save_success"] = request.query_params.get("saved") == "1"
    try:
        ctx["config"] = load_config(config_path)
        ctx["config_error"] = None
    except Exception as exc:
        ctx["config"] = None
        ctx["config_error"] = str(exc)
    return templates.TemplateResponse(request, "config.html", ctx)


@router.post("/config", response_class=HTMLResponse)
async def config_save(request: Request):
    from ...config import load_config

    config_path = request.app.state.config_path
    form = await request.form()

    def _get(key: str, default: str = "") -> str:
        return str(form.get(key, default)).strip()

    def _opt(key: str) -> str | None:
        v = _get(key)
        return v or None

    def _bool(key: str) -> bool:
        return key in form

    def _float(key: str, default: float) -> float:
        try:
            return float(_get(key) or default)
        except ValueError:
            return default

    path_map_raw = _get("calibre.path_map")
    try:
        path_map = yaml.safe_load(path_map_raw) or {}
        if not isinstance(path_map, dict):
            path_map = {}
    except Exception:
        path_map = {}

    data: dict = {
        "watcher": {
            "incoming_dir": _get("watcher.incoming_dir"),
            "poll_interval_seconds": _float("watcher.poll_interval_seconds", 2.0),
            "scan_interval_hours": _float("watcher.scan_interval_hours", 1.0),
        },
        "paths": {
            "staging_dir": _get("paths.staging_dir"),
            "review_dir": _get("paths.review_dir"),
            "failed_dir": _get("paths.failed_dir"),
            "state_db": _get("paths.state_db"),
        },
        "calibre": {
            "mode": _get("calibre.mode", "local"),
            "library_db_path": _opt("calibre.library_db_path"),
            "book_file_path": _opt("calibre.book_file_path"),
            "docker_container": _get("calibre.docker_container", "calibre-web"),
            "path_map": path_map,
            "reconnect_url": _opt("calibre.reconnect_url"),
        },
        "metadata": {
            "confidence_threshold": _float("metadata.confidence_threshold", 0.75),
            "google_books_api_key": _opt("metadata.google_books_api_key"),
            "mock_mode": _bool("metadata.mock_mode"),
            "overwrite_existing": _bool("metadata.overwrite_existing"),
            "duplicate_action": _get("metadata.duplicate_action", "review"),
        },
        "output": {
            "preferred_ebook_format": _get("output.preferred_ebook_format", "epub"),
            "preferred_audio_format": _get("output.preferred_audio_format", "m4b"),
            "embed_cover_art": _bool("output.embed_cover_art"),
            "ebook_format_policy": _get("output.ebook_format_policy", "preferred"),
        },
        "ntfy": {
            "topic": _get("ntfy.topic"),
            "base_url": _get("ntfy.base_url", "https://ntfy.sh"),
            "enabled": _bool("ntfy.enabled"),
            "auth_token": _opt("ntfy.auth_token"),
        },
        "multipart": {
            "timeout_hours": _float("multipart.timeout_hours", 48.0),
        },
        "api": {
            "enabled": _bool("api.enabled"),
            "api_key": _get("api.api_key"),
        },
        "log_level": _get("log_level", "INFO"),
    }

    ctx = base_ctx(request, "config")
    ctx["save_success"] = False
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, dir=config_path.parent
        ) as tf:
            yaml.safe_dump(data, tf, default_flow_style=False, allow_unicode=True)
            tmp_path = Path(tf.name)
        load_config(tmp_path)
        shutil.move(str(tmp_path), str(config_path))
        ctx["save_success"] = True
        ctx["config_error"] = None
    except Exception as exc:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        ctx["config_error"] = str(exc)
    finally:
        try:
            ctx["config"] = load_config(config_path)
        except Exception:
            ctx["config"] = None

    return templates.TemplateResponse(request, "config.html", ctx)


@router.post("/api/config/test", response_class=HTMLResponse)
async def config_test(request: Request):
    """Check paths, calibredb, docker, ntfy and return an HTML fragment."""
    from ...config import load_config

    config_path = request.app.state.config_path
    results: list[dict] = []

    def _check(label: str, ok: bool, detail: str = "") -> None:
        results.append({"label": label, "ok": ok, "detail": detail})

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        _check("Config loads", False, str(exc))
        return templates.TemplateResponse(
            request, "partials/test_results.html", {"results": results}
        )

    _check("Config loads", True)

    for label, path in [
        ("incoming_dir", cfg.watcher.incoming_dir),
        ("staging_dir", cfg.paths.staging_dir),
        ("review_dir", cfg.paths.review_dir),
        ("failed_dir", cfg.paths.failed_dir),
    ]:
        if path:
            _check(f"{label} exists", path.exists(), str(path))

    if cfg.paths.state_db:
        parent = cfg.paths.state_db.parent
        _check("state_db directory exists", parent.exists(), str(parent))

    if cfg.calibre.mode == "local":
        cal_ok = shutil.which("calibredb") is not None
        _check("calibredb in PATH", cal_ok)
        if cal_ok and cfg.calibre.library_db_path:
            try:
                r = subprocess.run(
                    [
                        "calibredb", "list",
                        "--with-library", str(cfg.calibre.library_db_path),
                        "--limit", "1", "--fields", "id",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                detail = r.stderr.strip()[:120] if r.returncode else ""
                _check("calibredb connects to library", r.returncode == 0, detail)
            except Exception as exc:
                _check("calibredb connects to library", False, str(exc))
    else:
        try:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}",
                 cfg.calibre.docker_container],
                capture_output=True, text=True, timeout=5,
            )
            _check(
                f"docker '{cfg.calibre.docker_container}' running",
                r.stdout.strip() == "true",
            )
        except Exception as exc:
            _check("docker inspect", False, str(exc))

    if cfg.calibre.reconnect_url:
        try:
            import httpx
            resp = httpx.get(cfg.calibre.reconnect_url, timeout=5)
            _check("reconnect_url reachable", resp.status_code < 500, f"HTTP {resp.status_code}")
        except Exception as exc:
            _check("reconnect_url reachable", False, str(exc))

    if cfg.ntfy.enabled and cfg.ntfy.topic:
        try:
            import httpx
            headers: dict[str, str] = {}
            if cfg.ntfy.auth_token:
                headers["Authorization"] = f"Bearer {cfg.ntfy.auth_token}"
            url = f"{cfg.ntfy.base_url.rstrip('/')}/{cfg.ntfy.topic}"
            resp = httpx.post(url, content="Libris config test", headers=headers, timeout=10)
            _check("ntfy notification sent", resp.status_code in (200, 204), f"HTTP {resp.status_code}")
        except Exception as exc:
            _check("ntfy notification", False, str(exc))

    return templates.TemplateResponse(
        request, "partials/test_results.html", {"results": results}
    )


@router.get("/api/fs/browse", response_class=HTMLResponse)
def fs_browse(request: Request, path: str = "/", field: str = ""):
    """Return a directory listing HTML fragment for the dir-browser modal."""
    try:
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            p = p.parent
        entries = sorted(
            [e for e in p.iterdir() if e.is_dir() and not e.name.startswith(".")],
            key=lambda e: e.name.lower(),
        )
    except Exception:
        p = Path("/")
        entries = []

    ctx = {
        "current": str(p),
        "parent": str(p.parent) if p != p.parent else None,
        "entries": [{"name": e.name, "path": str(e)} for e in entries],
        "field": field,
    }
    return templates.TemplateResponse(request, "partials/dir_browser.html", ctx)


@router.post("/api/fs/mkdir", response_class=HTMLResponse)
async def fs_mkdir(request: Request):
    """Create a directory and return an updated listing."""
    form = await request.form()
    parent = str(form.get("parent", "/")).strip()
    name = str(form.get("name", "")).strip()
    field = str(form.get("field", ""))

    new_path = Path(parent) / name if name else Path(parent)
    try:
        new_path.mkdir(parents=True, exist_ok=True)
        dest = str(new_path)
    except Exception:
        dest = parent

    return RedirectResponse(
        url=f"/api/fs/browse?path={dest}&field={field}", status_code=303
    )
