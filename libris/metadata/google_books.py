"""Google Books API metadata source.

Unauthenticated: ~60 requests/minute.
Authenticated (api_key set): 1000 requests/day per project.

API docs: https://developers.google.com/books/docs/v1/using
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..exceptions import RateLimitError
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
        rl_reason = _rate_limit_reason(response)
        if rl_reason is not None:
            raise RateLimitError(
                source="google_books",
                retry_after=_parse_retry_after(response),
                reason=rl_reason,
            )
        response.raise_for_status()
        data = response.json()
    except RateLimitError:
        raise  # propagate to CLI so user can choose wait / add key / skip
    except Exception as exc:
        log.warning("google_books.fetch_failed", extra={"error": str(exc)})
        return []

    candidates = _parse_response(data)
    return [score_candidate(query, c) for c in candidates]


def _rate_limit_reason(response: httpx.Response) -> Optional[str]:
    """Return the rate-limit reason string if the response indicates throttling, else None.

    Google Books uses both HTTP 429 and HTTP 403 for quota errors.  The reason
    is in the JSON body under error.errors[].reason.
    """
    if response.status_code == 429:
        # Try to extract reason from body; fall back to generic string
        try:
            errors = response.json().get("error", {}).get("errors", [])
            for e in errors:
                if e.get("reason"):
                    return e["reason"]
        except Exception:
            pass
        return "rateLimitExceeded"

    if response.status_code == 403:
        try:
            errors = response.json().get("error", {}).get("errors", [])
            for e in errors:
                if e.get("reason") in (
                    "rateLimitExceeded",
                    "userRateLimitExceeded",
                    "dailyLimitExceeded",
                ):
                    return e["reason"]
        except Exception:
            pass

    return None


def _parse_retry_after(response: httpx.Response) -> Optional[int]:
    """Return the Retry-After header value in seconds, or None if absent/unparseable."""
    header = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if header:
        try:
            return int(header)
        except ValueError:
            pass
    return None


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

        # Cover image — prefer the largest available
        image_links = info.get("imageLinks", {})
        cover_url = (
            image_links.get("large")
            or image_links.get("medium")
            or image_links.get("thumbnail")
            or image_links.get("smallThumbnail")
        )
        # Force HTTPS and request larger size
        if cover_url:
            cover_url = cover_url.replace("http://", "https://")
            if "zoom=" in cover_url:
                cover_url = cover_url.replace("zoom=1", "zoom=3")

        candidates.append(BookCandidate(
            title=title,
            authors=info.get("authors") or [],
            isbn_13=isbn_13,
            isbn_10=isbn_10,
            published_year=year,
            publisher=info.get("publisher"),
            description=info.get("description"),
            language=info.get("language"),
            categories=info.get("categories") or [],
            cover_url=cover_url,
            source="google_books",
            raw_response=item,
        ))
    return candidates
