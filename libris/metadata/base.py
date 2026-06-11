"""Shared data types for the metadata subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Descriptive User-Agent for all metadata API calls.  OpenLibrary's API
# policy asks clients to identify themselves with a contact point; sending
# it everywhere costs nothing and keeps the sources consistent.
USER_AGENT = "libris (+https://github.com/markbyrne/libris)"


@dataclass
class BookCandidate:
    """A single book record returned by a metadata source."""

    title: str
    authors: list[str]
    isbn_13: str | None = None
    isbn_10: str | None = None
    published_year: int | None = None
    publisher: str | None = None
    description: str | None = None      # synopsis / back-cover text
    language: str | None = None         # ISO 639-1 code e.g. "en"
    series: str | None = None           # series name
    series_index: float | None = None   # position in series
    cover_url: str | None = None        # remote URL for cover image
    categories: list[str] = field(default_factory=list)
    source: str = ""                       # "google_books" | "open_library"
    raw_response: dict = field(default_factory=dict, repr=False)

    @property
    def author_surnames(self) -> list[str]:
        """Last token of each author name, lowercased."""
        return [a.split()[-1].lower() for a in self.authors if a.strip()]

    @property
    def isbn(self) -> str | None:
        """Return ISBN-13 if available, else ISBN-10."""
        return self.isbn_13 or self.isbn_10


@dataclass
class SearchQuery:
    """Cleaned query to send to metadata sources."""

    clean_title: str
    author_hint: str | None = None       # may be None if not parseable from filename
    isbn: str | None = None              # extracted from filename, if any
    year_hint: int | None = None         # extracted from filename, if any
    series_hint: str | None = None       # extracted from filename, if any
    series_index_hint: float | None = None  # extracted from filename, if any


@dataclass
class ScoredCandidate:
    """A BookCandidate paired with a confidence score."""

    candidate: BookCandidate
    confidence: float                          # 0.0–1.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"ScoredCandidate(title={self.candidate.title!r}, "
            f"confidence={self.confidence:.2f}, source={self.candidate.source!r})"
        )


@dataclass
class MetadataResult:
    """Final resolved metadata for a file, ready to drive import decisions."""

    query: SearchQuery
    best: ScoredCandidate | None
    all_candidates: list[ScoredCandidate] = field(default_factory=list)
    above_threshold: bool = False
    cover_path: Path | None = None      # downloaded cover image (temp file)

    # ── Convenience accessors ─────────────────────────────────────────────────

    @property
    def title(self) -> str:
        return self.best.candidate.title if self.best else self.query.clean_title

    @property
    def author(self) -> str:
        if self.best and self.best.candidate.authors:
            return ", ".join(self.best.candidate.authors)
        return self.query.author_hint or ""

    @property
    def year(self) -> str:
        if self.best and self.best.candidate.published_year:
            return str(self.best.candidate.published_year)
        return str(self.query.year_hint) if self.query.year_hint else ""

    @property
    def publisher(self) -> str:
        return self.best.candidate.publisher or "" if self.best else ""

    @property
    def description(self) -> str:
        return self.best.candidate.description or "" if self.best else ""

    @property
    def language(self) -> str:
        return self.best.candidate.language or "" if self.best else ""

    @property
    def series(self) -> str | None:
        return self.best.candidate.series if self.best else None

    @property
    def series_index(self) -> float | None:
        return self.best.candidate.series_index if self.best else None

    @property
    def isbn(self) -> str | None:
        return self.best.candidate.isbn if self.best else None

    @property
    def confidence(self) -> float:
        return self.best.confidence if self.best else 0.0
