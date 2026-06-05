"""CLI entry points for libris.

Commands:
  libris run             — Start the watcher daemon
  libris import-one      — Process a single file (Mac dev / testing)
  libris check-config    — Validate config and print resolved values
  libris list-review     — Show all files in REVIEW state
  libris review-accept   — Force-import a file from review/, bypassing confidence check
  libris reset           — Reset stuck PROCESSING records back to INCOMING
  libris recover         — Move failed files back to review/ for re-processing
  libris revert-import   — Remove a book from Calibre and return it to review/
  libris search          — Search the Calibre library (uses library path from config)
  libris rematch         — Interactively re-query metadata APIs for a review item
"""

from __future__ import annotations

import logging
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
from .exceptions import RateLimitError
from .metadata.base import MetadataResult, SearchQuery
from .metadata.resolver import _extract_author_hint, _extract_year
from .metadata.scorer import score_candidate
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
    help="Config file path. Defaults to config.local.yaml in the current directory.",
)

_WEIGHT_MAX = {"isbn": 0.40, "title": 0.30, "author": 0.20, "year": 0.10}


def _resolve_config(config_path: Optional[Path]) -> Path:
    """Return *config_path* if given, otherwise auto-discover from standard locations."""
    if config_path is not None:
        if not config_path.exists():
            _die(f"Config file not found: {config_path}")
        return config_path

    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            click.echo(click.style(f"Using config: {candidate.resolve()}", dim=True))
            return candidate

    tried = "\n".join(f"  {p}" for p in _CONFIG_SEARCH_PATHS)
    _die(
        f"No config file found. Tried:\n{tried}\n"
        "Create config.local.yaml (cp config.example.yaml config.local.yaml) "
        "or pass --config <path>."
    )


def _die(msg: str) -> None:
    """Print an error and exit."""
    click.echo(f"\n  ❌  {msg}\n", err=True)
    sys.exit(1)


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


def _hyperlink(url: str, text: str) -> str:
    """Wrap text in an OSC 8 terminal hyperlink (supported by iTerm2, Terminal, Warp, etc.)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


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
    The function blocks during any countdown (wait choice).
    """
    is_google = error.source == "google_books"
    source_label = "Google Books" if is_google else "OpenLibrary"
    has_key = bool(config.metadata.google_books_api_key) if is_google else False
    wait_secs = error.retry_after or (60 if is_google else 30)

    click.echo()
    click.echo(click.style(f"  ⚠   {source_label} rate limit hit", fg="yellow"))

    if is_google and not has_key:
        click.echo(click.style(
            "      Unauthenticated: ~60 req/min. An API key grants 1,000 req/day.",
            dim=True,
        ))
    elif is_google and has_key:
        click.echo(click.style(
            "      Daily API key quota (1,000 req/day) exhausted.",
            dim=True,
        ))

    click.echo()
    click.echo(f"  [w]  Wait {wait_secs}s and retry")
    if is_google and not has_key:
        click.echo( "  [k]  Add a Google Books API key (free, 1,000 req/day)")
    click.echo(f"  [s]  Skip {source_label} for this search")
    click.echo()

    valid = ("w", "k", "s") if (is_google and not has_key) else ("w", "s")
    while True:
        choice = click.prompt("  Choice", default="w").strip().lower()
        if choice in valid:
            break
        click.echo(click.style(f"  Please enter one of: {', '.join(valid)}", fg="yellow"))

    if choice == "w":
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
        click.echo(f"  Error:   {record.error_msg}", err=True)
    click.echo()

    sys.exit(0 if record.state == FileState.IMPORTED else 1)


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
        click.echo(f"  Library path:   {config.calibre.library_path}")
    else:
        click.echo(f"  Container:      {config.calibre.docker_container}")
    click.echo(f"  Confidence:     {config.metadata.confidence_threshold}")
    click.echo(f"  Mock mode:      {config.metadata.mock_mode}")
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
    store = StateStore(config.paths.state_db)
    records = store.list_by_state(FileState.REVIEW)
    failed_records = store.list_by_state(FileState.FAILED)
    store.close()

    click.echo()
    if not records:
        click.echo("  No files in review.")
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
        matched = r.matched_title or "(unknown)"
        if r.matched_author:
            matched += f"  by {r.matched_author}"
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "n/a"

        # Build the publication detail line from whatever we have stored
        pub_parts = []
        if r.matched_year:
            pub_parts.append(str(r.matched_year))
        if r.matched_publisher:
            pub_parts.append(r.matched_publisher)
        if r.matched_isbn:
            pub_parts.append(f"ISBN {r.matched_isbn}")

        click.echo(f"  [{i}]  {Path(r.current_path).name}")
        click.echo(f"        Matched:  {matched}")
        click.echo(f"        Score:    {conf}")
        if pub_parts:
            click.echo(f"        Info:     {' · '.join(pub_parts)}")
        if r.matched_cover_url:
            link = _hyperlink(r.matched_cover_url, "View cover ↗")
            click.echo(f"        Cover:    {link}")
        click.echo(f"        Path:     \"{r.current_path}\"")
        click.echo()

    click.echo(_hr())
    click.echo("  Accept by ID:    libris review-accept --id <N>")
    click.echo("  Accept all:      libris review-accept --accept-all")
    click.echo("  Accept by path:  libris review-accept \"<path>\"")
    click.echo("  Fix bad match:   libris rematch --id <N>")
    if failed_records:
        click.echo()
        click.echo(click.style(
            f"  ⚠   {len(failed_records)} file(s) also in FAILED state — run 'libris recover'",
            fg="yellow",
        ))
    click.echo()


