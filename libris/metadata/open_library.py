"""OpenLibrary API metadata source.

No API key required. Rate limit: ~100 requests per 10 seconds.

API docs: https://openlibrary.org/developers/api
Search: https://openlibrary.org/search.json?title=...&author=...&limit=5
"""

from __future__ import annotations

import logging

import httpx

from ..exceptions import RateLimitError
from .base import BookCandidate, ScoredCandidate, SearchQuery
from .scorer import score_candidate

log = logging.getLogger(__name__)

_BASE_URL = "https://openlibrary.org/search.json"
_TIMEOUT = 10.0


def fetch(
    query: SearchQuery,
    client: httpx.Client | None = None,
) -> list[ScoredCandidate]:
    """Fetch candidates from OpenLibrary and return scored results.

    Args:
        query: The search query (clean title + optional hints).
        client: Optional pre-built httpx.Client (injected in tests).

    Returns:
        List of ScoredCandidate, may be empty on error or no results.
    """
    params = _build_params(query)
    log.debug("open_library.fetch", extra={"params": params})

    try:
        _client = client or httpx.Client(timeout=_TIMEOUT)
        response = _client.get(_BASE_URL, params=params)
        if response.status_code == 429:
            raise RateLimitError(
                source="open_library",
                retry_after=_parse_retry_after(response),
            )
        response.raise_for_status()
        data = response.json()
    except RateLimitError:
        raise  # propagate to CLI so user can choose wait / skip
    except Exception as exc:
        log.warning("open_library.fetch_failed", extra={"error": str(exc)})
        return []

    candidates = _parse_response(data)
    return [score_candidate(query, c) for c in candidates]


def _parse_retry_after(response: httpx.Response) -> int | None:
    """Return the Retry-After header value in seconds, or None if absent/unparseable."""
    header = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if header:
        try:
            return int(header)
        except ValueError:
            pass
    return None


def _build_params(query: SearchQuery) -> dict:
    params: dict = {"limit": 5, "fields": "title,author_name,isbn,first_publish_year,publisher,language,subject,cover_i"}
    if query.isbn:
        params["isbn"] = query.isbn
    else:
        params["title"] = query.clean_title
        if query.author_hint:
            params["author"] = query.author_hint
    return params


def _parse_response(data: dict) -> list[BookCandidate]:
    docs = data.get("docs") or []
    candidates = []
    for doc in docs:
        title = doc.get("title", "")
        if not title:
            continue

        authors = doc.get("author_name") or []

        # OpenLibrary returns a flat list of ISBNs (mix of 10 and 13 digit)
        isbns = doc.get("isbn") or []
        isbn_13 = next((i for i in isbns if len(i) == 13 and i.startswith(("978", "979"))), None)
        isbn_10 = next((i for i in isbns if len(i) == 10), None)

        year_raw = doc.get("first_publish_year")
        year: int | None = int(year_raw) if year_raw else None

        publishers = doc.get("publisher") or []
        publisher = publishers[0] if publishers else None

        # Cover image via OpenLibrary cover API
        cover_i = doc.get("cover_i")
        cover_url = f"https://covers.openlibrary.org/b/id/{cover_i}-L.jpg" if cover_i else None
        # Fallback: use ISBN if available
        if not cover_url and isbn_13:
            cover_url = f"https://covers.openlibrary.org/b/isbn/{isbn_13}-L.jpg"

        # Language: OpenLibrary returns codes like ["/languages/eng"]
        languages = doc.get("language") or []
        language = languages[0].split("/")[-1] if languages else None

        subjects = doc.get("subject") or []

        candidates.append(BookCandidate(
            title=title,
            authors=authors,
            isbn_13=isbn_13,
            isbn_10=isbn_10,
            published_year=year,
            publisher=publisher,
            language=language,
            categories=subjects[:5],  # cap at 5 subjects
            cover_url=cover_url,
            source="open_library",
            raw_response=doc,
        ))
    return candidates
