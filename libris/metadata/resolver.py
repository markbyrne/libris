"""High-level metadata resolver: queries both sources, fuses results, applies threshold."""

from __future__ import annotations

import atexit
import logging
import os
import re
import signal
from pathlib import Path

import httpx

from .._constants import HTTP_TIMEOUT_API, HTTP_TIMEOUT_COVER
from ..cleaner import DoubleDashResult, clean_query, extract_isbn, parse_double_dash
from ..config import MetadataConfig
from ..exceptions import RateLimitError
from .base import USER_AGENT, MetadataResult, SearchQuery
from .scorer import build_result

# Fixture data for mock_mode (keyed by clean title, case-insensitive)
_MOCK_CANDIDATES: dict[str, list[dict]] = {}

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cover temp-file cleanup
# ---------------------------------------------------------------------------
# Track temp cover paths created in this process so we can remove them on
# exit or SIGTERM.  Without cleanup a kill -TERM (e.g. systemd stop) leaves
# libris_cover_* files accumulating in /tmp.
_pending_cover_paths: set[str] = set()


def _cleanup_cover_temps() -> None:
    """Remove any outstanding cover temp files (atexit + SIGTERM handler)."""
    for p in list(_pending_cover_paths):
        try:
            os.unlink(p)
        except OSError:
            pass
    _pending_cover_paths.clear()


def _sigterm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    _cleanup_cover_temps()
    # Re-raise as SystemExit so atexit handlers run and the process exits cleanly.
    raise SystemExit(0)


atexit.register(_cleanup_cover_temps)
import threading as _threading
if _threading.current_thread() is _threading.main_thread():
    signal.signal(signal.SIGTERM, _sigterm_handler)


def resolve_metadata(
    filename: str,
    config: MetadataConfig,
    client: httpx.Client | None = None,
    fetch_cover: bool = True,
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

    # Structured " -- " convention ({title} -- {author} -- {year} --
    # {publisher} -- {hash}): the fields are authoritative — use them
    # instead of heuristics over the whole stem.
    dd = parse_double_dash(stem)
    if dd:
        clean = clean_query(dd["title"]) or dd["title"]
        author_hint = dd["author"] or author_hint
        year = dd["year"] or year
        if dd.get("isbn"):
            isbn = dd["isbn"]
        if dd.get("series"):
            series_hint = dd["series"]
        if dd.get("series_index") is not None:
            series_index_hint = float(dd["series_index"])

    # For "Series N - Book Title" filenames, query by book title only.
    # "Inheritance Cycle 2 - Eldest" → query "Eldest", not "Inheritance Cycle 2 Eldest",
    # which would otherwise match a collection box-set instead of the individual book.
    _dash_parts = re.split(r"\s[-–—]\s", stem, maxsplit=1)
    if series_hint and len(_dash_parts) == 2 and _SERIES_PREFIX.match(_dash_parts[0].strip()):
        clean = clean_query(_dash_parts[1]) or _dash_parts[1].strip()

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
        _client = client or httpx.Client(timeout=HTTP_TIMEOUT_API, headers={"User-Agent": USER_AGENT})

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

        # ── Zero-result fallback: DDG web search ──────────────────────────
        # Both APIs returned nothing.  Hit DuckDuckGo Instant Answers to extract
        # author/ISBN hints (Wikipedia-backed infobox), then retry once.
        if not google_scored and not ol_scored:
            from .ddg import search_book_hints
            hints = search_book_hints(query.clean_title, _client)
            if hints:
                log.info(
                    "metadata.ddg_retry",
                    extra={
                        "title": query.clean_title,
                        "author": hints.get("author"),
                        "isbn": hints.get("isbn"),
                    },
                )
                retry_query = SearchQuery(
                    clean_title=query.clean_title,
                    author_hint=hints.get("author") or query.author_hint,
                    isbn=hints.get("isbn") or query.isbn,
                    year_hint=int(hints["year"]) if hints.get("year") else query.year_hint,
                    series_hint=query.series_hint,
                    series_index_hint=query.series_index_hint,
                )
                try:
                    google_scored = google_books.fetch(
                        retry_query, api_key=config.google_books_api_key, client=_client
                    )
                except RateLimitError:
                    pass
                try:
                    ol_scored = open_library.fetch(retry_query, client=_client)
                except RateLimitError:
                    pass
                # If the retry found candidates, use the enriched query for scoring
                if google_scored or ol_scored:
                    query = retry_query

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
    if fetch_cover and result.best and result.best.candidate.cover_url and not config.mock_mode:
        _client = client or httpx.Client(timeout=HTTP_TIMEOUT_API, headers={"User-Agent": USER_AGENT})
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


# Reject downloads smaller than this — blank 1x1 GIFs and "no cover"
# stubs are well under it; real covers are tens of KB.
_MIN_COVER_BYTES = 1024
# Reject images smaller than this on either axis — real covers from the
# -L endpoints are 300px+; tiny images are tracking pixels / blanks.
_MIN_COVER_PX = 100


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) sniffed from PNG/GIF/JPEG headers, or None if unknown.

    Header-only parsing — no imaging library needed.  Unknown formats
    return None so validation fails open rather than rejecting covers
    in formats this sniffer doesn't speak.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":  # JPEG: scan segments for a SOFn frame header
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no-length markers
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height = int.from_bytes(data[i + 5:i + 7], "big")
                width = int.from_bytes(data[i + 7:i + 9], "big")
                return width, height
            i += 2 + seg_len
    return None


