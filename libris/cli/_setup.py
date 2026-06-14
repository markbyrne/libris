"""Config resolution, store management, logging, and interactive prompts."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

import click
import yaml

from ..exceptions import ConfigError, RateLimitError
from ..state import FileState, StateStore
from ._helpers import (
    _hr,
    _live_review_records,
    _render_review_hints,
    _render_review_record,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config search paths and shared click option
# ---------------------------------------------------------------------------

_CONFIG_SEARCH_PATHS = [
    Path("config.local.yaml"),
    Path("config.yaml"),
    Path.home() / ".config" / "libris" / "config.yaml",
]

_CONFIG_OPTION = click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(path_type=Path),
    help="Config file path. Overrides LIBRIS_CONFIG and auto-discovery.",
)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    """Print an error and exit."""
    click.echo(f"\n  ❌  {msg}\n", err=True)
    sys.exit(1)


def _resolve_config(config_path: Path | None) -> Path:
    """Return the config path to use, in priority order:

    1. ``--config <path>`` CLI flag
    2. ``LIBRIS_CONFIG`` environment variable
    3. ``config.local.yaml`` in the current directory
    4. ``config.yaml`` in the current directory
    5. ``~/.config/libris/config.yaml``
    """
    import os

    if config_path is not None:
        if not config_path.exists():
            _die(f"Config file not found: {config_path}")
        return config_path

    env_path = os.environ.get("LIBRIS_CONFIG")
    if env_path:
        p = Path(env_path).expanduser()
        if not p.exists():
            _die(f"Config file from LIBRIS_CONFIG not found: {p}")
        return p

    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            click.echo(click.style(f"Using config: {candidate.resolve()}", dim=True))
            return candidate

    tried = "\n".join(f"  {p.resolve()}" for p in _CONFIG_SEARCH_PATHS)
    _die(
        f"No config file found. Tried:\n{tried}\n"
        "Options:\n"
        "  Set LIBRIS_CONFIG=/path/to/config.yaml in your shell profile\n"
        "  Copy config:  cp config.example.yaml ~/.config/libris/config.yaml\n"
        "  Pass flag:    libris --config /path/to/config.yaml <command>"
    )


def _open_store(db_path: Path) -> StateStore:
    """Open the state store, printing a clear user-facing error if the DB is corrupt."""
    try:
        return StateStore(db_path)
    except ConfigError as exc:
        _die(str(exc))


def _calibredb_list(query: str, config) -> str:
    """Run calibredb list and return formatted output. Works in local and docker mode."""
    if config.calibre.mode == "docker":
        cmd = [
            "docker", "exec", config.calibre.docker_container,
            "calibredb", "list", "--search", query, "--fields", "id,title,authors",
        ]
    else:
        cmd = [
            "calibredb", "list",
            "--search", query,
            "--fields", "id,title,authors",
            "--with-library", str(config.calibre.library_path),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _die(f"calibredb error: {result.stderr.strip()}")
    return result.stdout.strip()


def _show_queue_summary(config) -> None:
    """Re-render the review queue after an accept/rematch so the user sees fresh IDs."""
    store = _open_store(config.paths.state_db)
    records, stale = _live_review_records(store)
    pending_count = len(store.list_by_state(FileState.PENDING_PARTS))
    failed_count = len(store.list_by_state(FileState.FAILED))
    store.close()
    click.echo()
    if not records:
        click.echo(click.style("  ✅  Review queue is now empty.", fg="green"))
    else:
        click.echo(f"  {len(records)} item(s) remaining in review:")
        click.echo(_hr())
        click.echo()
        for i, r in enumerate(records, 1):
            _render_review_record(i, r)
            click.echo()
        has_audio = any(r.media_type == "audiobook" for r in records)
        has_dupes = any(r.error_msg and r.error_msg.startswith("Duplicate:") for r in records)
        _render_review_hints(has_audio=has_audio, has_dupes=has_dupes)
    if stale:
        click.echo(click.style(
            f"  ⚠   {stale} stale record(s) not shown — run 'libris review-discard --stale'",
            fg="yellow",
        ))
    if pending_count:
        click.echo(click.style(
            f"  ⚠   {pending_count} file(s) in PENDING state — run 'libris list-pending' to see them.",
            fg="yellow",
        ))
    if failed_count:
        click.echo(click.style(
            f"  ⚠   {failed_count} file(s) in FAILED state — run 'libris list-failed' to see them.",
            fg="yellow",
        ))
    click.echo()


# ---------------------------------------------------------------------------
# Rate-limit helpers (used by rematch)
# ---------------------------------------------------------------------------

def _save_google_api_key(config_path: Path, api_key: str) -> bool:
    """Insert/update google_books_api_key in the config file. Returns True on success."""
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        if "metadata" not in data:
            data["metadata"] = {}
        data["metadata"]["google_books_api_key"] = api_key
        with open(config_path, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception as exc:
        log.warning("cli.save_api_key_failed", extra={"error": str(exc)})
        return False


def _prompt_add_google_key(config_path: Path) -> str:
    """Walk the user through getting a Google Books API key. Returns 'key_saved' or 'skip'."""
    click.echo()
    click.echo("  To get a free Google Books API key:")
    click.echo(click.style("    1.  https://console.developers.google.com/", bold=True))
    click.echo(click.style('    2.  Create (or select) a project', dim=True))
    click.echo(click.style('    3.  APIs & Services  →  Enable APIs & Services', dim=True))
    click.echo(click.style('    4.  Search "Books API" and enable it', dim=True))
    click.echo(click.style('    5.  Credentials  →  Create credentials  →  API key', dim=True))
    click.echo()

    api_key = click.prompt("  Paste API key (or Enter to skip)").strip()
    if not api_key:
        click.echo(click.style("  No key entered — skipping Google Books.", fg="yellow"))
        return "skip"

    if _save_google_api_key(config_path, api_key):
        click.echo(click.style(f"\n  ✅  Key saved to {config_path.name}. Retrying…\n", fg="green"))
        return "key_saved"
    else:
        click.echo(click.style(
            f"\n  ⚠   Could not save the key automatically.\n"
            f"  Add it manually to {config_path} under metadata:\n"
            f"    google_books_api_key: \"{api_key}\"\n",
            fg="yellow",
        ))
        return "skip"


def _prompt_rate_limit(error: RateLimitError, config_path: Path, config) -> str:
    """Show rate-limit options and return 'wait', 'key_saved', or 'skip'."""
    is_google = error.source == "google_books"
    source_label = "Google Books" if is_google else "OpenLibrary"
    has_key = bool(config.metadata.google_books_api_key) if is_google else False
    is_daily = error.reason in ("dailyLimitExceeded",) or (has_key and is_google)
    wait_secs = error.retry_after

    click.echo()
    click.echo(click.style(f"  ⚠   {source_label} rate limit hit", fg="yellow"))

    if is_google and not has_key:
        if is_daily or error.reason == "dailyLimitExceeded":
            click.echo(click.style(
                "      Daily unauthenticated quota exceeded — resets at midnight Pacific Time.",
                dim=True,
            ))
        else:
            click.echo(click.style(
                "      Unauthenticated requests are heavily throttled by Google.",
                dim=True,
            ))
        click.echo(click.style(
            "      A free API key grants 1,000 requests/day with reliable access.",
            dim=True,
        ))
    elif is_google and has_key:
        click.echo(click.style(
            "      Daily API key quota (1,000 req/day) exhausted — resets at midnight Pacific Time.",
            dim=True,
        ))

    click.echo()

    can_wait = wait_secs is not None and not is_daily
    if can_wait:
        click.echo(f"  [w]  Wait {wait_secs}s and retry")
    if is_google and not has_key:
        click.echo( "  [k]  Add a Google Books API key (free, 1,000 req/day)  ← recommended")
    click.echo(f"  [s]  Skip {source_label} for this search")
    click.echo()

    valid_opts = []
    if can_wait:
        valid_opts.append("w")
    if is_google and not has_key:
        valid_opts.append("k")
    valid_opts.append("s")

    default = "k" if (is_google and not has_key) else "s"
    while True:
        choice = click.prompt("  Choice", default=default).strip().lower()
        if choice in valid_opts:
            break
        click.echo(click.style(f"  Please enter one of: {', '.join(valid_opts)}", fg="yellow"))

    if choice == "w" and can_wait:
        click.echo()
        try:
            for remaining in range(wait_secs, 0, -1):
                click.echo(f"\r  Waiting {remaining}s…  ", nl=False)
                time.sleep(1)
        except KeyboardInterrupt:
            click.echo("\r  Wait cancelled — skipping.          ")
            return "skip"
        click.echo("\r  Done. Retrying…                      ")
        return "wait"

    if choice == "k":
        return _prompt_add_google_key(config_path)

    return "skip"


# ---------------------------------------------------------------------------
# Config file patching helpers (used by migrate-libris / migrate-library)
# ---------------------------------------------------------------------------

def _update_config_paths(config_path: Path, updates: dict) -> None:
    """Rewrite specific config keys in-place, preserving YAML comments."""
    import re as _re

    text = config_path.read_text()
    lines = text.splitlines(keepends=True)

    leaf_updates: dict[str, str] = {}
    for dotted_key, new_val in updates.items():
        leaf = dotted_key.split(".")[-1]
        leaf_updates[leaf] = str(new_val)

    result = []
    for line in lines:
        replaced = False
        for leaf, new_val in leaf_updates.items():
            m = _re.match(
                r'^(\s*' + _re.escape(leaf) + r'\s*:\s*)([^#\n]*)(#.*)?(\n?)$',
                line,
            )
            if m:
                prefix, _old_val, comment, newline = m.groups()
                comment_part = ("  " + comment) if comment else ""
                result.append(f"{prefix}{new_val}{comment_part}{newline}")
                replaced = True
                break
        if not replaced:
            result.append(line)

    config_path.write_text("".join(result))


def _update_config_calibre_split(config_path: Path, db_path: str, books_path: str) -> None:
    """Transition a config from single library_path to split library_db_path / book_file_path."""
    import re as _re

    text = config_path.read_text()

    if _re.search(r'^\s*library_path\s*:', text, _re.MULTILINE):
        text = _re.sub(
            r'^(\s*)library_path(\s*:)',
            r'\1library_db_path\2',
            text,
            flags=_re.MULTILINE,
        )

    if _re.search(r'^\s*library_db_path\s*:', text, _re.MULTILINE):
        text = _re.sub(
            r'^(\s*library_db_path\s*:\s*)([^#\n]*)(#.*)?$',
            lambda m: f"{m.group(1)}{db_path}{'  ' + m.group(3) if m.group(3) else ''}",
            text,
            flags=_re.MULTILINE,
        )
    else:
        text = _re.sub(
            r'^(\s*mode\s*:.*calibre.*\n)',
            r'\1' + f"  library_db_path: {db_path}\n",
            text,
            flags=_re.MULTILINE,
        )

    if _re.search(r'^\s*book_file_path\s*:', text, _re.MULTILINE):
        text = _re.sub(
            r'^(\s*book_file_path\s*:\s*)([^#\n]*)(#.*)?$',
            lambda m: f"{m.group(1)}{books_path}{'  ' + m.group(3) if m.group(3) else ''}",
            text,
            flags=_re.MULTILINE,
        )
    else:
        text = _re.sub(
            r'^(\s*library_db_path\s*:.*\n)',
            r'\1' + f"  book_file_path: {books_path}\n",
            text,
            flags=_re.MULTILINE,
        )

    config_path.write_text(text)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Suppress httpx/httpcore — they log full request URLs at INFO, which
    # would expose API keys in query parameters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
