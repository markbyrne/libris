"""Shared data types for the metadata subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BookCandidate:
    """A single book record returned by a metadata source."""

    title: str
    authors: list[str]
    isbn_13: Optional[str] = None
    isbn_10: Optional[str] = None
    published_year: Optional[int] = None
    publisher: Optional[str] = None
    source: str = ""          # "google_books" | "open_library"
    raw_response: dict = field(default_factory=dict, repr=False)

    @property
    def author_surnames(self) -> list[str]:
        """Last token of each author name, lowercased."""
        return [a.split()[-1].lower() for a in self.authors if a.strip()]

    @property
    def isbn(self) -> Optional[str]:
        """Return ISBN-13 if available, else ISBN-10."""
        return self.isbn_13 or self.isbn_10


@dataclass
class SearchQuery:
    """Cleaned query to send to metadata sources."""

    clean_title: str
    author_hint: Optional[str] = None  # may be None if not parseable from filename
    isbn: Optional[str] = None         # extracted from filename, if any
    year_hint: Optional[int] = None    # extracted from filename, if any


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
    best: Optional[ScoredCandidate]
    all_candidates: list[ScoredCandidate] = field(default_factory=list)
    above_threshold: bool = False

    @property
    def title(self) -> str:
        if self.best:
            return self.best.candidate.title
        return self.query.clean_title

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
    def confidence(self) -> float:
        return self.best.confidence if self.best else 0.0
