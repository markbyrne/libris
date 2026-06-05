"""Confidence scoring and cross-source fusion for book metadata candidates.

Signal weights (sum to 1.0):
  ISBN match    0.40  — decisive when present; rare but near-perfect signal
  Title match   0.30  — rapidfuzz token_sort_ratio; handles subtitle variants
  Author match  0.20  — surname token overlap; partial credit for partial matches
  Year match    0.10  — tiebreaker; many files omit year entirely

Cross-source agreement bonus +0.08 (capped at 1.0): applied when Google Books
and OpenLibrary independently produce best candidates whose titles agree > 0.85
AND share an author surname.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from .base import BookCandidate, MetadataResult, ScoredCandidate, SearchQuery

# ---------------------------------------------------------------------------
# Weight constants
# ---------------------------------------------------------------------------

W_ISBN   = 0.40
W_TITLE  = 0.30
W_AUTHOR = 0.20
W_YEAR   = 0.10

AGREEMENT_BONUS = 0.08
AGREEMENT_TITLE_THRESHOLD = 85.0   # rapidfuzz score (0–100)


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
    breakdown["author"] = _author_score(query.author_hint, candidate.authors) * W_AUTHOR

    # ── Year ──────────────────────────────────────────────────────────────
    breakdown["year"] = _year_score(query.year_hint, candidate.published_year) * W_YEAR

    confidence = min(1.0, sum(breakdown.values()))
    return ScoredCandidate(
        candidate=candidate,
        confidence=confidence,
        score_breakdown=breakdown,
    )


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
