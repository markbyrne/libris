"""CLI entry points for libris.

Commands:
  libris run             — Start the watcher daemon
  libris import-one      — Process a single file (Mac dev / testing)
  libris check-config    — Validate config and print resolved values
  libris list-review     — Show all files in REVIEW state
  libris show-cover      — Open the matched cover image in the default browser
  libris review-accept   — Force-import a file from review/, bypassing confidence check
  libris review-discard  — Delete a review-queue file (e.g. confirmed duplicate)
  libris reset           — Reset stuck PROCESSING records back to INCOMING
  libris recover         — Move failed files back to review/ for re-processing
  libris list-pending    — Show multi-part audiobooks waiting for sibling parts
  libris combine-parts   — Force-combine a pending part group and import
  libris revert-import   — Remove a book from Calibre and return it to review/
  libris search          — Search the Calibre library (uses library path from config)
  libris rematch         — Interactively re-query metadata APIs for a review item
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import yaml

import click
import httpx

from .calibre import get_calibre
from .cleaner import clean_query as _clean_query
from .config import load_config
from .exceptions import ConfigError, RateLimitError
from .metadata.base import MetadataResult, SearchQuery
from .metadata.resolver import _extract_author_hint, _extract_year, _extract_series, _SERIES_PREFIX
from .metadata.scorer import dedup_candidates, score_candidate
from .pipeline import Pipeline
from .state import FileState, StateStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
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

_WEIGHT_MAX = {"isbn": 0.40, "title": 0.30, "author": 0.20, "year": 0.10}


def _resolve_config(config_path: Optional[Path]) -> Path:
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


def _die(msg: str) -> None:
    """Print an error and exit."""
    click.echo(f"\n  ❌  {msg}\n", err=True)
    sys.exit(1)


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


def _hr(width: int = 50) -> str:
    return "  " + "─" * width


def _has_match(record) -> bool:
    """Return True if the record has a real API-sourced metadata candidate.

    A record with no stored JSON means the pipeline found zero API results
    (rate limited, unrecognised title, etc.) — review-accept should be
    blocked until the user runs rematch to find a candidate.
    """
    return record.matched_metadata_json is not None


def _hyperlink(url: str, text: str) -> str:
    """Wrap text in an OSC 8 terminal hyperlink (supported by iTerm2, Terminal, Warp, etc.)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _live_review_records(store: "StateStore") -> list:
    """Return REVIEW records whose file still exists on disk, in stable order.

    Records pointing to files that have been moved or deleted are silently
    excluded so list-review, rematch, review-accept, and show-cover all
    share the same consistent positional IDs.

    Returns (live_records, stale_count).
    """
    records = store.list_by_state(FileState.REVIEW)
    live = [r for r in records if Path(r.current_path).exists()]
    return live, len(records) - len(live)


def _render_review_record(i: int, r) -> None:
    """Print a single review-queue entry (shared by list-review and show-cover)."""
    is_dup = bool(r.error_msg and r.error_msg.startswith("Duplicate:"))
    dup_tag = "  " + click.style("[!]", fg="yellow", bold=True) if is_dup else ""
    click.echo(f"  [{i}]{dup_tag}  {Path(r.current_path).name}")

    # Duplicate warning — shown before the match block
    if is_dup:
        click.echo(click.style(f"        ⚠  {r.error_msg}", fg="yellow"))
        click.echo(click.style(
            f"           Accept (overwrite): libris review-accept --id {i} --overwrite", dim=True
        ))
        click.echo(click.style(
            f"           Discard:            libris review-discard --id {i}", dim=True
        ))

    if not _has_match(r):
        click.echo(click.style("        [!] No match found", fg="yellow"))
        click.echo(click.style(f"           Try:  libris rematch --id {i}", dim=True))
    else:
        matched = r.matched_title or "(unknown)"
        if r.matched_author:
            matched += f"  by {r.matched_author}"
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "n/a"

        pub_parts = []
        if r.matched_year:
            pub_parts.append(str(r.matched_year))
        if r.matched_publisher:
            pub_parts.append(r.matched_publisher)
        if r.matched_isbn:
            pub_parts.append(f"ISBN {r.matched_isbn}")

        click.echo(f"        Matched:  {matched}")
        click.echo(f"        Score:    {conf}")
        if pub_parts:
            click.echo(f"        Info:     {' · '.join(pub_parts)}")
        if r.matched_cover_url:
            click.echo(f"        Cover:    libris show-cover --id {i}")

    click.echo(f"        Path:     \"{r.current_path}\"")


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
    """Show rate-limit options and return 'wait', 'key_saved', or 'skip'.

    For Google Books without an API key, also offers to add one.
    Daily/quota limits don't offer a wait option — waiting seconds won't help.
    The function blocks during any countdown (wait choice).
    """
    is_google = error.source == "google_books"
    source_label = "Google Books" if is_google else "OpenLibrary"
    has_key = bool(config.metadata.google_books_api_key) if is_google else False
    is_daily = error.reason in ("dailyLimitExceeded",) or (has_key and is_google)
    wait_secs = error.retry_after  # None if not provided by the API

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

    # Only offer wait if we have an actual Retry-After time (short-term throttle).
    # Daily quota resets don't benefit from a short wait.
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

    return "skip"  # choice == "s"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@click.group()
def main() -> None:
    """Libris — intelligent book and audiobook organiser for Calibre."""


@main.command()
@_CONFIG_OPTION
def run(config_path: Optional[Path]) -> None:
    """Start the file watcher daemon."""
    path = _resolve_config(config_path)
    config = load_config(path)
    _setup_logging(config.log_level)
    pipeline = Pipeline(config)
    pipeline.run()


@main.command("import-one")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@_CONFIG_OPTION
def import_one(file_path: Path, config_path: Optional[Path]) -> None:
    """Process a single file immediately (no daemon, useful for testing)."""
    path = _resolve_config(config_path)
    config = load_config(path)
    _setup_logging(config.log_level)
    if file_path.is_symlink():
        _die(f"'{file_path}' is a symlink — refusing to process. Pass the real file path.")
    pipeline = Pipeline(config)
    record = pipeline.process_file(file_path.resolve())

    click.echo()
    status = "✅" if record.state == FileState.IMPORTED else (
        "🔍" if record.state == FileState.REVIEW else "❌"
    )
    click.echo(f"  {status}  {file_path.name}")
    click.echo(_hr())
    click.echo(f"  Result:  {record.state.value}")
    if record.matched_title:
        click.echo(f"  Title:   {record.matched_title}")
    if record.matched_author:
        click.echo(f"  Author:  {record.matched_author}")
    if record.confidence is not None:
        click.echo(f"  Score:   {record.confidence:.2f}")
    if record.error_msg:
        if record.state == FileState.IMPORTED:
            # Informational note (e.g. format-merge) — not an error
            click.echo(f"  Note:    {record.error_msg}")
        else:
            click.echo(f"  Error:   {record.error_msg}", err=True)
    click.echo()

    # Exit 0 for any controlled disposition; 1 only for genuine failure
    sys.exit(0 if record.state in (FileState.IMPORTED, FileState.REVIEW, FileState.PENDING_PARTS) else 1)


