"""Shared pytest fixtures for libris tests."""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from libris.config import (
    CalibreConfig,
    Config,
    MetadataConfig,
    NtfyConfig,
    OutputConfig,
    PathsConfig,
    WatcherConfig,
)
from libris.metadata.base import BookCandidate, ScoredCandidate, SearchQuery
from libris.metadata.scorer import score_candidate
from libris.state import FileRecord, FileState
from libris.watcher.base import FileEvent, Watcher

# ---------------------------------------------------------------------------
# Config fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_config(tmp_path: Path) -> Config:
    """Fully resolved Config pointing to tmp directories."""
    return Config(
        watcher=WatcherConfig(
            incoming_dir=tmp_path / "incoming",
            poll_interval_seconds=1,
        ),
        paths=PathsConfig(
            staging_dir=tmp_path / "staging",
            review_dir=tmp_path / "review",
            failed_dir=tmp_path / "failed",
            state_db=tmp_path / "state.db",
        ),
        calibre=CalibreConfig(
            mode="local",
            library_path=tmp_path / "calibre-library",
        ),
        metadata=MetadataConfig(
            confidence_threshold=0.75,
            mock_mode=True,
            overwrite_existing=True,
        ),
        output=OutputConfig(
            preferred_ebook_format="epub",
            preferred_audio_format="m4b",
            embed_cover_art=False,   # no HTTP in unit tests
        ),
        ntfy=NtfyConfig(
            topic="test-topic",
            enabled=False,       # never send real notifications in tests
        ),
        log_level="DEBUG",
    )


@pytest.fixture
def config_yaml(tmp_path: Path) -> Path:
    """Write a minimal valid YAML config and return its path."""
    lib = tmp_path / "calibre-library"
    lib.mkdir()
    content = textwrap.dedent(f"""
        watcher:
          incoming_dir: {tmp_path}/incoming
        paths:
          staging_dir: {tmp_path}/staging
          review_dir: {tmp_path}/review
          failed_dir: {tmp_path}/failed
          state_db: {tmp_path}/state.db
        calibre:
          mode: local
          library_path: {lib}
        metadata:
          confidence_threshold: 0.75
          mock_mode: true
          overwrite_existing: true
        output:
          preferred_ebook_format: epub
          preferred_audio_format: m4b
          embed_cover_art: false
        ntfy:
          topic: test
          enabled: false
        log_level: DEBUG
    """)
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# FakeWatcher — feeds pre-built FileEvents without spawning a subprocess
# ---------------------------------------------------------------------------

class FakeWatcher(Watcher):
    """Test double: yields a pre-defined sequence of FileEvent objects."""

    def __init__(self, events: list[FileEvent]) -> None:
        self._events = events

    def events(self) -> Iterator[FileEvent]:
        yield from self._events

    def stop(self) -> None:
        pass


@pytest.fixture
def fake_watcher_factory():
    """Return a factory that builds a FakeWatcher from a list of paths."""
    def _make(paths: list[Path]) -> FakeWatcher:
        return FakeWatcher([
            FileEvent(path=p, event_type="created")
            for p in paths
        ])
    return _make


# ---------------------------------------------------------------------------
# Canned metadata candidates
# ---------------------------------------------------------------------------

PROJECT_HAIL_MARY = BookCandidate(
    title="Project Hail Mary",
    authors=["Andy Weir"],
    isbn_13="9780593135204",
    published_year=2021,
    source="google_books",
)

ERAGON = BookCandidate(
    title="Eragon",
    authors=["Christopher Paolini"],
    isbn_13="9780385737951",
    published_year=2003,
    source="google_books",
)

DUNE = BookCandidate(
    title="Dune",
    authors=["Frank Herbert"],
    isbn_13="9780441013593",
    published_year=1965,
    source="google_books",
)


def make_scored(candidate: BookCandidate, query_title: str, **query_kwargs) -> ScoredCandidate:
    """Helper: score a canned candidate against a query title."""
    query = SearchQuery(clean_title=query_title, **query_kwargs)
    return score_candidate(query, candidate)


# ---------------------------------------------------------------------------
# FileRecord helper
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_record(tmp_path: Path) -> FileRecord:
    """A minimal INCOMING FileRecord pointing to a non-existent file."""
    fake_path = tmp_path / "sample.epub"
    return FileRecord(
        id="abc123",
        original_path=str(fake_path),
        current_path=str(fake_path),
        media_type="ebook",
        state=FileState.INCOMING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