@main.command("review-accept")
@click.argument("file_path", required=False, default=None, type=click.Path(path_type=Path))
@click.option("--id", "review_id", type=int, default=None,
              help="Accept by review queue position (from 'libris list-review')")
@click.option("--accept-all", "accept_all", is_flag=True, default=False,
              help="Accept every file currently in the review queue")
@_CONFIG_OPTION
def review_accept(
    file_path: Optional[Path],
    review_id: Optional[int],
    accept_all: bool,
    config_path: Optional[Path],
) -> None:
    """Force-import file(s) from review/, bypassing the confidence threshold.

    Three ways to select which file(s) to accept:

    \b
      libris review-accept --id 1
      libris review-accept --accept-all
      libris review-accept "/path/with spaces/file.epub"
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

    store = StateStore(config.paths.state_db)

    # Build a list of (path, record_or_None) pairs so we can use cached
    # metadata from the record and avoid a redundant API call.
    if review_id is not None or accept_all:
        all_records = store.list_by_state(FileState.REVIEW)
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
            target_pairs = [(Path(all_records[review_id - 1].current_path), all_records[review_id - 1])]
        else:
            target_pairs = [(Path(r.current_path), r) for r in all_records]
    else:
        # Path-based: look up the record by current_path so we can use its cache
        resolved = file_path.resolve()
        cached = store.get_by_current_path(str(resolved))
        store.close()
        target_pairs = [(resolved, cached)]

    any_failed = False

    click.echo()
    for target, cached_record in target_pairs:
        if not target.exists():
            click.echo(f"  ⚠   Skipping (file not found): {target}", err=True)
            any_failed = True
            continue

        pipeline = Pipeline(config)

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

        status = "✅" if record.state == FileState.IMPORTED else "❌"
        click.echo(f"  {status}  {target.name}  [{record.state.value}]")
        if record.matched_title:
            click.echo(f"       Title:   {record.matched_title}")
        if record.matched_author:
            click.echo(f"       Author:  {record.matched_author}")
        if record.confidence is not None:
            click.echo(f"       Score:   {record.confidence:.2f}")
        if record.error_msg:
            click.echo(f"       Error:   {record.error_msg}", err=True)
            any_failed = True
        click.echo()

    sys.exit(1 if any_failed else 0)


@main.command("recover")
@click.option("--id", "recover_id", type=int, default=None,
              help="Recover by position from the failed list")
@click.option("--all", "recover_all", is_flag=True, default=False,
              help="Recover every failed file back to review/")
@_CONFIG_OPTION
def recover(
    recover_id: Optional[int],
    recover_all: bool,
    config_path: Optional[Path],
) -> None:
    """Move failed files back to review/ for re-processing.

    Run without arguments to list failed files, then use --id or --all to
    recover them.  Recovered files appear in 'libris list-review' and can be
    fixed with 'libris rematch'.

    \b
      libris recover             # list failed files
      libris recover --id 1     # move file [1] back to review/
      libris recover --all      # move all failed files back to review/
    """
    path = _resolve_config(config_path)
    config = load_config(path)
    store = StateStore(config.paths.state_db)
    records = store.list_by_state(FileState.FAILED)

    if not records:
        store.close()
        click.echo("\n  No files in failed state.\n")
        return

    # ── List mode (no action flag) ────────────────────────────────────────
    if recover_id is None and not recover_all:
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
            click.echo()
        click.echo(_hr())
        click.echo("  Recover by ID:   libris recover --id <N>")
        click.echo("  Recover all:     libris recover --all")
        click.echo()
        return

    # ── Determine targets ─────────────────────────────────────────────────
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

    store = StateStore(config.paths.state_db)
    records = store.list_by_state(FileState.REVIEW)
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
    current_query = _clean_query(stem) or stem
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

        all_results = sorted(
            google_results + ol_results,
            key=lambda r: r.confidence,
            reverse=True,
        )[:3]

        current_query = query_str  # persist refined query for next iteration

        if not all_results:
            click.echo()
            click.echo("  No results found.")
            if _by not in query_str.lower():
                click.echo(click.style(
                    "  Tip: add the author using 'by' — "
                    f'"{_parsed_title} by <Author Name>"',
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
            else:
                click.echo(f"  ❌  Import failed: {imported_record.error_msg}", err=True)
                sys.exit(1)
            click.echo()
            return

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
    store = StateStore(config.paths.state_db)

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
    store = StateStore(config.paths.state_db)
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
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
