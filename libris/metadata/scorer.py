"""Confidence scoring and cross-source fusion for book metadata candidates.

Signal weights (sum to 1.0):
  ISBN match    0.40  — decisive when present; rare but near-perfect signal
  Title match   0.30  — rapidfuzz token_sort_ratio; handles subtitle variants
  Author match  0.20  — surname token overlap; partial credit for partial matches
  Year match    0.10  — tiebreaker; many files omit year entirely

Cross-source agreement bonus +0.12 (capped at 1.0): applied when Google Books
and OpenLibrary independently produce best candidates whose titles agree > 0.85
AND share an author surname.

Strong-match floor: without an ISBN in the filename, the maximum achievable
base score is only 0.60 (title+author+year perfect) or 0.68 with the
agreement bonus — below the default 0.75 threshold.  When title AND author
both score clearly correct we apply a minimum confidence floor so the file
is not needlessly sent to review:

  Strong (title ≥ 90% + exact surname):   floor 0.82
  Good   (title ≥ 85% + author present):  floor 0.76

Floors only raise confidence, never lower it, and are recorded in
score_breakdown["strong_match_floor"] for transparency in the rematch UI.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .base import BookCandidate, MetadataResult, ScoredCandidate, SearchQuery

# ---------------------------------------------------------------------------
# Weight constants
# ---------------------------------------------------------------------------

W_ISBN   = 0.40
W_TITLE  = 0.30
W_AUTHOR = 0.20
W_YEAR   = 0.10

AGREEMENT_BONUS = 0.12            # raised from 0.08 — two APIs agreeing is strong
AGREEMENT_TITLE_THRESHOLD = 85.0  # rapidfuzz score (0–100)

# ── Strong-match floor ────────────────────────────────────────────────────
# Without ISBN, max base score is 0.60 (perfect title+author+year).
# These floors guarantee a passing confidence when title+author are both clear.
STRONG_TITLE_THRESHOLD = 90   # raw token_sort_ratio; ~2 chars off in a 20-char title
GOOD_TITLE_THRESHOLD   = 85

STRONG_MATCH_FLOOR = 0.82     # title ≥ 90 AND exact author surname match
GOOD_MATCH_FLOOR   = 0.76     # title ≥ 85 AND author token present


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_candidate(query: SearchQuery, candidate: BookCandidate) -> ScoredCandidate:
    """Compute a confidence score for a single candidate against the search query."""
    breakdown: dict[str, float] = {}

    # ── ISBN ──────────────────────────────────────────────────────────────
    breakdown["isbn"] = _isbn_score(query.isbn, candidate) * W_ISBN

    # ── Title ─────────────────────────────────────────────────────────────
    raw_title_score = fuzz.token_sort_ratio(
        query.clean_title.lower(), candidate.title.lower()
    )
    breakdown["title"] = (raw_title_score / 100.0) * W_TITLE

    # ── Author ────────────────────────────────────────────────────────────
    raw_author_score = _author_score(query.author_hint, candidate.authors)
    breakdown["author"] = raw_author_score * W_AUTHOR

    # ── Year ──────────────────────────────────────────────────────────────
    breakdown["year"] = _year_score(query.year_hint, candidate.published_year) * W_YEAR

    confidence = min(1.0, sum(breakdown.values()))

    # ── Strong-match floor ────────────────────────────────────────────────
    # Without ISBN the base score caps at 0.60–0.68, below the default 0.75
    # threshold.  When both title and author signal clearly correct, apply a
    # minimum floor rather than sending an obvious match to review.
    # Floors only raise — they never reduce an already-high score.
    if raw_title_score >= STRONG_TITLE_THRESHOLD and raw_author_score >= 1.0:
        floor = STRONG_MATCH_FLOOR
    elif raw_title_score >= GOOD_TITLE_THRESHOLD and raw_author_score >= 0.7:
        floor = GOOD_MATCH_FLOOR
    else:
        floor = 0.0

    if floor and confidence < floor:
        breakdown["strong_match_floor"] = round(floor - confidence, 4)
        confidence = floor

    return ScoredCandidate(
        candidate=candidate,
        confidence=confidence,
        score_breakdown=breakdown,
    )


def dedup_candidates(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Remove duplicate editions of the same book.

    Groups by normalised (title, author surnames) and keeps the highest-scored
    candidate per group.  Different editions of the same book (same title and
    author, different publisher/year/ISBN) are treated as duplicates — the user
    only needs to see the book once.

    Input order within each group is already score-descending (callers sort
    before deduping), so the first entry seen per key is the winner.
    """
    def _key(sc: ScoredCandidate) -> tuple:
        title = re.sub(r"[^\w\s]", "", sc.candidate.title.lower()).strip()
        surnames = tuple(sorted(sc.candidate.author_surnames))
        return (title, surnames)

    seen: dict[tuple, ScoredCandidate] = {}
    for sc in candidates:
        k = _key(sc)
        if k not in seen or sc.confidence > seen[k].confidence:
            seen[k] = sc
    return sorted(seen.values(), key=lambda sc: sc.confidence, reverse=True)