@main.command("check-config")
@_CONFIG_OPTION
def check_config(config_path: Optional[Path]) -> None:
    """Validate config file and print resolved settings."""
    path = _resolve_config(config_path)
    try:
        config = load_config(path)
    except Exception as exc:
        _die(f"Config error: {exc}")

    click.echo()
    click.echo("  ✅  Config valid")
    click.echo(_hr())
    click.echo(f"  Incoming dir:   {config.watcher.incoming_dir}")
    click.echo(f"  Staging dir:    {config.paths.staging_dir}")
    click.echo(f"  Review dir:     {config.paths.review_dir}")
    click.echo(f"  Failed dir:     {config.paths.failed_dir}")
    click.echo(f"  State DB:       {config.paths.state_db}")
    click.echo(f"  Calibre mode:   {config.calibre.mode}")
    if config.calibre.mode == "local":
        _lib = config.calibre.library_path
        click.echo(f"  Library path:   {_lib}")
        if not _lib.exists():
            click.echo(click.style(
                f"  ⚠   Library path does not exist — it will be created on first import.", fg="yellow"
            ))
    else:
        click.echo(f"  Container:      {config.calibre.docker_container}")
    click.echo(f"  Confidence:     {config.metadata.confidence_threshold}")
    click.echo(f"  Duplicates:     {config.metadata.duplicate_action}")
    click.echo(f"  Mock mode:      {config.metadata.mock_mode}")
    click.echo(f"  Ebook format:   {config.output.preferred_ebook_format}  (policy: {config.output.ebook_format_policy})")
    _scan = config.watcher.scan_interval_hours
    _scan_str = f"every {_scan:g}h" if _scan > 0 else "startup only (periodic rescan disabled)"
    click.echo(f"  Folder scan:    {_scan_str}")
    click.echo(f"  ntfy topic:     {config.ntfy.topic or '(not set)'}")
    click.echo(f"  ntfy enabled:   {config.ntfy.enabled}")
    click.echo(f"  Log level:      {config.log_level}")

    # ── ntfy connectivity check ───────────────────────────────────────────
    if config.ntfy.enabled and config.ntfy.topic:
        click.echo()
        click.echo("  Checking ntfy…  ", nl=False)
        try:
            _url = f"{config.ntfy.base_url.rstrip('/')}/{config.ntfy.topic}"
            _headers = {"Title": "Libris check-config", "Priority": "min", "Tags": "white_check_mark"}
            if config.ntfy.auth_token:
                _headers["Authorization"] = f"Bearer {config.ntfy.auth_token}"
            _r = httpx.post(_url, content=b"Connection test from libris check-config.", headers=_headers, timeout=8.0)
            _r.raise_for_status()
            click.echo(click.style("✅  notification sent", fg="green"))
        except Exception as _exc:
            click.echo(click.style(f"❌  failed: {_exc}", fg="red"))
    elif config.ntfy.enabled and not config.ntfy.topic:
        click.echo()
        click.echo(click.style("  ⚠   ntfy is enabled but no topic is set — notifications will be skipped.", fg="yellow"))

    click.echo()


@main.command("list-review")
@_CONFIG_OPTION
def list_review(config_path: Optional[Path]) -> None:
    """List all files currently in REVIEW state (low-confidence matches)."""
    path = _resolve_config(config_path)
    config = load_config(path)
    store = _open_store(config.paths.state_db)
    records, stale_count = _live_review_records(store)
    failed_records = store.list_by_state(FileState.FAILED)
    store.close()

    click.echo()
    if not records:
        click.echo("  No files in review.")
        if stale_count:
            click.echo(click.style(
                f"  ⚠   {stale_count} record(s) skipped — file no longer at expected path.\n"
                f"      Clean up: libris review-discard --stale",
                fg="yellow",
            ))
        if failed_records:
            click.echo()
            click.echo(click.style(
                f"  ⚠   {len(failed_records)} file(s) in FAILED state — run 'libris recover' to see them.",
                fg="yellow",
            ))
        click.echo()
        return

    click.echo(f"  {len(records)} file(s) in review")
    click.echo(_hr())
    click.echo()

    for i, r in enumerate(records, 1):
        _render_review_record(i, r)
        click.echo()

    has_dupes = any(
        r.error_msg and r.error_msg.startswith("Duplicate:") for r in records
    )
    click.echo(_hr())
    click.echo("  Accept by ID:    libris review-accept --id <N>")
    click.echo("  Accept all:      libris review-accept --accept-all")
    click.echo("  Accept by path:  libris review-accept \"<path>\"")
    if has_dupes:
        click.echo(click.style(
            "  Overwrite [!]:   libris review-accept --id <N> --overwrite",
            fg="yellow",
        ))
    click.echo("  Fix bad match:   libris rematch --id <N>")
    click.echo("  Preview cover:   libris show-cover --id <N>")
    click.echo("  Discard:         libris review-discard --id <N>")
    click.echo("  Discard dupes:   libris review-discard --duplicates")
    if stale_count:
        click.echo()
        click.echo(click.style(
            f"  ⚠   {stale_count} record(s) not shown — file moved or deleted.\n"
            f"      Clean up: libris review-discard --stale",
            fg="yellow",
        ))
    if failed_records:
        click.echo()
        click.echo(click.style(
            f"  ⚠   {len(failed_records)} file(s) also in FAILED state — run 'libris recover'",
            fg="yellow",
        ))
    click.echo()


@main.command("show-cover")
@click.option("--id", "review_id", required=True, type=int,
              help="Review queue position (from 'libris list-review')")