def _download_cover(url: str, client: httpx.Client) -> Path | None:
    """Download and validate a cover image. Returns temp path or None.

    Validation rejects the junk that cover APIs serve with HTTP 200:
    HTML error pages (content-type), blank 1x1 GIFs and "no cover" stubs
    (size floor), and tracking-pixel-sized images (dimension floor).
    OpenLibrary URLs additionally carry ?default=false so a missing cover
    is a 404 instead of an "image not available" placeholder JPEG —
    placeholders are full-size real JPEGs that no content check can
    reliably distinguish from an actual cover.

    The temp file path is registered in ``_pending_cover_paths`` so the atexit /
    SIGTERM handler can clean it up if the process is killed before the caller
    removes the file via ``cover_path.unlink()``.
    """
    import tempfile
    cover_path: Path | None = None
    try:
        response = client.get(url, timeout=HTTP_TIMEOUT_COVER, follow_redirects=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            log.warning(
                "metadata.cover_rejected_not_image",
                extra={"url": url, "content_type": content_type},
            )
            return None
        data = response.content
        if len(data) < _MIN_COVER_BYTES:
            log.warning(
                "metadata.cover_rejected_too_small",
                extra={"url": url, "bytes": len(data)},
            )
            return None
        dims = _image_dimensions(data)
        if dims and (dims[0] < _MIN_COVER_PX or dims[1] < _MIN_COVER_PX):
            log.warning(
                "metadata.cover_rejected_tiny_image",
                extra={"url": url, "width": dims[0], "height": dims[1]},
            )
            return None
        ext = ".jpg" if "jpeg" in content_type or "jpg" in content_type else ".png"
        fd, path_str = tempfile.mkstemp(suffix=ext, prefix="libris_cover_")
        os.close(fd)
        cover_path = Path(path_str)
        _pending_cover_paths.add(path_str)   # track for SIGTERM cleanup
        cover_path.write_bytes(data)
        log.debug("metadata.cover_downloaded", extra={"url": url, "path": path_str})
        return cover_path
    except Exception as exc:
        log.warning("metadata.cover_download_failed", extra={"url": url, "error": str(exc)})
        if cover_path is not None:
            cover_path.unlink(missing_ok=True)
            _pending_cover_paths.discard(str(cover_path))
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

def _extract_year(stem: str) -> int | None:
    """Extract a 4-digit year from the stem (first match in range 1800–2099)."""
    m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", stem)
    return int(m.group(1)) if m else None


def _extract_author_hint(stem: str) -> str | None:
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
        return not any(t.lower() in _NOISE_WORDS for t in tokens)

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


def _extract_series(stem: str) -> tuple[str | None, float | None]:
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