def pick_best(candidates: list[ScoredCandidate]) -> ScoredCandidate | None:
    """Return the highest-confidence candidate, or None if the list is empty."""
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.confidence)


def fuse_sources(
    google_candidates: list[ScoredCandidate],
    ol_candidates: list[ScoredCandidate],
    threshold: float,
) -> MetadataResult:
    """Combine scored candidates from both sources into a final MetadataResult.

    If both sources independently agree on a best match, apply the agreement
    bonus before computing above_threshold.
    """
    # Placeholder query; caller replaces with real query
    _dummy_query = SearchQuery(clean_title="")

    google_best = pick_best(google_candidates)
    ol_best = pick_best(ol_candidates)

    all_candidates = google_candidates + ol_candidates

    winner = _apply_agreement_bonus(google_best, ol_best)

    return MetadataResult(
        query=_dummy_query,          # caller sets this
        best=winner,
        all_candidates=sorted(all_candidates, key=lambda c: c.confidence, reverse=True),
        above_threshold=winner is not None and winner.confidence >= threshold,
    )


def build_result(
    query: SearchQuery,
    google_candidates: list[ScoredCandidate],
    ol_candidates: list[ScoredCandidate],
    threshold: float,
) -> MetadataResult:
    """Score all candidates and return a MetadataResult with the query attached."""
    result = fuse_sources(google_candidates, ol_candidates, threshold)
    result.query = query
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _isbn_score(query_isbn: str | None, candidate: BookCandidate) -> float:
    """1.0 if ISBNs match exactly (digits only), else 0.0."""
    if not query_isbn:
        return 0.0
    q = _digits(query_isbn)
    for isbn in filter(None, [candidate.isbn_13, candidate.isbn_10]):
        if _digits(isbn) == q:
            return 1.0
    return 0.0


def _author_score(author_hint: str | None, candidate_authors: list[str]) -> float:
    """Score author match using token overlap of surnames (0.0–1.0).

    Scoring:
    - No hint provided → 0.5 (neutral; don't penalise for missing info)
    - Any candidate surname found in hint → 1.0
    - Any hint token found in any candidate author string → 0.7
    - No overlap → 0.0
    """
    if not author_hint:
        return 0.5

    hint_lower = author_hint.lower()
    hint_tokens = set(hint_lower.split())

    candidate_surnames = {a.split()[-1].lower() for a in candidate_authors if a.strip()}
    candidate_tokens = {
        tok.lower()
        for a in candidate_authors
        for tok in a.split()
        if len(tok) > 2
    }

    if candidate_surnames & hint_tokens:
        return 1.0
    if candidate_tokens & hint_tokens:
        return 0.7
    # Fuzzy fallback: best partial match across all candidate author strings
    best_fuzz = max(
        (fuzz.partial_ratio(hint_lower, a.lower()) for a in candidate_authors),
        default=0,
    )
    if best_fuzz >= 80:
        return 0.5
    return 0.0


def _year_score(query_year: int | None, candidate_year: int | None) -> float:
    """Score year proximity (0.0–1.0)."""
    if query_year is None or candidate_year is None:
        return 0.5   # neutral — don't penalise missing year info
    delta = abs(query_year - candidate_year)
    if delta <= 1:
        return 1.0
    if delta <= 3:
        return 0.5
    return 0.0


def _apply_agreement_bonus(
    google_best: ScoredCandidate | None,
    ol_best: ScoredCandidate | None,
) -> ScoredCandidate | None:
    """If both sources agree, apply +AGREEMENT_BONUS to the higher-scored candidate."""
    if google_best is None and ol_best is None:
        return None
    if google_best is None:
        return ol_best
    if ol_best is None:
        return google_best

    titles_agree = fuzz.token_sort_ratio(
        google_best.candidate.title.lower(),
        ol_best.candidate.title.lower(),
    ) >= AGREEMENT_TITLE_THRESHOLD

    g_surnames = set(google_best.candidate.author_surnames)
    o_surnames = set(ol_best.candidate.author_surnames)
    authors_agree = bool(g_surnames & o_surnames)

    winner = google_best if google_best.confidence >= ol_best.confidence else ol_best

    if titles_agree and authors_agree:
        boosted = min(1.0, winner.confidence + AGREEMENT_BONUS)
        winner.score_breakdown["agreement_bonus"] = AGREEMENT_BONUS
        winner.confidence = boosted

    return winner


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())