@_CONFIG_OPTION
def show_cover(review_id: int, config_path: Optional[Path]) -> None:
    """Open the cover image for a review queue item in the default browser.

    \b
      libris show-cover --id 1
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    store = _open_store(config.paths.state_db)
    records, _ = _live_review_records(store)
    store.close()

    if not records:
        click.echo("\n  No files in review queue.\n")
        return

    if review_id < 1 or review_id > len(records):
        _die(
            f"ID {review_id} out of range — queue has {len(records)} item(s).\n"
            "  Run 'libris list-review' to see current IDs."
        )

    record = records[review_id - 1]
    if not record.matched_cover_url:
        click.echo(click.style(
            f"\n  No cover URL stored for [{review_id}] {Path(record.current_path).name}\n"
            "  Run 'libris rematch --id <N>' to fetch a new match with cover art.\n",
            fg="yellow",
        ))
        return

    import platform
    url = record.matched_cover_url
    opener = "open" if platform.system() == "Darwin" else "xdg-open"
    click.echo()
    try:
        subprocess.run([opener, url], check=True)
        click.echo(click.style("  ✅  Cover opened in browser", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"  ❌  Failed to open cover: {exc}", fg="red"), err=True)
        click.echo(f"  URL: {url}")
        click.echo()
        return

    # Re-render the record so the user has full context alongside the browser window
    click.echo()
    click.echo(_hr())
    _render_review_record(review_id, record)
    click.echo()
    click.echo(_hr())
    is_dup = record.error_msg and record.error_msg.startswith("Duplicate:")
    if is_dup:
        click.echo(f"  Accept (overwrite):  libris review-accept --id {review_id} --overwrite")
    else:
        click.echo(f"  Accept:              libris review-accept --id {review_id}")
    click.echo(f"  Fix match:           libris rematch --id {review_id}")
    click.echo()


@main.command("review-discard")
@click.option("--id", "review_id", type=int, default=None,
              help="Discard by review queue position (from 'libris list-review')")
@click.option("--duplicates", "duplicates_only", is_flag=True, default=False,
              help="Discard all items flagged as duplicates")
@click.option("--all", "discard_all", is_flag=True, default=False,
              help="Discard every item in the review queue")
@click.option("--stale", "discard_stale", is_flag=True, default=False,
              help="Remove DB records for review items whose file no longer exists on disk")
@_CONFIG_OPTION
def review_discard(
    review_id: Optional[int],
    duplicates_only: bool,
    discard_all: bool,
    discard_stale: bool,
    config_path: Optional[Path],
) -> None:
    """Delete a review-queue file and remove it from the queue.

    The file is permanently deleted from disk and the record is marked so it
    won't be re-imported by a future scan.  Use this to clean up duplicates
    or files you simply don't want in your library.

    \b
      libris review-discard --id 1           # discard one item
      libris review-discard --duplicates     # discard all detected duplicates
      libris review-discard --stale          # remove records where the file is already gone
      libris review-discard --all            # discard every item in review
    """
    path = _resolve_config(config_path)
    config = load_config(path)

    n_flags = sum([review_id is not None, duplicates_only, discard_all, discard_stale])
    if n_flags == 0:
        _die(
            "Provide one of: --id <N>, --duplicates, --stale, or --all\n"
            "  Run 'libris list-review' to see queued files and their IDs."
        )
    if n_flags > 1:
        _die("Only one of --id, --duplicates, --stale, or --all may be used at a time.")

    store = _open_store(config.paths.state_db)

    # --stale: clean up records whose file has already been removed from disk
    if discard_stale:
        all_review = store.list_by_state(FileState.REVIEW)
        stale = [r for r in all_review if not Path(r.current_path).exists()]
        if not stale:
            store.close()
            click.echo("\n  No stale records found.\n")
            return
        click.echo()
        for record in stale:
            name = Path(record.current_path).name
            record.state = FileState.IMPORTED
            record.error_msg = f"Pruned: file no longer at expected path ({record.current_path})"
            store.upsert(record)
            click.echo(f"  🧹  {name}")
            click.echo(click.style(f"       was: {record.current_path}", dim=True))
        store.close()
        click.echo()
        return

    records, _ = _live_review_records(store)

    if not records:
        store.close()
        click.echo("\n  No files in review queue.\n")
        return

    # Determine target records
    if review_id is not None:
        if review_id < 1 or review_id > len(records):
            store.close()
            _die(
                f"ID {review_id} out of range — queue has {len(records)} item(s).\n"
                "  Run 'libris list-review' to see current IDs."
            )
        targets = [(review_id, records[review_id - 1])]
    elif duplicates_only:
        targets = [
            (i, r) for i, r in enumerate(records, 1)
            if r.error_msg and r.error_msg.startswith("Duplicate:")
        ]
        if not targets:
            store.close()
            click.echo("\n  No duplicate items in review queue.\n")
            return
    else:  # --all
        targets = list(enumerate(records, 1))

    click.echo()
    any_failed = False

    for queue_pos, record in targets:
        file_path = Path(record.current_path)
        name = file_path.name

        # Show what we're about to delete with duplicate context if available
        dup_note = ""
        if record.error_msg and record.error_msg.startswith("Duplicate:"):
            # Extract "ID(s): N" from the error_msg for a compact display
            dup_note = click.style(f"  ({record.error_msg})", dim=True)

        try:
            file_path.unlink(missing_ok=True)
            # Mark IMPORTED so re-scans don't pick it up again
            record.state = FileState.IMPORTED
            record.error_msg = f"Discarded by user (was in review/{name})"
            store.upsert(record)
            click.echo(f"  🗑   [{queue_pos}] {name}{dup_note}")
        except Exception as exc:
            click.echo(click.style(f"  ❌  [{queue_pos}] {name}: {exc}", fg="red"), err=True)
            any_failed = True

    store.close()
    click.echo()
    sys.exit(1 if any_failed else 0)


@main.command("review-accept")
@click.argument("file_path", required=False, default=None, type=click.Path(path_type=Path))
@click.option("--id", "review_id", type=int, default=None,
              help="Accept by review queue position (from 'libris list-review')")
@click.option("--accept-all", "accept_all", is_flag=True, default=False,
              help="Accept every file currently in the review queue")
@click.option("--overwrite", "overwrite", is_flag=True, default=False,
              help="Import even if the book is already in the Calibre library (bypass duplicate check)")
@_CONFIG_OPTION
def review_accept(
    file_path: Optional[Path],
    review_id: Optional[int],
    accept_all: bool,
    overwrite: bool,
    config_path: Optional[Path],
) -> None:
    """Force-import file(s) from review/, bypassing the confidence threshold.

    Three ways to select which file(s) to accept:

    \b
      libris review-accept --id 1
      libris review-accept --accept-all
      libris review-accept "/path/with spaces/file.epub"

    For items flagged as duplicates, add --overwrite to import anyway:

    \b
      libris review-accept --id 1 --overwrite
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    _setup_logging(config.log_level)

    n_methods = sum([file_path is not None, review_id is not None, accept_all])
    if n_methods == 0:
        _die(
            "Provide one of: FILE_PATH argument, --id N, or --accept-all\n"
            "  Run 'libris list-review' to see queued files and their IDs."
        )
    if n_methods > 1:
        _die("Only one of FILE_PATH, --id, or --accept-all may be used at a time.")

    store = _open_store(config.paths.state_db)

    # Build a list of (path, record_or_None, queue_position) triples so we can
    # use cached metadata and show accurate rematch IDs in error messages.
    # Uses _live_review_records so IDs match what list-review showed.
    if review_id is not None or accept_all:
        all_records, _ = _live_review_records(store)
        store.close()
        if not all_records:
            click.echo("\n  No files in review queue.\n")
            return
        if review_id is not None:
            if review_id < 1 or review_id > len(all_records):
                _die(
                    f"ID {review_id} out of range — queue has {len(all_records)} item(s).\n"
                    "  Run 'libris list-review' to see current IDs."
                )
            target_triples = [(Path(all_records[review_id - 1].current_path), all_records[review_id - 1], review_id)]
        else:
            target_triples = [(Path(r.current_path), r, i) for i, r in enumerate(all_records, 1)]
    else:
        # Path-based: look up the record and its queue position
        resolved = file_path.resolve()
        cached = store.get_by_current_path(str(resolved))
        all_review = store.list_by_state(FileState.REVIEW)
        store.close()
        queue_pos = next(
            (i for i, r in enumerate(all_review, 1) if r.current_path == str(resolved)),
            None,
        )
        target_triples = [(resolved, cached, queue_pos)]

    any_failed = False
    # Items skipped during --accept-all (no match, or duplicate blocked).
    # IDs are re-resolved after the loop so hints reflect the post-import queue.
    skipped_items: list[tuple[str, str]] = []  # (filename, current_path)

    click.echo()
    for target, cached_record, queue_pos in target_triples:
        if not target.exists():
            click.echo(f"  ⚠   Skipping (file not found): {target}", err=True)
            any_failed = True
            continue

        # Block acceptance if no metadata match has been found yet
        if cached_record and not _has_match(cached_record):
            click.echo(f"  ⚠   {target.name}")
            if accept_all:
                # Don't emit a stale ID — the queue renumbers as items are accepted.
                # We'll print fresh IDs in the summary below.
                click.echo(click.style("       No metadata match yet.", fg="yellow"))
                skipped_items.append((target.name, str(target)))
            else:
                id_hint = f"--id {queue_pos}" if queue_pos else "--id <N>  (run 'libris list-review' to find ID)"
                click.echo(click.style(
                    f"       No metadata match yet — find one first:\n"
                    f"       libris rematch {id_hint}",
                    fg="yellow",
                ))
            click.echo()
            any_failed = True
            continue

        # Block duplicates unless --overwrite is explicitly given
        is_duplicate = (
            cached_record
            and cached_record.error_msg
            and cached_record.error_msg.startswith("Duplicate:")
        )
        if is_duplicate and not overwrite:
            click.echo(f"  ⚠   {target.name}")
            if accept_all:
                click.echo(click.style(
                    "       Already in Calibre — skipped. Use --overwrite to import anyway.",
                    fg="yellow",
                ))
                skipped_items.append((target.name, str(target)))
            else:
                id_hint = f"--id {queue_pos}" if queue_pos else ""
                click.echo(click.style(
                    f"       Already in Calibre — add --overwrite to import anyway:\n"
                    f"       libris review-accept {id_hint} --overwrite",
                    fg="yellow",
                ))
            click.echo()
            any_failed = True
            continue

        pipeline = Pipeline(config)
        # Ensure the pipeline won't re-flag this as a duplicate mid-import
        if overwrite:
            pipeline.config.metadata.duplicate_action = "import"

        if cached_record and cached_record.matched_metadata_json:
            # Fast path: use the metadata we already have — no API call
            record = pipeline.import_from_record(cached_record)
        else:
            # Legacy path: no cached metadata (pre-feature records or path-based
            # accept where the record wasn't found)
            pipeline.config.metadata.confidence_threshold = 0.0
            record = pipeline.process_file(target)

        if record.state == FileState.IMPORTED:
            pipeline._store.cleanup_stale_review(str(target), exclude_id=record.id)

        # ── Newly-detected duplicate ───────────────────────────────────────
        # force_import found the same format already in Calibre even though
        # the record wasn't pre-flagged (e.g. file went to review for low
        # confidence, not duplicate).  Prompt interactively rather than failing.
        is_newly_dup = (
            not is_duplicate
            and record.state == FileState.REVIEW
            and record.error_msg
            and record.error_msg.startswith("Duplicate:")
        )
        if is_newly_dup:
            if accept_all:
                # No interactive prompt in batch mode — skip and report
                click.echo(f"  ⚠   {target.name}")
                click.echo(click.style("       Duplicate detected — skipped.", fg="yellow"))
                click.echo()
                skipped_items.append((target.name, str(target)))
                any_failed = True
                continue

            click.echo(f"  ⚠   {target.name}")
            click.echo(click.style(f"       {record.error_msg}", fg="yellow"))
            click.echo()
            click.echo("       [o]  Overwrite — replace the existing Calibre entry")
            click.echo("       [d]  Discard   — delete this file from the review queue")
            click.echo("       [s]  Skip      — leave in review (use --overwrite later)")
            click.echo("       [r]  Rematch   — find a different book match")
            click.echo()
            while True:
                dup_choice = click.prompt("       Choice", default="s").strip().lower()
                if dup_choice in ("o", "d", "s", "r"):
                    break
                click.echo(click.style("       Please enter o, d, s, or r.", fg="yellow"))

            if dup_choice == "o":
                pipeline.config.metadata.duplicate_action = "import"
                record = pipeline.import_from_record(record)
                if record.state == FileState.IMPORTED:
                    pipeline._store.cleanup_stale_review(str(target), exclude_id=record.id)
                # fall through to status display below
            elif dup_choice == "d":
                Path(record.current_path).unlink(missing_ok=True)
                record.state = FileState.IMPORTED
                record.error_msg = "Discarded by user (duplicate)"
                pipeline._store.upsert(record)
                click.echo(f"  🗑   {target.name}")
                click.echo()
                continue
            elif dup_choice == "r":  # rematch
                id_hint = f"--id {queue_pos}" if queue_pos else "--id <N>"
                click.echo(click.style(
                    f"  ↩   {target.name} — kept in review for rematching.\n"
                    f"      Run: libris rematch {id_hint}",
                    dim=True,
                ))
                click.echo()
                continue
            else:  # s — skip, leave in review
                click.echo(click.style(
                    f"  ↩   {target.name} — kept in review "
                    f"(run 'libris review-accept --id {queue_pos} --overwrite' to import).",
                    dim=True,
                ))
                click.echo()
                continue

        status = "✅" if record.state == FileState.IMPORTED else "❌"
        click.echo(f"  {status}  {target.name}  [{record.state.value}]")
        if record.matched_title:
            click.echo(f"       Title:   {record.matched_title}")
        if record.matched_author:
            click.echo(f"       Author:  {record.matched_author}")
        if record.confidence is not None:
            click.echo(f"       Score:   {record.confidence:.2f}")
        if record.error_msg:
            if record.state == FileState.IMPORTED:
                # Informational note (e.g. "Added EPUB format to Calibre book N")
                click.echo(f"       Note:    {record.error_msg}")
            else:
                click.echo(f"       Error:   {record.error_msg}", err=True)
                any_failed = True
        click.echo()

    # After --accept-all, re-query the queue for fresh IDs and show a tidy
    # summary of anything that still needs attention.
    if accept_all and skipped_items:
        fresh_store = _open_store(config.paths.state_db)
        fresh_records, _ = _live_review_records(fresh_store)
        fresh_store.close()

        click.echo(_hr())
        click.echo(f"  {len(skipped_items)} item(s) still need attention:\n")
        for name, path_str in skipped_items:
            pos = next(
                (i for i, r in enumerate(fresh_records, 1) if r.current_path == path_str),
                None,
            )
            click.echo(f"  [{pos or '?'}]  {name}")
            if pos:
                click.echo(click.style(f"       libris rematch --id {pos}", dim=True))
            else:
                click.echo(click.style("       libris list-review  (to find current ID)", dim=True))
            click.echo()

    sys.exit(1 if any_failed else 0)


