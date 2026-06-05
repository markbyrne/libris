"""High-level metadata resolver: queries both sources, fuses results, applies threshold."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from ..cleaner import clean_query, extract_isbn
from ..config import MetadataConfig
from .base import MetadataResult, SearchQuery
from .scorer import build_result

# Fixture data for mock_mode (keyed by clean title, case-insensitive)
_MOCK_CANDIDATES: dict[str, list[dict]] = {}

log = logging.getLogger(__name__)


def resolve_metadata(
    filename: str,
    config: MetadataConfig,
    client: Optional[httpx.Client] = None,
) -> MetadataResult:
    """Resolve book metadata from a filename.

    Steps:
    1. Strip noise from filename → clean query + optional ISBN hint
    2. Query Google Books and OpenLibrary (or return mock data)
    3. Score all candidates
    4. Apply cross-source agreement bonus
    5. Return MetadataResult with above_threshold flag set

    Args:
        filename: Raw filename (basename, with or without extension).
        config: MetadataConfig with threshold and optional API key.
        client: Optional httpx.Client for dependency injection in tests.

    Returns:
        MetadataResult. best may be None if no candidates were found.
    """
    # ── Build query ──────────────────────────────────────────────────────
    stem = Path(filename).stem
    clean = clean_query(stem)
    isbn = extract_isbn(stem)
    year = _extract_year(stem)
    author_hint = _extract_author_hint(stem)

    query = SearchQuery(
        clean_title=clean or stem,   # fallback to stem if everything was stripped
        author_hint=author_hint,
        isbn=isbn,
        year_hint=year,
    )

    log.info(
        "metadata.resolving",
        extra={
            "filename": filename,
            "clean_title": query.clean_title,
            "isbn": query.isbn,
            "author_hint": query.author_hint,
        },
    )

    # ── Fetch ────────────────────────────────────────────────────────────
    if config.mock_mode:
        google_scored, ol_scored = _mock_fetch(query)
    else:
        from . import google_books, open_library
        _client = client or httpx.Client(timeout=12.0)
        google_scored = google_books.fetch(query, api_key=config.google_books_api_key, client=_client)
        ol_scored = open_library.fetch(query, client=_client)

    # ── Fuse & score ─────────────────────────────────────────────────────
    result = build_result(query, google_scored, ol_scored, config.confidence_threshold)

    log.info(
        "metadata.resolved",
        extra={
            "filename": filename,
            "best_title": result.title,
            "best_author": result.author,
            "confidence": f"{result.confidence:.2f}",
            "above_threshold": result.above_threshold,
        },
    )

    return result


# ---------------------------------------------------------------------------
# Mock support
# ---------------------------------------------------------------------------

def register_mock_candidates(title_key: str, candidates: list[dict]) -> None:
    """Register fixture data for mock_mode. Used in tests via conftest."""
    _MOCK_CANDIDATES[title_key.lower()] = candidates


def _mock_fetch(query: SearchQuery):  # type: ignore[return]
    """Return canned ScoredCandidates for testing."""
    from .base import BookCandidate
    from .scorer import score_candidate

    key = query.clean_title.lower()
    raw_list = _MOCK_CANDIDATES.get(key, [])
    google_scored = []
    ol_scored = []
    for raw in raw_list:
        c = BookCandidate(
            title=raw.get("title", query.clean_title),
            authors=raw.get("authors", []),
            isbn_13=raw.get("isbn_13"),
            isbn_10=raw.get("isbn_10"),
            published_year=raw.get("published_year"),
            source=raw.get("source", "google_books"),
        )
        scored = score_candidate(query, c)
        if c.source == "open_library":
            ol_scored.append(scored)
        else:
            google_scored.append(scored)
    return google_scored, ol_scored


# ---------------------------------------------------------------------------
# Filename hint extractors
# ---------------------------------------------------------------------------

def _extract_year(stem: str) -> Optional[int]:
    """Extract a 4-digit year from the stem (first match in range 1800–2099)."""
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", stem)
    return int(m.group(1)) if m else None


def _extract_author_hint(stem: str) -> Optional[str]:
    """Heuristic: if stem contains ' - Author Name', extract the part after ' - '.

    Rules for a valid author candidate:
    - 1–4 tokens
    - Every token starts with an uppercase letter (proper name)
    - Not a known noise word

    Returns None if no confident author hint is found.
    """
    _NOISE_WORDS = frozenset({
        "retail", "unabridged", "abridged", "audiobook", "ebook",
        "digital", "hq", "repack", "proper",
    })

    parts = re.split(r"\s[-–—]\s", stem, maxsplit=1)
    if len(parts) != 2:
        return None

    for part in parts:
        tokens = part.strip().split()
        if not (1 <= len(tokens) <= 4):
            continue
        # All tokens must start with uppercase — proper names only, not generic lowercase tags
        if not all(t[0].isupper() for t in tokens if t):
            continue
        # Reject known noise words (case-insensitive)
        if any(t.lower() in _NOISE_WORDS for t in tokens):
            continue
        return part.strip()

    return None
