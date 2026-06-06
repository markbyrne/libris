"""DuckDuckGo Instant Answer API — fallback for zero-result metadata queries.

When Google Books and OpenLibrary both return nothing, DDG's Instant Answer
endpoint (backed by Wikipedia/WikiData) can often return a structured infobox
with the author name, ISBN, and publication year.  We use this to build an
enriched query and silently retry the primary sources.

No API key required.  Only called when both primary sources return 0 results,
so quota is not a concern.

API docs: https://duckduckgo.com/api
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://api.duckduckgo.com/"
_TIMEOUT = 8.0

# "written by John Smith" / "novel by Jane Doe" etc.
_ABSTRACT_AUTHOR_RE = re.compile(
    r'\b(?:by|written by|authored by)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})',
)


def search_book_hints(
    title: str,
    client: Optional[httpx.Client] = None,
) -> dict[str, str]:
    """Query DDG Instant Answers for author/ISBN/year hints for a book title.

    Returns a dict containing any subset of keys:
      "author"  — e.g. "Christopher Paolini"
      "isbn"    — digits only, e.g. "9780375826702"
      "year"    — four-digit string, e.g. "2003"

    Returns an empty dict on failure or when no relevant data is found.
    """
    hints: dict[str, str] = {}
    params = {
        "q": f"{title} book",
        "format": "json",
        "no_redirect": "1",
        "no_html": "1",
        "skip_disambig": "1",
    }

    try:
        _client = client or httpx.Client(timeout=_TIMEOUT)
        response = _client.get(_BASE_URL, params=params)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        log.debug("ddg.fetch_failed", extra={"title": title, "error": str(exc)})
        return hints

    # ── Structured infobox (most reliable) ───────────────────────────────
    infobox = data.get("Infobox") or {}
    for entry in infobox.get("content") or []:
        label = (entry.get("label") or "").lower().strip()
        value = (entry.get("value") or "").strip()
        if not value:
            continue
        if label in ("author", "authors") and "author" not in hints:
            hints["author"] = value
        elif label in ("isbn", "isbn-13", "isbn-10") and "isbn" not in hints:
            hints["isbn"] = re.sub(r"[\s\-–]", "", value)
        elif label in ("published", "publication date", "first published", "first edition") \
                and "year" not in hints:
            m = re.search(r"\b(1[89]\d{2}|20\d{2})\b", value)
            if m:
                hints["year"] = m.group(1)

    # ── Abstract text fallback ────────────────────────────────────────────
    if "author" not in hints:
        abstract = data.get("Abstract", "")
        m = _ABSTRACT_AUTHOR_RE.search(abstract)
        if m:
            hints["author"] = m.group(1)

    if hints:
        log.info("ddg.hints_found", extra={"title": title, "hints": hints})
    else:
        log.debug("ddg.no_hints", extra={"title": title})

    return hints