@main.command("recover")
@click.option("--id", "recover_id", type=int, default=None,
              help="Recover by position from the failed list")
@click.option("--all", "recover_all", is_flag=True, default=False,
              help="Recover every failed file back to review/")
@click.option("--delete", "delete_records", is_flag=True, default=False,
              help="Remove the DB record(s) instead of recovering to review/. "
                   "Alone: removes only records whose file is already gone. "
                   "With --id or --all: removes those records (and their files if present).")
@_CONFIG_OPTION
def recover(
    recover_id: Optional[int],
    recover_all: bool,
    delete_records: bool,
    config_path: Optional[Path],
) -> None:
    """Move failed files back to review/ for re-processing.

    Run without arguments to list failed files, then use --id or --all to
    recover them.  Recovered files appear in 'libris list-review' and can be
    fixed with 'libris rematch'.

    Use --delete to clean up records that can't be recovered (e.g. the file
    is already gone):

    \b
      libris recover                    # list failed files
      libris recover --id 1            # move file [1] back to review/
      libris recover --all             # move all failed files back to review/
      libris recover --delete          # remove records where file is missing
      libris recover --delete --id 1  # remove a specific record (and file)
      libris recover --delete --all   # remove all failed records
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    store = _open_store(config.paths.state_db)
    records = store.list_by_state(FileState.FAILED)

    if not records:
        store.close()
        click.echo("\n  No files in failed state.\n")
        return

    # ── List mode (no action flag) ────────────────────────────────────────
    has_missing = any(not Path(r.current_path).exists() for r in records)
    if recover_id is None and not recover_all and not delete_records:
        store.close()
        click.echo()
        click.echo(f"  {len(records)} file(s) in failed state")
        click.echo(_hr())
        click.echo()
        for i, r in enumerate(records, 1):
            exists = Path(r.current_path).exists()
            name = Path(r.current_path).name
            missing = click.style("  (file missing)", fg="yellow") if not exists else ""
            click.echo(f"  [{i}]  {name}{missing}")
            if r.error_msg:
                click.echo(f"        Error:   {r.error_msg[:120]}")
            click.echo(f"        Path:    \"{r.current_path}\"")
            if not exists:
                click.echo(click.style(f"        libris recover --delete --id {i}", dim=True))
            click.echo()
        click.echo(_hr())
        click.echo("  Recover by ID:   libris recover --id <N>")
        click.echo("  Recover all:     libris recover --all")
        if has_missing:
            click.echo("  Delete missing:  libris recover --delete")
        click.echo()
        return

    # ── Delete mode ───────────────────────────────────────────────────────
    if delete_records:
        if recover_id is not None:
            if recover_id < 1 or recover_id > len(records):
                store.close()
                _die(
                    f"ID {recover_id} out of range — {len(records)} failed file(s).\n"
                    "  Run 'libris recover' to see current IDs."
                )
            targets = [records[recover_id - 1]]
        elif recover_all:
            targets = list(records)
        else:
            # Default: only records whose file is already gone
            targets = [r for r in records if not Path(r.current_path).exists()]
            if not targets:
                store.close()
                click.echo(
                    "\n  All failed files still exist on disk — nothing to delete.\n"
                    "  Use --delete --id <N> or --delete --all to force-remove.\n"
                )
                return

        click.echo()
        for record in targets:
            name = Path(record.current_path).name
            Path(record.current_path).unlink(missing_ok=True)
            record.state = FileState.IMPORTED
            record.error_msg = "Deleted by user from failed queue"
            store.upsert(record)
            click.echo(f"  🗑   {name}")
        store.close()
        click.echo()
        return

    # ── Determine targets for recovery ────────────────────────────────────
    if recover_id is not None:
        if recover_id < 1 or recover_id > len(records):
            store.close()
            _die(
                f"ID {recover_id} out of range — {len(records)} failed file(s).\n"
                "  Run 'libris recover' to see current IDs."
            )
        targets = [records[recover_id - 1]]
    else:
        targets = list(records)

    review_dir = config.paths.review_dir
    review_dir.mkdir(parents=True, exist_ok=True)

    click.echo()
    any_failed = False
    for record in targets:
        current = Path(record.current_path)
        if not current.exists():
            click.echo(
                click.style(f"  ⚠   File not found, skipping: {current}", fg="yellow"),
                err=True,
            )
            any_failed = True
            continue

        dest = review_dir / current.name
        if dest.exists():
            dest = review_dir / f"{current.stem}_recovered{current.suffix}"
        shutil.move(str(current), str(dest))

        record.state = FileState.REVIEW
        record.current_path = str(dest)
        record.error_msg = None
        store.upsert(record)

        click.echo(f"  ✅  {current.name}")
        click.echo(f"       → review/{dest.name}")
        click.echo()

    store.close()
    click.echo(_hr())
    click.echo("  Run 'libris list-review' to confirm.")
    click.echo("  Run 'libris rematch --id <N>' to fix the metadata match.")
    click.echo()
    sys.exit(1 if any_failed else 0)


@main.command("list-pending")
@_CONFIG_OPTION
def list_pending(config_path: Optional[Path]) -> None:
    """Show multi-part audiobooks waiting for all parts to arrive.

    Parts are held in staging/pending/ until the complete set is received,
    then automatically combined and imported.  Groups that time out appear
    here until you run 'libris combine-parts --id <N>' to force-combine
    whatever parts are available.

    \b
      libris list-pending
    """
    from datetime import datetime, timezone, timedelta
    path = _resolve_config(config_path)
    config = load_config(path)
    store = _open_store(config.paths.state_db)
    groups = store.list_pending_groups()
    store.close()

    click.echo()
    if not groups:
        click.echo("  No pending part groups.\n")
        return

    click.echo(f"  {len(groups)} pending group(s)")
    click.echo(_hr())
    click.echo()

    now = datetime.now(timezone.utc)
    timeout_td = timedelta(hours=config.multipart.timeout_hours)

    for i, (group_key, records) in enumerate(groups.items(), 1):
        oldest = min(records, key=lambda r: r.created_at)
        age = now - oldest.created_at
        time_left = timeout_td - age
        timed_out = time_left.total_seconds() <= 0

        # Determine which part numbers we have vs expect
        received_nums = sorted(r.part_num for r in records if r.part_num is not None)
        total = records[0].total_parts
        total_str = str(total) if total else "?"

        if total:
            missing = sorted(set(range(1, total + 1)) - set(received_nums))
            missing_str = ", ".join(str(m) for m in missing) if missing else "none"
        else:
            missing_str = "unknown"

        click.echo(f"  [{i}]  {click.style(group_key, bold=True)}")
        click.echo(f"        Parts:    {len(received_nums)} of {total_str} received"
                   + (click.style(f"  (missing: {missing_str})", fg="yellow") if missing_str != "none" else ""))

        age_str = _fmt_age(age)
        if timed_out:
            click.echo(click.style(f"        Age:      {age_str}  ⚠ TIMED OUT", fg="red"))
        else:
            hrs_left = int(time_left.total_seconds() / 3600)
            mins_left = int((time_left.total_seconds() % 3600) / 60)
            click.echo(f"        Age:      {age_str}  (times out in {hrs_left}h {mins_left}m)")

        for r in records:
            exists = Path(r.current_path).exists()
            marker = click.style("✓", fg="green") if exists else click.style("✗", fg="red")
            click.echo(f"        {marker} part {r.part_num}  {Path(r.current_path).name}")
        click.echo()

    click.echo(_hr())
    click.echo("  Force-combine:  libris combine-parts --id <N>")
    click.echo("  Combine all:    libris combine-parts --all")
    click.echo()


@main.command("combine-parts")
@click.option("--id", "group_id", type=int, default=None,
              help="Group position from 'libris list-pending'")
@click.option("--all", "combine_all", is_flag=True, default=False,
              help="Force-combine all pending groups with available parts")
@_CONFIG_OPTION
def combine_parts(
    group_id: Optional[int],
    combine_all: bool,
    config_path: Optional[Path],
) -> None:
    """Force-combine a pending part group and import it into Calibre.

    Use this when you want to import without waiting for all parts, or when
    the timeout has fired but you still want to use the parts you have.

    \b
      libris combine-parts --id 1      # combine group [1]
      libris combine-parts --all       # combine every pending group
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    _setup_logging(config.log_level)

    store = _open_store(config.paths.state_db)
    groups = store.list_pending_groups()
    store.close()

    if not groups:
        click.echo("\n  No pending groups.\n")
        return

    group_list = list(groups.items())

    if group_id is None and not combine_all:
        _die(
            "Provide --id <N> or --all.\n"
            "  Run 'libris list-pending' to see pending groups and their IDs."
        )

    if group_id is not None:
        if group_id < 1 or group_id > len(group_list):
            _die(
                f"ID {group_id} out of range — {len(group_list)} group(s).\n"
                "  Run 'libris list-pending' to see current IDs."
            )
        targets = [group_list[group_id - 1]]
    else:
        targets = group_list

    pipeline = Pipeline(config)
    click.echo()
    any_failed = False

    for group_key, records in targets:
        # Filter to parts that still exist on disk
        live = [r for r in records if Path(r.current_path).exists()]
        if not live:
            click.echo(click.style(f"  ⚠   [{group_key}] all files missing — skipping", fg="yellow"))
            any_failed = True
            continue

        click.echo(f"  Combining {len(live)} part(s) for: {group_key}")
        try:
            result_record = pipeline._combine_pending_group(group_key, live)
            if result_record.state == FileState.IMPORTED:
                click.echo(click.style(
                    f"  ✅  {result_record.matched_title or group_key}", fg="green"
                ))
                if result_record.matched_author:
                    click.echo(f"       Author:  {result_record.matched_author}")
                click.echo(f"       Score:   {result_record.confidence:.2f}")
            else:
                click.echo(f"  🔍  {result_record.matched_title or group_key} → review/")
                click.echo(f"       Run 'libris rematch' to find the correct match.")
        except Exception as exc:
            click.echo(click.style(f"  ❌  {group_key}: {exc}", fg="red"), err=True)
            any_failed = True
        click.echo()

    sys.exit(1 if any_failed else 0)


