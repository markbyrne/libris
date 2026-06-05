"""High-level metadata resolver: queries both sources, fuses results, applies threshold."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import httpx

from ..cleaner import clean_query, extract_isbn
from ..config import MetadataConfig
from ..exceptions import RateLimitError
from .base import MetadataResult, SearchQuery
from .scorer import build_result

# Fixture data for mock_mode (keyed by clean title, case-insensitive)
_MOCK_CANDIDATES: dict[str, list[dict]] = {}

log = logging.getLogger(__name__)


def resolve_metadata(
    filename: str,
    config: MetadataConfig,
    client: Optional[httpx.Client] = None,
    embed_cover: bool = True,
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
    series_hint, series_index_hint = _extract_series(stem)

    query = SearchQuery(
        clean_title=clean or stem,   # fallback to stem if everything was stripped
        author_hint=author_hint,
        isbn=isbn,
        year_hint=year,
        series_hint=series_hint,
        series_index_hint=series_index_hint,
    )

    log.info(
        "metadata.resolving",
        extra={
            "source_file": filename,
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

        google_scored = []
        try:
            google_scored = google_books.fetch(
                query, api_key=config.google_books_api_key, client=_client
            )
        except RateLimitError as exc:
            # Rate limited during automatic pipeline run — treat as zero results so
            # the file lands in review/ rather than failed/.  The user can then run
            # `libris rematch` which surfaces the rate limit interactively.
            log.warning(
                "metadata.rate_limited",
                extra={"source": exc.source, "retry_after": exc.retry_after},
            )

        ol_scored = []
        try:
            ol_scored = open_library.fetch(query, client=_client)
        except RateLimitError as exc:
            log.warning(
                "metadata.rate_limited",
                extra={"source": exc.source, "retry_after": exc.retry_after},
            )

    # ── Fuse & score ─────────────────────────────────────────────────────
    result = build_result(query, google_scored, ol_scored, config.confidence_threshold)

    # ── Apply filename series hints ───────────────────────────────────────
    # If the filename contained a series hint and the winning candidate has no
    # series data from the API, fill it in from the filename.
    if result.best and query.series_hint:
        c = result.best.candidate
        if not c.series:
            c.series = query.series_hint
        if c.series_index is None and query.series_index_hint is not None:
            c.series_index = query.series_index_hint

    # ── Download cover art ───────────────────────────────────────────────
    if embed_cover and result.best and result.best.candidate.cover_url and not config.mock_mode:
        _client = client or httpx.Client(timeout=12.0)
        result.cover_path = _download_cover(result.best.candidate.cover_url, _client)

    log.info(
        "metadata.resolved",
        extra={
            "source_file": filename,
            "best_title": result.title,
            "best_author": result.author,
            "confidence": f"{result.confidence:.2f}",
            "above_threshold": result.above_threshold,
            "has_cover": result.cover_path is not None,
        },
    )

    return result


def _download_cover(url: str, client: httpx.Client) -> Optional[Path]:
    """Download cover image to a temp file. Returns path or None on failure."""
    import tempfile
    try:
        response = client.get(url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        ext = ".jpg" if "jpeg" in content_type or "jpg" in content_type else ".png"
        fd, path_str = tempfile.mkstemp(suffix=ext, prefix="libris_cover_")
        import os
        os.close(fd)
        cover_path = Path(path_str)
        cover_path.write_bytes(response.content)
        log.debug("metadata.cover_downloaded", extra={"url": url, "path": str(cover_path)})
        return cover_path
    except Exception as exc:
        log.warning("metadata.cover_download_failed", extra={"url": url, "error": str(exc)})
        return None


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
    - No bare numbers (avoids "Series 1 - Title" misidentifying the title as author)

    Returns None if no confident author hint is found.
    """
    _NOISE_WORDS = frozenset({
        "retail", "unabridged", "abridged", "audiobook", "ebook",
        "digital", "hq", "repack", "proper",
    })

    parts = re.split(r"\s[-–—]\s", stem, maxsplit=1)
    if len(parts) != 2:
        return None

    # If either part contains a bare number it's almost certainly a
    # "Series N - Title" pattern, not "Title - Author" — bail out early.
    if any(re.search(r'\b\d+\b', p) for p in parts):
        return None

    def _looks_like_author(s: str) -> bool:
        tokens = s.strip().split()
        if not (1 <= len(tokens) <= 4):
            return False
        if not all(t[0].isupper() for t in tokens if t):
            return False
        if any(t.lower() in _NOISE_WORDS for t in tokens):
            return False
        return True

    # Convention is almost always "Title - Author Name", so check the part
    # after the dash first.  Fall back to the first part for the rarer
    # "Author - Title" pattern.
    for part in (parts[1], parts[0]):
        if _looks_like_author(part):
            return part.strip()

    return None


# Series extraction patterns (applied in order, first match wins)
_SERIES_IN_TITLE = re.compile(
    r'\(([^)]+?)[,\s]+(?:book|vol(?:ume)?|part|#)\s*(\d+(?:\.\d+)?)\)',
    re.IGNORECASE,
)
_SERIES_IN_TITLE_SIMPLE = re.compile(
    r'\(([^)#,]+?),?\s+#(\d+(?:\.\d+)?)\)',
    re.IGNORECASE,
)
_SERIES_PREFIX = re.compile(
    r'^(.+?)\s+(\d+(?:\.\d+)?)\s*$',
)


def _extract_series(stem: str) -> tuple[Optional[str], Optional[float]]:
    """Extract series name and index from a filename stem.

    Handles the two most common patterns:
      "Inheritance Cycle 1 - Eragon"   → ("Inheritance Cycle", 1.0)
      "Eragon (Inheritance Cycle, #1)" → ("Inheritance Cycle", 1.0)
      "Eragon (Inheritance Cycle #1)"  → ("Inheritance Cycle", 1.0)
      "Harry Potter (Book 3)"          → ("Harry Potter", 3.0)

    Returns (None, None) if no series pattern is found.
    """
    # Pattern 1: "Series N - Title" prefix
    parts = re.split(r"\s[-–—]\s", stem, maxsplit=1)
    if len(parts) == 2:
        m = _SERIES_PREFIX.match(parts[0].strip())
        if m:
            try:
                return m.group(1).strip(), float(m.group(2))
            except ValueError:
                pass

    # Pattern 2: "Title (Series, #N)" or "Title (Series Book N)" in stem
    for pat in (_SERIES_IN_TITLE, _SERIES_IN_TITLE_SIMPLE):
        m = pat.search(stem)
        if m:
            try:
                return m.group(1).strip(), float(m.group(2))
            except ValueError:
                return m.group(1).strip(), None

    return None, None
