"""CLI entry points for libris.

Commands:
  libris run             — Start the watcher daemon
  libris import-one      — Process a single file (Mac dev / testing)
  libris check-config    — Validate config and print resolved values
  libris list-review     — Show all files in REVIEW state
  libris review-accept   — Force-import a file from review/, bypassing confidence check
  libris reset           — Reset stuck PROCESSING records back to INCOMING
  libris revert-import   — Remove a book from Calibre and return it to review/
  libris search          — Search the Calibre library (uses library path from config)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import click

from .calibre import get_calibre
from .config import load_config
from .pipeline import Pipeline
from .state import FileState, StateStore


# ---------------------------------------------------------------------------
# Config auto-discovery
# ---------------------------------------------------------------------------

# Searched in order when --config is not provided.
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


def _resolve_config(config_path: Optional[Path]) -> Path:
    """Return *config_path* if given, otherwise auto-discover from standard locations.

    Exits with a helpful message if no config file is found.
    """
    if config_path is not None:
        if not config_path.exists():
            click.echo(f"❌ Config file not found: {config_path}", err=True)
            sys.exit(1)
        return config_path

    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            click.echo(f"Using config: {candidate.resolve()}")
            return candidate

    lines = "\n".join(f"  {p}" for p in _CONFIG_SEARCH_PATHS)
    click.echo(
        f"❌ No config file found. Tried:\n{lines}\n"
        "Create config.local.yaml (cp config.example.yaml config.local.yaml) "
        "or pass --config <path>.",
        err=True,
    )
    sys.exit(1)


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
    click.echo(f"Result: {record.state.value}")
    if record.matched_title:
        click.echo(f"Title:  {record.matched_title}")
    if record.matched_author:
        click.echo(f"Author: {record.matched_author}")
    if record.confidence is not None:
        click.echo(f"Score:  {record.confidence:.2f}")
    if record.error_msg:
        click.echo(f"Error:  {record.error_msg}", err=True)
    sys.exit(0 if record.state == FileState.IMPORTED else 1)


@main.command("check-config")
@_CONFIG_OPTION
def check_config(config_path: Optional[Path]) -> None:
    """Validate config file and print resolved settings."""
    path = _resolve_config(config_path)
    try:
        config = load_config(path)
    except Exception as exc:
        click.echo(f"❌ Config error: {exc}", err=True)
        sys.exit(1)

    click.echo("✅ Config valid\n")
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


@main.command("list-review")
@_CONFIG_OPTION
def list_review(config_path: Optional[Path]) -> None:
    """List all files currently in REVIEW state (low-confidence matches)."""
    path = _resolve_config(config_path)
    config = load_config(path)
    store = StateStore(config.paths.state_db)
    records = store.list_by_state(FileState.REVIEW)
    store.close()

    if not records:
        click.echo("No files in review.")
        return

    click.echo(f"{len(records)} file(s) in review:\n")
    for i, r in enumerate(records, 1):
        click.echo(f"[{i}] {Path(r.current_path).name}")
        matched = r.matched_title or "(unknown)"
        if r.matched_author:
            matched += f" by {r.matched_author}"
        click.echo(f"    Matched: {matched}")
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "n/a"
        click.echo(f"    Score:   {conf}")
        # Show path quoted so it can be copy-pasted directly
        click.echo(f"    Path:    \"{r.current_path}\"")
        click.echo()

    click.echo("Accept by ID:    libris review-accept --id <N>")
    click.echo("Accept all:      libris review-accept --accept-all")
    click.echo("Accept by path:  libris review-accept \"<path>\"")


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

    # Validate: exactly one selection method must be provided
    n_methods = sum([file_path is not None, review_id is not None, accept_all])
    if n_methods == 0:
        click.echo(
            "❌ Provide one of: FILE_PATH argument, --id N, or --accept-all\n"
            "Run 'libris list-review' to see queued files and their IDs.",
            err=True,
        )
        sys.exit(1)
    if n_methods > 1:
        click.echo("❌ Only one of FILE_PATH, --id, or --accept-all may be used at a time.", err=True)
        sys.exit(1)

    # ── Resolve target path(s) ────────────────────────────────────────
    store = StateStore(config.paths.state_db)

    if review_id is not None or accept_all:
        records = store.list_by_state(FileState.REVIEW)
        store.close()
        if not records:
            click.echo("No files in review queue.")
            return
        if review_id is not None:
            if review_id < 1 or review_id > len(records):
                click.echo(
                    f"❌ ID {review_id} out of range — queue has {len(records)} item(s).\n"
                    "Run 'libris list-review' to see current IDs.",
                    err=True,
                )
                sys.exit(1)
            targets = [Path(records[review_id - 1].current_path)]
        else:
            targets = [Path(r.current_path) for r in records]
    else:
        store.close()
        targets = [file_path.resolve()]

    # ── Process each target ───────────────────────────────────────────
    config.metadata.confidence_threshold = 0.0
    any_failed = False

    for target in targets:
        if not target.exists():
            click.echo(f"⚠ Skipping (file not found): {target}", err=True)
            any_failed = True
            continue

        pipeline = Pipeline(config)
        record = pipeline.process_file(target)

        if record.state == FileState.IMPORTED:
            pipeline._store.cleanup_stale_review(str(target), exclude_id=record.id)

        status = "✅" if record.state == FileState.IMPORTED else "❌"
        click.echo(f"\n{status} {target.name}  [{record.state.value}]")
        if record.matched_title:
            click.echo(f"   Title:  {record.matched_title}")
        if record.matched_author:
            click.echo(f"   Author: {record.matched_author}")
        if record.confidence is not None:
            click.echo(f"   Score:  {record.confidence:.2f} (threshold overridden)")
        if record.error_msg:
            click.echo(f"   Error:  {record.error_msg}", err=True)
            any_failed = True

    sys.exit(1 if any_failed else 0)


def _calibredb_list(query: str, config) -> str:
    """Run calibredb list with *query* and return the formatted output string.

    Works in both local and docker modes.  Returns an empty string if no
    books match; raises SystemExit on calibredb error.
    """
    if config.calibre.mode == "docker":
        cmd = [
            "docker", "exec", config.calibre.docker_container,
            "calibredb", "list",
            "--search", query,
            "--fields", "id,title,authors",
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
        click.echo(f"❌ calibredb error: {result.stderr.strip()}", err=True)
        sys.exit(1)
    return result.stdout.strip()


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
    click.echo(output if output else f"No books found matching: {query}")


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

    # ── Resolve book_id (direct arg or interactive search) ────────────
    if book_id is None and search_query is None:
        click.echo(
            "❌ Provide a BOOK_ID argument or use --search <query>.\n"
            "Example: libris revert-import --search \"Caliban\"",
            err=True,
        )
        sys.exit(1)

    if search_query is not None:
        output = _calibredb_list(search_query, config)
        if not output:
            click.echo(f"No books found matching: {search_query}")
            sys.exit(0)
        click.echo(f"\nResults for \"{search_query}\":\n")
        click.echo(output)
        click.echo()
        book_id = click.prompt("Enter Calibre book ID to revert (or Ctrl-C to cancel)", type=int)
        click.echo()

    calibre = get_calibre(config.calibre)
    store = StateStore(config.paths.state_db)

    # ── Export book files from Calibre ────────────────────────────────
    review_dir = config.paths.review_dir
    review_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="libris_revert_") as tmp:
        tmp_path = Path(tmp)
        try:
            exported = calibre.export_book(book_id, tmp_path)
        except Exception as exc:
            click.echo(f"❌ Export failed: {exc}", err=True)
            store.close()
            sys.exit(1)

        if not exported:
            click.echo(
                f"❌ calibredb export returned no files for book ID {book_id}.\n"
                "Check that the ID is correct and the book has a stored format.",
                err=True,
            )
            store.close()
            sys.exit(1)

        moved: list[Path] = []
        for f in exported:
            dest = review_dir / f.name
            # Avoid overwriting an existing file in review/
            if dest.exists():
                dest = review_dir / f"{f.stem}_reverted{f.suffix}"
            shutil.move(str(f), str(dest))
            moved.append(dest)
            click.echo(f"  → review/: {dest.name}")

    # ── Remove from Calibre ───────────────────────────────────────────
    try:
        calibre.remove_book(book_id)
        click.echo(f"Removed book {book_id} from Calibre library.")
    except Exception as exc:
        click.echo(
            f"⚠ Calibre remove failed: {exc}\n"
            "Files are in review/ but the book is still in Calibre — remove it manually.",
            err=True,
        )
        store.close()
        sys.exit(1)

    # ── Update state DB ───────────────────────────────────────────────
    record = store.get_by_calibre_id(book_id)
    if record:
        record.state = FileState.REVIEW
        record.current_path = str(moved[0]) if moved else record.current_path
        record.error_msg = None
        store.upsert(record)
        click.echo(
            f"State updated: {record.matched_title or Path(record.original_path).name} → REVIEW"
        )
    else:
        click.echo(
            "⚠ No state record found for this Calibre ID "
            "(book may have been imported before this version of Libris). "
            "DB not updated — files are in review/ regardless."
        )

    store.close()
    click.echo("\n✅ Done. Run 'libris list-review' to see the queued file.")


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

    if count == 0:
        click.echo("No stuck records found — nothing to reset.")
    else:
        click.echo(f"Reset {count} stuck PROCESSING record(s) to INCOMING.")
        click.echo("Re-run 'libris import-one' or 'libris run' to reprocess them.")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