def _fmt_age(delta) -> str:
    """Format a timedelta as a human-readable age string."""
    total = int(delta.total_seconds())
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        h, m = divmod(total // 60, 60)
        return f"{h}h {m}m"
    d, rem = divmod(total, 86400)
    return f"{d}d {rem // 3600}h"


@main.command("search")
@click.argument("query")
@_CONFIG_OPTION
def search(query: str, config_path: Optional[Path]) -> None:
    """Search the Calibre library and show matching book IDs.

    QUERY is passed directly to calibredb as a search expression.
    Common forms:
      libris search "Caliban"
      libris search "title:Dune"
      libris search "authors:Herbert"

    The Calibre library path is read from config — no --with-library needed.
    Use the book ID shown here with 'libris revert-import <ID>'.
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    output = _calibredb_list(query, config)

    click.echo()
    if output:
        click.echo(f"  Results for \"{query}\"")
        click.echo(_hr())
        for line in output.splitlines():
            click.echo(f"  {line}")
    else:
        click.echo(f"  No books found matching: {query}")
    click.echo()


@main.command("rematch")
@click.option("--id", "review_id", required=True, type=int,
              help="Review queue position (from 'libris list-review')")
@click.option("--source",
              type=click.Choice(["all", "google", "openlibrary"], case_sensitive=False),
              default="all", show_default=True,
              help="Metadata source(s) to query")
@_CONFIG_OPTION
def rematch(review_id: int, source: str, config_path: Optional[Path]) -> None:
    """Interactively re-query metadata APIs for a review queue item.

    Shows top 3 candidates with full score breakdowns. Refine the search
    query until you find the right match, then select it to import immediately.

    \b
    Example:
      libris rematch --id 1
      libris rematch --id 1 --source google
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    _setup_logging(config.log_level)

    store = _open_store(config.paths.state_db)
    records, _ = _live_review_records(store)
    store.close()

    if not records:
        click.echo("\n  No files in review queue.\n")
        return

    if review_id < 1 or review_id > len(records):
        _die(
            f"ID {review_id} out of range — queue has {len(records)} item(s).\n"
            "  Run 'libris list-review' to see current IDs."
        )

    record = records[review_id - 1]
    file_path = Path(record.current_path)

    if not file_path.exists():
        _die(f"File not found: {file_path}")

    # Header
    click.echo()
    click.echo(f"  Rematching: {file_path.name}")
    if record.matched_title:
        conf_str = f"{record.confidence:.2f}" if record.confidence is not None else "n/a"
        author_str = f"  by {record.matched_author}" if record.matched_author else ""
        click.echo(f"  Current:    {record.matched_title}{author_str}  (score: {conf_str})")
    click.echo()

    stem = file_path.stem
    author_hint = _extract_author_hint(stem)
    year_hint = _extract_year(stem)
    series_hint, _ = _extract_series(stem)
    current_query = _clean_query(stem) or stem

    # For "Series N - Book Title" filenames, use just the book title as the
    # initial query — "Eldest" is far more targeted than "Inheritance Cycle 2 Eldest"
    _dash_parts = re.split(r"\s[-–—]\s", stem, maxsplit=1)
    if series_hint and len(_dash_parts) == 2 and _SERIES_PREFIX.match(_dash_parts[0].strip()):
        current_query = _clean_query(_dash_parts[1]) or _dash_parts[1].strip()

    current_source = source

    from .metadata import google_books, open_library

    while True:
        click.clear()

        # ── File context (always visible after clear) ─────────────────
        click.echo()
        click.echo(f"  Rematching: {file_path.name}")
        if record.matched_title:
            conf_str = f"{record.confidence:.2f}" if record.confidence is not None else "n/a"
            author_str = f"  by {record.matched_author}" if record.matched_author else ""
            click.echo(f"  Current:    {record.matched_title}{author_str}  (score: {conf_str})")
        click.echo()

        # ── API status panel ─────────────────────────────────────────
        google_on = current_source in ("all", "google")
        ol_on = current_source in ("all", "openlibrary")
        g_label = click.style("✅ Google Books", bold=google_on) if google_on \
            else click.style("○  Google Books", dim=True)
        ol_label = click.style("✅ OpenLibrary", bold=ol_on) if ol_on \
            else click.style("○  OpenLibrary", dim=True)
        click.echo(f"  APIs       {g_label}     {ol_label}")
        click.echo(click.style(
            "             libris rematch --id <id> --source <option>  ·  options: all, google, openlibrary",
            dim=True,
        ))
        click.echo(click.style(
            "             or type  /api <option>  in the query prompt",
            dim=True,
        ))
        click.echo()

        # ── Query tips ───────────────────────────────────────────────
        click.echo(click.style("  Tips", bold=True))
        click.echo(click.style("    · Title only", dim=True))
        click.echo(click.style("        \"Caliban and the Witch\"", dim=True))
        click.echo(click.style("    · Add author with 'by' for best results", dim=True))
        click.echo(click.style("        \"Caliban and the Witch by Silvia Federici\"", dim=True))
        click.echo(click.style("    · Use ISBN if known", dim=True))
        click.echo(click.style("        \"9780441013593\"", dim=True))
        click.echo(click.style("    · Type /clear to redraw the screen", dim=True))
        click.echo()

        click.echo(_hr())
        query_str = click.prompt("  Query", default=current_query)

        # ── Handle slash commands ─────────────────────────────────────
        _API_CHOICES = ("all", "google", "openlibrary")
        _cmd = query_str.strip().lower()

        if _cmd == "/clear":
            continue  # click.clear() fires at top of loop

        if _cmd.startswith("/api"):
            parts = _cmd.split()
            if len(parts) == 2 and parts[1] in _API_CHOICES:
                current_source = parts[1]
            else:
                click.echo(click.style(
                    f"\n  Usage: /api <option>  ·  options: {', '.join(_API_CHOICES)}\n",
                    fg="yellow",
                ))
                click.prompt("  Press Enter to continue", default="", show_default=False)
            continue  # Re-render the panel with updated source (no search)

        # Parse "Title by Author" format — keeps title and author in separate
        # API fields for much better results than a fused free-text string.
        _by = " by "
        if _by in query_str.lower():
            _split = query_str.lower().index(_by)
            _parsed_title = query_str[:_split].strip()
            _parsed_author = query_str[_split + len(_by):].strip()
        else:
            _parsed_title = query_str
            _parsed_author = author_hint  # fall back to hint from filename

        search_query = SearchQuery(
            clean_title=_parsed_title,
            author_hint=_parsed_author or author_hint,
            year_hint=year_hint,
        )

        # ── Fetch (with rate-limit retry) ─────────────────────────────
        google_results: list = []
        ol_results: list = []
        _retry_search = True

        while _retry_search:
            _retry_search = False
            google_results = []
            ol_results = []

            click.echo()
            click.echo("  Searching…")
            click.echo()

            with httpx.Client(timeout=12.0) as client:
                if current_source in ("all", "google"):
                    try:
                        google_results = google_books.fetch(
                            search_query,
                            api_key=config.metadata.google_books_api_key,
                            client=client,
                        )
                        _g = len(google_results)
                        click.echo(f"    Google Books   " + (
                            click.style(f"{_g} result(s)", bold=True) if _g
                            else click.style("no results", dim=True)
                        ))
                    except RateLimitError as _rl:
                        _action = _prompt_rate_limit(_rl, path, config)
                        if _action == "key_saved":
                            config = load_config(path)
                            _retry_search = True
                            break
                        elif _action == "wait":
                            _retry_search = True
                            break
                        else:
                            click.echo(click.style(
                                "    Google Books   rate limited — skipped", dim=True
                            ))

                if not _retry_search and current_source in ("all", "openlibrary"):
                    try:
                        ol_results = open_library.fetch(search_query, client=client)
                        _ol = len(ol_results)
                        click.echo(f"    OpenLibrary    " + (
                            click.style(f"{_ol} result(s)", bold=True) if _ol
                            else click.style("no results", dim=True)
                        ))
                    except RateLimitError as _rl:
                        _action = _prompt_rate_limit(_rl, path, config)
                        if _action == "wait":
                            _retry_search = True
                            break
                        else:
                            click.echo(click.style(
                                "    OpenLibrary    rate limited — skipped", dim=True
                            ))

        all_results = dedup_candidates(
            sorted(
                google_results + ol_results,
                key=lambda r: r.confidence,
                reverse=True,
            )
        )[:3]

        current_query = query_str  # persist refined query for next iteration

        if not all_results:
            click.echo()
            click.echo("  No results found.")

            # Try DDG to surface an author/ISBN hint for the user
            from .metadata.ddg import search_book_hints
            with httpx.Client(timeout=8.0) as _ddg_client:
                _hints = search_book_hints(_parsed_title, _ddg_client)
            if _hints:
                click.echo()
                click.echo(click.style("  Web search suggests:", bold=True))
                if _hints.get("author"):
                    click.echo(click.style(f'    Author:  {_hints["author"]}', fg="cyan"))
                    click.echo(click.style(
                        f'    Try:     {_parsed_title} by {_hints["author"]}',
                        dim=True,
                    ))
                if _hints.get("isbn"):
                    click.echo(click.style(f'    ISBN:    {_hints["isbn"]}', fg="cyan"))
                if _hints.get("year"):
                    click.echo(click.style(f'    Year:    {_hints["year"]}', fg="cyan"))
            elif _by not in query_str.lower():
                click.echo(click.style(
                    f'  Tip: add the author using \'by\' — "{_parsed_title} by <Author Name>"',
                    dim=True,
                ))
            click.prompt("\n  Press Enter to refine", default="", show_default=False)
            continue

        click.echo()
        for i, scored in enumerate(all_results, 1):
            c = scored.candidate
            bd = scored.score_breakdown
            source_label = c.source.replace("_", " ").title()
            authors = ", ".join(c.authors) if c.authors else "Unknown"

            click.echo(f"  [{i}]  {c.title}")
            click.echo(f"        {authors}  ·  {source_label}  ·  score {scored.confidence:.2f}")

            details = [p for p in [
                c.publisher,
                str(c.published_year) if c.published_year else None,
                f"ISBN {c.isbn}" if c.isbn else None,
            ] if p]
            if details:
                click.echo(f"        {' · '.join(details)}")

            bd_parts = [
                f"{k} {bd.get(k, 0.0):.2f}/{mx:.2f}"
                for k, mx in _WEIGHT_MAX.items()
            ]
            if bd.get("agreement_bonus"):
                bd_parts.append(f"agreement +{bd['agreement_bonus']:.2f}")
            click.echo(f"        Breakdown:  {' · '.join(bd_parts)}")
            click.echo()

        _num_label = "/".join(str(i) for i in range(1, len(all_results) + 1))
        click.echo(_hr())
        click.echo(f"  [{_num_label}] import    [r] refine query    [q] quit")
        choice = click.prompt("  Choice", default="1").strip().lower()
        click.echo()

        if choice in ("1", "2", "3"):
            idx = int(choice) - 1
            if idx >= len(all_results):
                click.echo(f"  Only {len(all_results)} result(s) shown.\n")
                continue

            selected = all_results[idx]

            cover_path = None
            if config.output.embed_cover_art and selected.candidate.cover_url:
                from .metadata.resolver import _download_cover
                with httpx.Client(timeout=12.0) as client:
                    cover_path = _download_cover(selected.candidate.cover_url, client)

            result = MetadataResult(
                query=search_query,
                best=selected,
                all_candidates=all_results,
                above_threshold=True,
                cover_path=cover_path,
            )

            pipeline = Pipeline(config)
            imported_record = pipeline.force_import(file_path, result)

            if imported_record.state == FileState.IMPORTED:
                pipeline._store.cleanup_stale_review(
                    str(file_path), exclude_id=imported_record.id
                )
                click.echo(f"  ✅  {selected.candidate.title}")
                if selected.candidate.authors:
                    click.echo(f"      Author:  {', '.join(selected.candidate.authors)}")
                click.echo(f"      Score:   {selected.confidence:.2f} (manually selected)")
                click.echo()
                return

            elif (imported_record.state == FileState.REVIEW
                  and imported_record.error_msg
                  and imported_record.error_msg.startswith("Duplicate:")):
                # force_import detected the selected candidate is already in Calibre.
                # Let the user decide — overwrite, discard, or try a different match.
                click.echo(click.style(f"  ⚠   {imported_record.error_msg}", fg="yellow"))
                click.echo()
                click.echo("  What would you like to do?")
                click.echo("  [o]  Overwrite — replace the existing Calibre entry")
                click.echo("  [d]  Discard   — delete this file from the review queue")
                click.echo("  [s]  Skip      — leave in review (use --overwrite later)")
                click.echo("  [r]  Rematch   — try a different match")
                click.echo()
                while True:
                    dup_choice = click.prompt("  Choice", default="r").strip().lower()
                    if dup_choice in ("o", "d", "s", "r"):
                        break
                    click.echo(click.style("  Please enter o, d, s, or r.", fg="yellow"))

                if dup_choice == "o":
                    # import_from_record re-downloads cover using the stored URL
                    # and retries with duplicate_action="import"
                    pipeline.config.metadata.duplicate_action = "import"
                    imported_record = pipeline.import_from_record(imported_record)
                    if imported_record.state == FileState.IMPORTED:
                        pipeline._store.cleanup_stale_review(
                            str(file_path), exclude_id=imported_record.id
                        )
                        click.echo(f"  ✅  {selected.candidate.title}")
                        if selected.candidate.authors:
                            click.echo(f"      Author:  {', '.join(selected.candidate.authors)}")
                        click.echo(f"      Score:   {selected.confidence:.2f} (manually selected)")
                    else:
                        click.echo(
                            f"  ❌  Overwrite failed: {imported_record.error_msg}", err=True
                        )
                        sys.exit(1)
                    click.echo()
                    return

                elif dup_choice == "d":
                    file_path.unlink(missing_ok=True)
                    imported_record.state = FileState.IMPORTED
                    imported_record.error_msg = "Discarded by user (duplicate)"
                    pipeline._store.upsert(imported_record)
                    click.echo(f"  🗑   {file_path.name}")
                    click.echo()
                    return

                elif dup_choice == "s":  # skip — leave in review
                    click.echo(click.style(
                        f"  ↩   {file_path.name} — kept in review.", dim=True
                    ))
                    click.echo()
                    return

                else:  # r — rematch: go back to the query loop
                    click.echo()
                    continue  # back to rematch loop

            else:
                click.echo(f"  ❌  Import failed: {imported_record.error_msg}", err=True)
                sys.exit(1)

        elif choice == "r":
            continue  # current_query already updated above; clear+redraw

        elif choice == "q":
            click.echo("  Cancelled — file remains in review.")
            click.echo()
            return

        else:
            click.echo(click.style("  Please enter 1, 2, 3, r, or q.", fg="yellow"))
            click.prompt("\n  Press Enter to continue", default="", show_default=False)


@main.command("revert-import")
@click.argument("book_id", required=False, default=None, type=int)
@click.option("--search", "search_query", default=None, metavar="QUERY",
              help="Search Calibre by title/author, show matches, then prompt for the ID to revert.")
@_CONFIG_OPTION
def revert_import(
    book_id: Optional[int],
    search_query: Optional[str],
    config_path: Optional[Path],
) -> None:
    """Remove a book from Calibre and return it to review/ for re-processing.

    Provide the Calibre book ID directly, or use --search to find it:

    \b
      libris revert-import 42
      libris revert-import --search "Caliban"

    \b
    Steps performed:
      1. Export the book file(s) from Calibre to review/
      2. Remove the book from the Calibre library
      3. Mark the state record as REVIEW so it appears in list-review
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    _setup_logging(config.log_level)

    if book_id is None and search_query is None:
        _die(
            "Provide a BOOK_ID argument or use --search <query>.\n"
            "  Example: libris revert-import --search \"Caliban\""
        )

    click.echo()

    if search_query is not None:
        output = _calibredb_list(search_query, config)
        if not output:
            click.echo(f"  No books found matching: {search_query}\n")
            sys.exit(0)
        click.echo(f"  Results for \"{search_query}\"")
        click.echo(_hr())
        for line in output.splitlines():
            click.echo(f"  {line}")
        click.echo()
        book_id = click.prompt("  Enter Calibre book ID to revert (or Ctrl-C to cancel)", type=int)
        click.echo()

    calibre = get_calibre(config.calibre)
    store = _open_store(config.paths.state_db)

    review_dir = config.paths.review_dir
    review_dir.mkdir(parents=True, exist_ok=True)

    # Export
    with tempfile.TemporaryDirectory(prefix="libris_revert_") as tmp:
        tmp_path = Path(tmp)
        try:
            exported = calibre.export_book(book_id, tmp_path)
        except Exception as exc:
            store.close()
            _die(f"Export failed: {exc}")

        if not exported:
            store.close()
            _die(
                f"calibredb export returned no files for book ID {book_id}.\n"
                "  Check that the ID is correct and the book has a stored format."
            )

        moved: list[Path] = []
        for f in exported:
            dest = review_dir / f.name
            if dest.exists():
                dest = review_dir / f"{f.stem}_reverted{f.suffix}"
            shutil.move(str(f), str(dest))
            moved.append(dest)
            click.echo(f"  →  review/{dest.name}")

    # Remove from Calibre
    try:
        calibre.remove_book(book_id)
        click.echo(f"  Removed book {book_id} from Calibre library.")
    except Exception as exc:
        store.close()
        click.echo(
            f"\n  ⚠   Calibre remove failed: {exc}\n"
            "  Files are in review/ but the book is still in Calibre — remove it manually.\n",
            err=True,
        )
        sys.exit(1)

    # Update state DB
    record = store.get_by_calibre_id(book_id)
    if record:
        record.state = FileState.REVIEW
        record.current_path = str(moved[0]) if moved else record.current_path
        record.error_msg = None
        store.upsert(record)
        click.echo(f"  State updated: {record.matched_title or Path(record.original_path).name} → REVIEW")
    else:
        click.echo(
            "  ⚠   No state record found for this Calibre ID "
            "(book may have been imported before this version of Libris)."
        )

    store.close()
    click.echo()
    click.echo("  ✅  Done. Run 'libris list-review' to see the queued file.")
    click.echo()


@main.command("reset")
@_CONFIG_OPTION
def reset(config_path: Optional[Path]) -> None:
    """Reset stuck PROCESSING records back to INCOMING.

    If Libris crashes mid-import it can leave records in PROCESSING state,
    which causes subsequent runs to skip those files. Run this to unlock them.
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    store = _open_store(config.paths.state_db)
    count = store.reset_processing()
    store.close()

    click.echo()
    if count == 0:
        click.echo("  No stuck records found — nothing to reset.")
    else:
        click.echo(f"  ✅  Reset {count} stuck PROCESSING record(s) to INCOMING.")
        click.echo("      Re-run 'libris import-one' or 'libris run' to reprocess them.")
    click.echo()


# ---------------------------------------------------------------------------
# clean-library
# ---------------------------------------------------------------------------

@main.command("clean-library")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would happen without making any changes")
@_CONFIG_OPTION
def clean_library(dry_run: bool, config_path: Optional[Path]) -> None:
    """Deduplicate Calibre and re-queue Unknown-metadata books.

    \b
    Two passes over the library:

    1.  Dedup — groups books by normalised title + author surname; for each
        group with more than one entry, merges all formats into the lowest-ID
        book and removes the extras.

    2.  Unknown — finds books whose title or every author is 'Unknown';
        exports the file(s), drops them into incoming/ for the normal
        import pipeline to re-match, and removes the Calibre entry.

    \b
    Run without flags first to preview what will change:

        libris clean-library --dry-run

    Then apply:

        libris clean-library
        libris run              # re-imports anything moved to incoming/
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    _setup_logging(config.log_level)

    calibre = get_calibre(config.calibre)
    store = _open_store(config.paths.state_db)
    incoming = config.watcher.incoming_dir

    books = calibre.list_books()
    if not books:
        click.echo("\n  Calibre library is empty.\n")
        store.close()
        return

    tag = click.style("[dry-run]", fg="cyan") + " " if dry_run else ""

    click.echo(f"\n  {len(books)} book(s) in Calibre library\n")

    # ── Pass 1: Dedup ────────────────────────────────────────────────────────
    click.echo(click.style("  ── Pass 1: Dedup ──", bold=True))

    import unicodedata

    def _norm(s: str) -> str:
        """Lower-case, strip accents, collapse whitespace — for grouping."""
        nfkd = unicodedata.normalize("NFKD", s)
        ascii_s = "".join(c for c in nfkd if not unicodedata.combining(c))
        return " ".join(ascii_s.lower().split())

    # Group by (normalised-title, normalised-first-author-surname)
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for b in books:
        title_key = _norm(b["title"])
        if b["authors"]:
            surname = _norm(b["authors"][0].split()[-1])
        else:
            surname = ""
        groups[(title_key, surname)].append(b)

    dedup_removed = 0
    for key, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        # Sort by ID: keep the lowest (earliest import)
        group_sorted = sorted(group, key=lambda b: b["id"])
        primary = group_sorted[0]
        extras = group_sorted[1:]

        # Collect formats present only in extras
        primary_fmts = set(primary["formats"])
        for extra in extras:
            for fmt in extra["formats"]:
                if fmt not in primary_fmts:
                    click.echo(
                        f"  {tag}  merge format {fmt.upper()} from book "
                        f"{extra['id']} → book {primary['id']} "
                        f"({primary['title']!r})"
                    )
                    if not dry_run:
                        # Export extra's format and add to primary
                        try:
                            tmp = Path(tempfile.mkdtemp(prefix="libris_dedup_"))
                            exported = calibre.export_book(extra["id"], tmp)
                            for ef in exported:
                                if ef.suffix.lstrip(".").lower() == fmt:
                                    calibre.add_format(primary["id"], ef)
                            shutil.rmtree(tmp, ignore_errors=True)
                            primary_fmts.add(fmt)
                        except Exception as exc:
                            click.echo(
                                click.style(f"       ⚠ format merge failed: {exc}", fg="yellow"),
                                err=True,
                            )
                            continue

        for extra in extras:
            click.echo(
                f"  {tag}  remove duplicate book {extra['id']} "
                f"({extra['title']!r} by {', '.join(extra['authors'])}) "
                f"[{', '.join(extra['formats']).upper() or 'no formats'}]"
            )
            if not dry_run:
                try:
                    calibre.remove_book(extra["id"])
                    # Clean up any DB record pointing at this calibre_book_id
                    store.delete_by_calibre_id(extra["id"])
                    dedup_removed += 1
                except Exception as exc:
                    click.echo(
                        click.style(f"       ⚠ remove failed: {exc}", fg="yellow"),
                        err=True,
                    )

    if dedup_removed == 0 and not dry_run:
        click.echo("  No duplicates found.")
    elif dry_run:
        dup_count = sum(len(g) - 1 for g in groups.values() if len(g) > 1)
        click.echo(f"  Would remove {dup_count} duplicate book(s).")

    # ── Pass 2: Unknown metadata ─────────────────────────────────────────────
    click.echo()
    click.echo(click.style("  ── Pass 2: Unknown metadata ──", bold=True))

    unknown_books = [
        b for b in books
        if b["title"].strip().lower() in ("unknown", "")
        or all(a.strip().lower() in ("unknown", "") for a in b["authors"])
    ]

    if not unknown_books:
        click.echo("  No Unknown-metadata books found.")
    else:
        incoming.mkdir(parents=True, exist_ok=True)
        for b in unknown_books:
            label = f"book {b['id']} ({', '.join(b['formats']).upper() or 'no formats'})"
            click.echo(f"  {tag}  re-queue {label}")
            if not dry_run:
                try:
                    tmp = Path(tempfile.mkdtemp(prefix="libris_unknown_"))
                    exported = calibre.export_book(b["id"], tmp)
                    if not exported:
                        click.echo(
                            click.style(f"       ⚠ export returned no files — skipping", fg="yellow"),
                            err=True,
                        )
                        shutil.rmtree(tmp, ignore_errors=True)
                        continue
                    for ef in exported:
                        dest = incoming / ef.name
                        # Avoid clobbering if same filename already there
                        if dest.exists():
                            stem, suf = ef.stem, ef.suffix
                            dest = incoming / f"{stem}_unknown{b['id']}{suf}"
                        shutil.move(str(ef), dest)
                        click.echo(f"       → incoming/{dest.name}")
                    shutil.rmtree(tmp, ignore_errors=True)
                    calibre.remove_book(b["id"])
                    store.delete_by_calibre_id(b["id"])
                except Exception as exc:
                    click.echo(
                        click.style(f"       ⚠ failed: {exc}", fg="yellow"),
                        err=True,
                    )

    click.echo()
    if not dry_run and unknown_books:
        click.echo(
            click.style(
                f"  {len(unknown_books)} book(s) moved to incoming/. "
                "Run 'libris run' to re-import them.",
                fg="green",
            )
        )
    click.echo()

    store.close()


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Suppress noisy third-party HTTP loggers regardless of the configured
    # log level.  httpx/httpcore log full request URLs at INFO level, which
    # would expose API keys embedded in query parameters (e.g. Google Books
    # appends ?key=YOUR_KEY to every request URL).  These logs are never
    # useful in normal operation and create significant terminal noise during
    # interactive commands such as import-one and review-accept.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
