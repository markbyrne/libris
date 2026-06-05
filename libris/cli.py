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
  libris rematch         — Interactively re-query metadata APIs for a review item
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
import httpx

from .calibre import get_calibre
from .cleaner import clean_query as _clean_query
from .config import load_config
from .metadata.base import MetadataResult, SearchQuery
from .metadata.resolver import _extract_author_hint, _extract_year
from .metadata.scorer import score_candidate
from .pipeline import Pipeline
from .state import FileState, StateStore


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
    click.echo()


@main.command("list-review")
@_CONFIG_OPTION
def list_review(config_path: Optional[Path]) -> None:
    """List all files currently in REVIEW state (low-confidence matches)."""
    path = _resolve_config(config_path)
    config = load_config(path)
    store = StateStore(config.paths.state_db)
    records = store.list_by_state(FileState.REVIEW)
    store.close()

    click.echo()
    if not records:
        click.echo("  No files in review.")
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

        click.echo(f"  [{i}]  {Path(r.current_path).name}")
        click.echo(f"        Matched:  {matched}")
        click.echo(f"        Score:    {conf}")
        click.echo(f"        Path:     \"{r.current_path}\"")
        click.echo()

    click.echo(_hr())
    click.echo("  Accept by ID:    libris review-accept --id <N>")
    click.echo("  Accept all:      libris review-accept --accept-all")
    click.echo("  Accept by path:  libris review-accept \"<path>\"")
    click.echo("  Fix bad match:   libris rematch --id <N>")
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

    if review_id is not None or accept_all:
        records = store.list_by_state(FileState.REVIEW)
        store.close()
        if not records:
            click.echo("\n  No files in review queue.\n")
            return
        if review_id is not None:
            if review_id < 1 or review_id > len(records):
                _die(
                    f"ID {review_id} out of range — queue has {len(records)} item(s).\n"
                    "  Run 'libris list-review' to see current IDs."
                )
            targets = [Path(records[review_id - 1].current_path)]
        else:
            targets = [Path(r.current_path) for r in records]
    else:
        store.close()
        targets = [file_path.resolve()]

    config.metadata.confidence_threshold = 0.0
    any_failed = False

    click.echo()
    for target in targets:
        if not target.exists():
            click.echo(f"  ⚠   Skipping (file not found): {target}", err=True)
            any_failed = True
            continue

        pipeline = Pipeline(config)
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
            click.echo(f"       Score:   {record.confidence:.2f} (threshold overridden)")
        if record.error_msg:
            click.echo(f"       Error:   {record.error_msg}", err=True)
            any_failed = True
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
        click.echo(_hr())

        # ── API status panel ─────────────────────────────────────────
        google_on = current_source in ("all", "google")
        ol_on = current_source in ("all", "openlibrary")
        g_label = click.style("✅ Google Books", bold=google_on) if google_on \
            else click.style("○  Google Books", dim=True)
        ol_label = click.style("✅ OpenLibrary", bold=ol_on) if ol_on \
            else click.style("○  OpenLibrary", dim=True)
        click.echo(f"  APIs       {g_label}     {ol_label}")
        click.echo(click.style(
            "             Change at the Source prompt  ·  libris rematch --id <id> --source <option>",
            dim=True,
        ))
        click.echo(click.style(
            "             or in query prompt  ·  /api <option>  ·  options: all, google, openlibrary",
            dim=True,
        ))
        click.echo()

        # ── Query tips ───────────────────────────────────────────────
        click.echo(click.style("  Tips", bold=True))
        click.echo(click.style("    · Use the book title for best results", dim=True))
        click.echo(click.style("        \"Caliban and the Witch\"", dim=True))
        click.echo(click.style("    · Add author surname to narrow results", dim=True))
        click.echo(click.style("        \"Caliban Federici\"", dim=True))
        click.echo(click.style("    · Use ISBN if known", dim=True))
        click.echo(click.style("        \"9780441013593\"", dim=True))
        click.echo()

        click.echo(_hr())
        query_str = click.prompt("  Query", default=current_query)

        # ── Handle /api slash command ─────────────────────────────────
        _API_CHOICES = ("all", "google", "openlibrary")
        if query_str.strip().lower().startswith("/api"):
            parts = query_str.strip().split()
            if len(parts) == 2 and parts[1].lower() in _API_CHOICES:
                current_source = parts[1].lower()
                click.echo()
                click.echo(f"  Source updated to: {current_source}")
                click.echo()
            else:
                click.echo()
                click.echo(click.style(
                    f"  Usage: /api <option>  ·  options: {', '.join(_API_CHOICES)}",
                    fg="yellow",
                ))
                click.echo()
            continue  # Re-render the panel with the new source (no search)

        source_str = click.prompt(
            "  Source",
            default=current_source,
            type=click.Choice(list(_API_CHOICES), case_sensitive=False),
        )
        click.echo()
        click.echo("  Searching…")

        search_query = SearchQuery(
            clean_title=query_str,
            author_hint=author_hint,
            year_hint=year_hint,
        )

        with httpx.Client(timeout=12.0) as client:
            google_results: list = []
            ol_results: list = []
            if source_str in ("all", "google"):
                google_results = google_books.fetch(
                    search_query,
                    api_key=config.metadata.google_books_api_key,
                    client=client,
                )
            if source_str in ("all", "openlibrary"):
                ol_results = open_library.fetch(search_query, client=client)

        all_results = sorted(
            google_results + ol_results,
            key=lambda r: r.confidence,
            reverse=True,
        )[:3]

        if not all_results:
            click.echo("  No results found — try a different query.")
            click.echo()
            current_query = query_str
            current_source = source_str
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

        click.echo(_hr())
        click.echo("  [1/2/3] import    [r] refine query    [q] quit")
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
            current_query = query_str
            current_source = source_str
            click.echo()
            continue

        elif choice == "q":
            click.echo("  Cancelled — file remains in review.")
            click.echo()
            return

        else:
            click.echo("  Please enter 1, 2, 3, r, or q.\n")


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
