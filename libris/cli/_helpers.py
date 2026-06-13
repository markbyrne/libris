"""Pure rendering and formatting helpers for the CLI.

No side-effects, no I/O — all functions here take data and produce
strings or write to stdout via click.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from ..state import FileRecord, FileState, StateStore


def _hr(width: int = 50) -> str:
    return "  " + "─" * width


def _has_match(record: FileRecord) -> bool:
    """Return True if the record has a real API-sourced metadata candidate."""
    return record.matched_metadata_json is not None


def _hyperlink(url: str, text: str) -> str:
    """Wrap text in an OSC 8 terminal hyperlink (iTerm2, Terminal, Warp, etc.)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def _fmt_age(delta: timedelta) -> str:
    """Format a timedelta as a human-readable age string, e.g. '2d 4h', '3h 12m', '45m'."""
    total = int(delta.total_seconds())
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        h, m = divmod(total // 60, 60)
        return f"{h}h {m}m"
    d, rem = divmod(total, 86400)
    return f"{d}d {rem // 3600}h"


def _render_review_hints(has_audio: bool = False, has_dupes: bool = False) -> None:
    """Print the standard action-hints footer for the review queue."""
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
    if has_audio:
        click.echo("  Mark as part:    libris mark-as-part --id <N> --part <num> --total <total>")


def _live_review_records(store: StateStore) -> tuple[list[FileRecord], int]:
    """Return REVIEW records whose file still exists on disk, in stable order.

    Records pointing to missing files are silently excluded so list-review,
    rematch, review-accept, and show-cover share consistent positional IDs.

    Returns (live_records, stale_count).
    """
    records = store.list_by_state(FileState.REVIEW)
    live = [r for r in records if Path(r.current_path).exists()]
    return live, len(records) - len(live)


def _render_review_record(i: int, r: FileRecord) -> None:
    """Print a single review-queue entry (shared by list-review and show-cover)."""
    is_dup = bool(r.error_msg and r.error_msg.startswith("Duplicate:"))
    dup_tag = "  " + click.style("[!]", fg="yellow", bold=True) if is_dup else ""
    click.echo(f"  [{i}]{dup_tag}  {Path(r.current_path).name}")

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


def _render_failed_list(records: list[FileRecord]) -> None:
    """Render the failed queue in the standard list-failed format.

    Shared by list-failed, remove, and recover so they all show the same
    updated view after completing their action.
    """
    click.echo()
    if not records:
        click.echo("  No files in failed state.")
        click.echo()
        return

    live = [(r, Path(r.current_path).exists()) for r in records]
    stale_count = sum(1 for _, exists in live if not exists)

    click.echo(f"  {len(records)} file(s) in failed state")
    click.echo(_hr())
    click.echo()
    for i, (r, exists) in enumerate(live, 1):
        name = Path(r.current_path).name
        _ts = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        age = _fmt_age(datetime.now(timezone.utc) - _ts)
        if not exists:
            click.echo(click.style(f"  [{i}]  {name}  (file missing — {age} ago)", dim=True))
        else:
            click.echo(f"  [{i}]  {name}  ({age} ago)")
        if r.error_msg:
            click.echo(f"        Error:  {r.error_msg[:120]}")
        if not exists:
            click.echo(click.style(f"        libris remove --id {i}", dim=True))
        click.echo()

    click.echo(_hr())
    click.echo("  Recover by ID:   libris recover --id <N>")
    click.echo("  Recover all:     libris recover --all")
    click.echo("  Remove by ID:    libris remove --id <N>")
    click.echo("  Remove chaff:    libris remove --chaff")
    click.echo("  Remove all:      libris remove --all")
    if stale_count:
        click.echo()
        click.echo(click.style(
            f"  ⚠   {stale_count} record(s) with missing files — run 'libris remove --id <N>' to clean up.",
            fg="yellow",
        ))
    click.echo()
