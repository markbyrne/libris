"""Google Books API metadata source.

Unauthenticated: ~60 requests/minute.
Authenticated (api_key set): 1000 requests/day per project.

API docs: https://developers.google.com/books/docs/v1/using
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .base import BookCandidate, SearchQuery, ScoredCandidate
from .scorer import score_candidate

log = logging.getLogger(__name__)

_BASE_URL = "https://www.googleapis.com/books/v1/volumes"
_TIMEOUT = 10.0


def fetch(
    query: SearchQuery,
    api_key: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> list[ScoredCandidate]:
    """Fetch candidates from Google Books and return scored results.

    Args:
        query: The search query (clean title + optional hints).
        api_key: Optional Google Books API key.
        client: Optional pre-built httpx.Client (injected in tests).

    Returns:
        List of ScoredCandidate, may be empty on error or no results.
    """
    q_string = _build_query_string(query)
    params: dict = {"q": q_string, "maxResults": 5, "printType": "books"}
    if api_key:
        params["key"] = api_key

    log.debug("google_books.fetch", extra={"query": q_string})

    try:
        _client = client or httpx.Client(timeout=_TIMEOUT)
        response = _client.get(_BASE_URL, params=params)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        log.warning("google_books.fetch_failed", extra={"error": str(exc)})
        return []

    candidates = _parse_response(data)
    return [score_candidate(query, c) for c in candidates]


def _build_query_string(query: SearchQuery) -> str:
    parts = [query.clean_title]
    if query.isbn:
        parts.append(f"isbn:{query.isbn}")
    elif query.author_hint:
        parts.append(f"inauthor:{query.author_hint}")
    return " ".join(parts)


def _parse_response(data: dict) -> list[BookCandidate]:
    items = data.get("items") or []
    candidates = []
    for item in items:
        info = item.get("volumeInfo", {})
        title = info.get("title", "")
        if not title:
            continue

        isbn_13 = isbn_10 = None
        for identifier in info.get("industryIdentifiers", []):
            if identifier.get("type") == "ISBN_13":
                isbn_13 = identifier.get("identifier")
            elif identifier.get("type") == "ISBN_10":
                isbn_10 = identifier.get("identifier")

        year_raw = info.get("publishedDate", "")
        year: Optional[int] = None
        if year_raw and len(year_raw) >= 4 and year_raw[:4].isdigit():
            year = int(year_raw[:4])

        candidates.append(BookCandidate(
            title=title,
            authors=info.get("authors") or [],
            isbn_13=isbn_13,
            isbn_10=isbn_10,
            published_year=year,
            publisher=info.get("publisher"),
            source="google_books",
            raw_response=item,
        ))
    return candidates
