"""Filename noise stripping, chaff detection, and multi-part helpers.

Converts a raw filename like:
  "Project Hail Mary (Unabridged) [MP3 320kbps] Part 1 of 2 - Andy Weir (2021).mp3"
into a clean query string:
  "Project Hail Mary Andy Weir"

The cleaner is intentionally conservative — it removes known noise patterns
without trying to extract structured title/author fields, since filename
conventions vary wildly. The output is passed directly to search APIs as a
free-text query.

Part-detection functions (extract_part, strip_part_marker) are used by the
pipeline to recognise multi-file audiobooks and hold them until all parts
arrive before combining and importing.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Chaff detection
# ---------------------------------------------------------------------------

# Filenames (stem, lowercased) that are definitively not real books
_CHAFF_EXACT: frozenset[str] = frozenset({
    "read me", "readme", "read_me", "readthis",
    "license", "licence", "copying",
    "credits", "credit", "nfo",
    "sample", "preview", "demo",
    "cover", "cover art", "coverart", "folder",
    "info", "description", "about",
})

# Stem prefixes (lowercased) that indicate system/download clutter
_CHAFF_PREFIXES: tuple[str, ...] = (
    "downloaded from",
    "www.",
    "http",
    "[req]",
    "[request]",
    "req -",
)

# Extensions that are never real books even if the classifier would accept them
# (e.g. txt is in EBOOK_EXTENSIONS but almost never a real book when downloaded)
_CHAFF_EXTENSIONS: frozenset[str] = frozenset({
    "txt", "nfo", "url", "htm", "html",
    "jpg", "jpeg", "png", "gif", "bmp", "webp",
    "exe", "zip", "rar", "7z", "torrent",
    "srt", "sub", "ass",  # subtitle files
})

# Stems that are at most this many characters are almost certainly not books
_CHAFF_SHORT_STEM = 2


def is_chaff(filename: str) -> bool:
    """Return True if *filename* looks like clutter rather than a real book.

    Checks extension, stem length, known exact names, and known bad prefixes.
    False positives can be recovered with 'libris recover --id N'.

    Args:
        filename: Basename only (e.g. "Read Me!.epub"), not a full path.
    """
    p = Path(filename)
    ext = p.suffix.lstrip(".").lower()
    stem = p.stem.strip().lower()

    if ext in _CHAFF_EXTENSIONS:
        return True

    # Very short stems: "a.epub", "1.epub" — not real books
    if len(stem) <= _CHAFF_SHORT_STEM:
        return True

    # Normalise: strip trailing and leading punctuation/symbols for exact matching
    # "Read Me!" → "read me", "nfo!" → "nfo"
    stem_clean = re.sub(r"[^a-z0-9\s]+", " ", stem).strip()
    stem_clean = re.sub(r"\s+", " ", stem_clean)

    if stem_clean in _CHAFF_EXACT or stem in _CHAFF_EXACT:
        return True

    # Normalize stem for prefix matching (strip leading punctuation/brackets)
    stem_norm = re.sub(r"^[\[\(!\s]+", "", stem)
    return bool(any(stem_norm.startswith(pfx) for pfx in _CHAFF_PREFIXES))


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

    # Content hashes (md5/sha1/sha256) appended by shadow-library archives
    (re.compile(r"\b[0-9a-fA-F]{32}\b|\b[0-9a-fA-F]{40}\b|\b[0-9a-fA-F]{64}\b"), ""),

    # Strip trailing lowercase single-word suffix after a dash separator
    # e.g. "Book Title - sometag" — lowercase tokens are not proper author names
    (re.compile(r"\s[-–—]\s+[a-z][a-z0-9_-]*$"), ""),

    # Separators that typically divide title from author: " - Author", " by Author"
    # (single dash, double dash, en/em dash)
    # We keep the content but strip the separator token itself
    (re.compile(r"\s(?:-{1,2}|[–—])\s"), " "),
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


_DOUBLE_DASH_SEP = re.compile(r"\s+--\s+")
_HEX_HASH_FIELD = re.compile(r"^[0-9a-fA-F]{16,64}$")
_BARE_YEAR_FIELD = re.compile(r"^(?:19|20)\d{2}$")


def parse_double_dash(stem: str) -> dict | None:
    """Parse the shadow-library ' -- ' field convention into structured parts.

    Archives such as Anna's Archive name files
    ``{title} -- {author} -- {year} -- {publisher} -- {md5}.ext``.
    The generic substitution cleaner mangles these (the publisher and hash
    pollute the search query and the author is never extracted), so the
    resolver should consume the fields directly.

    Returns ``{"title": str, "author": str|None, "year": int|None}``
    when the stem uses the convention (two or more " -- " separators),
    else None.  Trailing hash fields and bare-year fields are recognised
    and consumed; the publisher field (anything after the author) is
    deliberately dropped — it adds noise to title/author search queries.
    """
    fields = [f.strip() for f in _DOUBLE_DASH_SEP.split(stem) if f.strip()]
    if len(fields) < 3:
        return None  # "A -- B" alone is too ambiguous to call structured

    year: int | None = None
    rest: list[str] = []
    for f in fields:
        if _HEX_HASH_FIELD.match(f):
            continue
        if year is None and _BARE_YEAR_FIELD.match(f):
            year = int(f)
            continue
        rest.append(f)

    if not rest:
        return None
    return {
        "title": rest[0],
        "author": rest[1] if len(rest) > 1 else None,
        "year": year,
    }


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


# ---------------------------------------------------------------------------
# Multi-part audiobook helpers
# ---------------------------------------------------------------------------

# Parenthesised patterns: (part 1 of 3)  (part 1/3)  (disc 2 of 2)
_PART_PAREN_OF = re.compile(
    r'\(\s*(?:part|disc|disk|cd)\s*(\d+)\s*(?:of|/)\s*(\d+)\s*\)',
    re.IGNORECASE,
)
# Dot notation inside parens: (part 1.3) → part 1 of 3
_PART_PAREN_DOT = re.compile(
    r'\(\s*(?:part|disc|disk|cd)\s*(\d+)\.(\d+)\s*\)',
    re.IGNORECASE,
)
# Bare (no parens) with total: "Part 1 of 3"  "Disc 1 of 2"
_PART_BARE_OF = re.compile(
    r'\b(?:part|disc|disk|cd)\s+(\d+)\s+of\s+(\d+)\b',
    re.IGNORECASE,
)
# Lone part without total: "(Part 1)"  "Part 2"
_PART_LONE_PAREN = re.compile(
    r'\(\s*(?:part|disc|disk|cd)\s*(\d+)\s*\)',
    re.IGNORECASE,
)
_PART_LONE_BARE = re.compile(
    r'\b(?:part|disc|disk|cd)\s+(\d+)\b',
    re.IGNORECASE,
)
# Bare "N of M" or "N/M" in parens with no keyword: "(1 of 3)", "(1/3)"
_PART_PAREN_NUM_OF = re.compile(
    r'\((\d+)\s*(?:of|/)\s*(\d+)\)',
)
# Bare trailing number in parens with no keyword: "(1)", "(2)"
# Capped at ≤3 digits and end-anchored to avoid matching years mid-stem
_PART_PAREN_BARE = re.compile(
    r'\((\d{1,3})\)\s*$',
)
# Compact disc/cd notation without space: "Disc01", "Disc01-001" (disc+track),
# "CD03".  Only the disc number is used as the part number; a trailing -NNN
# track counter is consumed but ignored.
_PART_COMPACT_DISC = re.compile(
    r'\b(?:disc|disk|cd)(\d+)(?:-\d+)?\b',
    re.IGNORECASE,
)
# Trailing "-NN-NN" pair with no keyword: "Title-01-46" → part 1 of 46.
# The (?<![\d-]) lookbehind rejects trailing dates ("Show-2024-12-25" would
# otherwise match "-12-25").  Matches must also pass _valid_trailing_pair —
# the regex alone cannot enforce part <= total.
_PART_TRAILING_PAIR = re.compile(
    r'(?<![\d-])-(\d{1,3})-(\d{1,3})\s*$',
)


def _valid_trailing_pair(part: int, total: int) -> bool:
    """A bare trailing pair only counts as a part marker when it is plausible.

    Requires 1 <= part <= total and total >= 2: "Title-46-01" is more likely
    a series/volume code than part 46 of 1, and "Title-1-1" more likely series
    notation than a one-part set (which imports fine as a standalone file).
    """
    return 1 <= part <= total and total >= 2

# Strips all of the above from a filename stem
_PART_STRIP_PATTERNS = (
    _PART_PAREN_OF,
    _PART_PAREN_DOT,
    _PART_BARE_OF,
    _PART_LONE_PAREN,
    _PART_LONE_BARE,
    _PART_PAREN_NUM_OF,
    _PART_PAREN_BARE,
    _PART_COMPACT_DISC,
)


def extract_part(raw: str) -> tuple[int | None, int | None]:
    """Extract (part_num, total_parts) from a filename stem.

    Returns (None, None) if no part pattern is found.
    total_parts is None when the filename only specifies the part number
    without the total (e.g. "Part 1" with no "of N").

    Examples:
      "Brisingr (part 1 of 3)"   → (1, 3)
      "Brisingr (part 1.3)"      → (1, 3)
      "Brisingr (part 1/3)"      → (1, 3)
      "Brisingr Disc 1 of 2"     → (1, 2)
      "Brisingr Part 1"          → (1, None)
      "Eragon"                   → (None, None)
    """
    # Priority order: patterns with totals first (most informative)
    for pat in (_PART_PAREN_OF, _PART_PAREN_DOT, _PART_BARE_OF, _PART_PAREN_NUM_OF):
        m = pat.search(raw)
        if m:
            try:
                return int(m.group(1)), int(m.group(2))
            except (ValueError, IndexError):
                pass

    # Bare trailing "-NN-NN" pair (also carries a total, but needs the
    # plausibility check, so it sits below the explicit keyword patterns)
    m = _PART_TRAILING_PAIR.search(raw)
    if m:
        part, total = int(m.group(1)), int(m.group(2))
        if _valid_trailing_pair(part, total):
            return part, total

    # Lone part number (no total known)
    for pat in (_PART_LONE_PAREN, _PART_LONE_BARE, _PART_PAREN_BARE, _PART_COMPACT_DISC):
        m = pat.search(raw)
        if m:
            try:
                return int(m.group(1)), None
            except (ValueError, IndexError):
                pass

    return None, None


def strip_part_marker(raw: str) -> str:
    """Remove part/disc/cd markers from a filename stem.

    Used to build stable group keys and output filenames for combined files.

    Examples:
      "Brisingr (part 1 of 3)"        → "Brisingr"
      "Inheritance Cycle 3 - Brisingr (part 2 of 3)" → "Inheritance Cycle 3 - Brisingr"
      "Name of the Wind Disc 1 of 2"  → "Name of the Wind"
      "Eragon"                         → "Eragon"
    """
    result = raw
    # Trailing pair is stripped only when it passes the same plausibility
    # check extract_part applies — an implausible pair ("Title-46-01") is
    # part of the title and must survive as the group key.
    m = _PART_TRAILING_PAIR.search(result)
    if m and _valid_trailing_pair(int(m.group(1)), int(m.group(2))):
        result = result[:m.start()]
    for pat in _PART_STRIP_PATTERNS:
        result = pat.sub("", result)
    # Collapse extra spaces and trailing/leading punctuation
    result = re.sub(r'\s{2,}', ' ', result).strip(" -–—")
    return result
