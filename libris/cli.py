"""CLI entry points for libris.

Commands:
  libris run           — Start the watcher daemon
  libris import-one    — Process a single file (Mac dev / testing)
  libris check-config  — Validate config and print resolved values
  libris list-review   — Show all files in REVIEW state
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .config import load_config
from .pipeline import Pipeline
from .state import FileState, StateStore


@click.group()
def main() -> None:
    """Libris — intelligent book and audiobook organiser for Calibre."""


@main.command()
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, path_type=Path))
def run(config_path: Path) -> None:
    """Start the file watcher daemon."""
    config = load_config(config_path)
    _setup_logging(config.log_level)
    pipeline = Pipeline(config)
    pipeline.run()


@main.command("import-one")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, path_type=Path))
def import_one(file_path: Path, config_path: Path) -> None:
    """Process a single file immediately (no daemon, useful for testing)."""
    config = load_config(config_path)
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
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, path_type=Path))
def check_config(config_path: Path) -> None:
    """Validate config file and print resolved settings."""
    try:
        config = load_config(config_path)
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
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, path_type=Path))
def list_review(config_path: Path) -> None:
    """List all files currently in REVIEW state (low-confidence matches)."""
    config = load_config(config_path)
    store = StateStore(config.paths.state_db)
    records = store.list_by_state(FileState.REVIEW)
    store.close()

    if not records:
        click.echo("No files in review.")
        return

    click.echo(f"{len(records)} file(s) in review:\n")
    for r in records:
        click.echo(f"  {Path(r.current_path).name}")
        click.echo(f"    Matched: {r.matched_title or '(unknown)'}"
                   + (f" by {r.matched_author}" if r.matched_author else ""))
        conf = f"{r.confidence:.2f}" if r.confidence is not None else "n/a"
        click.echo(f"    Score:   {conf}")
        click.echo(f"    Path:    {r.current_path}")
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
