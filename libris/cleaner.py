"""Filename noise stripping for metadata query generation.

Converts a raw filename like:
  "Project Hail Mary (Unabridged) [MP3 320kbps] Part 1 of 2 - Andy Weir (2021).mp3"
into a clean query string:
  "Project Hail Mary Andy Weir"

The cleaner is intentionally conservative — it removes known noise patterns
without trying to extract structured title/author fields, since filename
conventions vary wildly. The output is passed directly to search APIs as a
free-text query.
"""

from __future__ import annotations

import re


# Ordered list of (pattern, replacement) substitutions applied sequentially.
_SUBSTITUTIONS: list[tuple[re.Pattern, str]] = [
    # Strip file extension
    (re.compile(r"\.[a-zA-Z0-9]{2,5}$"), ""),

    # Part/chapter markers: "Part 1 of 3", "(Part 2)", "Pt. 1", "Disc 1", "CD 2"
    (re.compile(r"\bPart\s*\d+\s*(?:of\s*\d+)?\b", re.IGNORECASE), ""),
    (re.compile(r"\bPt\.?\s*\d+\b", re.IGNORECASE), ""),
    (re.compile(r"\b(?:Disc|CD|Vol\.?|Volume)\s*\d+\b", re.IGNORECASE), ""),

    # Format/quality tags: [MP3 320kbps], (EPUB), {RETAIL}, etc.
    (re.compile(r"[\[\({][^\]\)}{]*[\]\)}{]"), ""),

    # Known format/quality keywords standing alone
    (re.compile(
        r"\b(?:EPUB|PDF|MOBI|AZW3?|MP3|M4B|M4A|FLAC|AAC|OGG|WAV|OPUS"
        r"|320kbps|128kbps|256kbps|HQ|Retail|Unabridged|Abridged"
        r"|Audiobook|eBook|Digital)\b",
        re.IGNORECASE,
    ), ""),

    # Year in parens or standalone: (2021), [2021], 2021
    (re.compile(r"\b(?:19|20)\d{2}\b"), ""),

    # Strip trailing lowercase single-word suffix after a dash separator
    # e.g. "Book Title - sometag" — lowercase tokens are not proper author names
    (re.compile(r"\s[-–—]\s+[a-z][a-z0-9_-]*$"), ""),

    # Separators that typically divide title from author: " - Author", " by Author"
    # We keep the content but strip the separator token itself
    (re.compile(r"\s[-–—]\s"), " "),
    (re.compile(r"\bby\b", re.IGNORECASE), ""),

    # Underscores → spaces
    (re.compile(r"_"), " "),

    # Collapse multiple whitespace
    (re.compile(r"\s{2,}"), " "),
]


def clean_query(raw: str) -> str:
    """Strip noise from a filename and return a search-friendly query string.

    Args:
        raw: Raw filename (with or without extension).

    Returns:
        Cleaned string suitable for passing to a book metadata API.
        May be empty if the filename was entirely noise.
    """
    result = raw
    for pattern, replacement in _SUBSTITUTIONS:
        result = pattern.sub(replacement, result)
    return result.strip()


def extract_isbn(raw: str) -> str | None:
    """Attempt to extract an ISBN-13 or ISBN-10 from a filename.

    Returns the first match found (digits only, no hyphens), or None.
    Handles both hyphenated (978-0-593-13520-4) and plain (9780593135204) forms.
    """
    # Normalise: collapse hyphen/space runs so we can match digit-only blocks
    # Try ISBN-13 first (higher specificity)
    # Pattern: 978/979 prefix, then exactly 10 more digits, with optional hyphens/spaces
    m = re.search(r"\b(97[89][\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d)\b", raw)
    if m:
        digits = re.sub(r"[^\d]", "", m.group(1))
        if len(digits) == 13:
            return digits

    # ISBN-10: 9 digits followed by a digit or X, with optional hyphens
    m = re.search(r"\b(\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?\d[\s\-]?[\dXx])\b", raw)
    if m:
        digits = re.sub(r"[^\dXx]", "", m.group(1).upper())
        if len(digits) == 10 and not digits.startswith(("978", "979")):
            return digits

    return None
