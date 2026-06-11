"""Tests for libris.metadata.scorer — confidence scoring logic."""

import pytest

from libris.metadata.base import BookCandidate, SearchQuery
from libris.metadata.scorer import (
    AGREEMENT_BONUS,
    GOOD_MATCH_FLOOR,
    STRONG_MATCH_FLOOR,
    _apply_agreement_bonus,
    pick_best,
    score_candidate,
)
from tests.conftest import DUNE, PROJECT_HAIL_MARY, make_scored

# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------

class TestScoreCandidate:
    def test_exact_isbn_match_gives_high_score(self):
        query = SearchQuery(
            clean_title="Project Hail Mary",
            isbn="9780593135204",
        )
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        # ISBN contributes 0.40; title is exact = 0.30; author neutral 0.5*0.20 = 0.10
        assert scored.score_breakdown["isbn"] == pytest.approx(0.40)
        assert scored.confidence >= 0.70

    def test_exact_title_match_contributes_correctly(self):
        query = SearchQuery(clean_title="Project Hail Mary")
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        # Title is exact → 1.0 * 0.30 = 0.30
        assert scored.score_breakdown["title"] == pytest.approx(0.30, abs=0.01)

    def test_partial_title_match_lower_score(self):
        query = SearchQuery(clean_title="Hail Mary Project")   # scrambled
        score_candidate(SearchQuery(clean_title="Project Hail Mary"), PROJECT_HAIL_MARY)
        partial = score_candidate(query, PROJECT_HAIL_MARY)
        # token_sort_ratio handles word order → should still be high
        assert partial.score_breakdown["title"] >= 0.25

    def test_author_hint_matching_surname(self):
        query = SearchQuery(clean_title="Project Hail Mary", author_hint="Weir")
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        assert scored.score_breakdown["author"] == pytest.approx(0.20)  # 1.0 * 0.20

    def test_author_hint_no_match_zero(self):
        query = SearchQuery(clean_title="Project Hail Mary", author_hint="Tolkien")
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        assert scored.score_breakdown["author"] == 0.0

    def test_no_author_hint_neutral(self):
        query = SearchQuery(clean_title="Project Hail Mary")
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        # 0.5 * 0.20 = 0.10
        assert scored.score_breakdown["author"] == pytest.approx(0.10)

    def test_year_exact_match(self):
        query = SearchQuery(clean_title="Project Hail Mary", year_hint=2021)
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        assert scored.score_breakdown["year"] == pytest.approx(0.10)

    def test_year_off_by_one(self):
        query = SearchQuery(clean_title="Project Hail Mary", year_hint=2022)
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        assert scored.score_breakdown["year"] == pytest.approx(0.10)

    def test_year_off_by_two(self):
        query = SearchQuery(clean_title="Project Hail Mary", year_hint=2023)
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        assert scored.score_breakdown["year"] == pytest.approx(0.05)  # 0.5 * 0.10

    def test_year_far_off_zero(self):
        query = SearchQuery(clean_title="Project Hail Mary", year_hint=1990)
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        assert scored.score_breakdown["year"] == 0.0

    def test_wrong_book_low_score(self):
        # Search for Eragon, get Dune result
        query = SearchQuery(clean_title="Eragon", author_hint="Paolini")
        scored = score_candidate(query, DUNE)
        assert scored.confidence < 0.50

    def test_confidence_capped_at_1(self):
        query = SearchQuery(
            clean_title="Project Hail Mary",
            author_hint="Andy Weir",
            isbn="9780593135204",
            year_hint=2021,
        )
        scored = score_candidate(query, PROJECT_HAIL_MARY)
        assert scored.confidence <= 1.0


# ---------------------------------------------------------------------------
# pick_best
# ---------------------------------------------------------------------------

class TestPickBest:
    def test_empty_list_returns_none(self):
        assert pick_best([]) is None

    def test_single_candidate(self):
        s = make_scored(PROJECT_HAIL_MARY, "Project Hail Mary")
        assert pick_best([s]) is s

    def test_picks_highest_confidence(self):
        high = make_scored(PROJECT_HAIL_MARY, "Project Hail Mary", isbn="9780593135204")
        low = make_scored(DUNE, "Project Hail Mary")
        assert pick_best([low, high]) is high


# ---------------------------------------------------------------------------
# Agreement bonus
# ---------------------------------------------------------------------------

class TestAgreementBonus:
    def test_both_none_returns_none(self):
        assert _apply_agreement_bonus(None, None) is None

    def test_one_none_returns_other(self):
        s = make_scored(PROJECT_HAIL_MARY, "Project Hail Mary")
        assert _apply_agreement_bonus(s, None) is s
        assert _apply_agreement_bonus(None, s) is s

    def test_agreeing_sources_get_bonus(self):
        # Same book from two sources should agree
        google_c = BookCandidate(
            title="Project Hail Mary",
            authors=["Andy Weir"],
            source="google_books",
        )
        ol_c = BookCandidate(
            title="Project Hail Mary",
            authors=["Andy Weir"],
            source="open_library",
        )
        query = SearchQuery(clean_title="Project Hail Mary")
        g_scored = score_candidate(query, google_c)
        o_scored = score_candidate(query, ol_c)

        before = max(g_scored.confidence, o_scored.confidence)
        winner = _apply_agreement_bonus(g_scored, o_scored)
        assert winner is not None
        assert winner.confidence >= before
        assert "agreement_bonus" in winner.score_breakdown

    def test_disagreeing_sources_no_bonus(self):
        # Totally different books from two sources
        query = SearchQuery(clean_title="Project Hail Mary")
        g_scored = score_candidate(query, PROJECT_HAIL_MARY)
        o_scored = score_candidate(query, DUNE)

        max(g_scored.confidence, o_scored.confidence)
        winner = _apply_agreement_bonus(g_scored, o_scored)
        assert winner is not None
        # Winner should be Project Hail Mary (higher score); no bonus
        assert "agreement_bonus" not in winner.score_breakdown or \
               winner.score_breakdown.get("agreement_bonus", 0) == 0

    def test_agreement_bonus_value(self):
        """Bonus applied should equal the AGREEMENT_BONUS constant (currently 0.12)."""
        google_c = BookCandidate(
            title="Blood River",
            authors=["Tim Butcher"],
            source="google_books",
        )
        ol_c = BookCandidate(
            title="Blood River",
            authors=["Tim Butcher"],
            source="open_library",
        )
        query = SearchQuery(clean_title="Blood River", author_hint="Tim Butcher")
        g_scored = score_candidate(query, google_c)
        o_scored = score_candidate(query, ol_c)

        winner = _apply_agreement_bonus(g_scored, o_scored)
        assert winner is not None
        assert "agreement_bonus" in winner.score_breakdown
        assert winner.score_breakdown["agreement_bonus"] == pytest.approx(AGREEMENT_BONUS)


# ---------------------------------------------------------------------------
# Strong-match floor
# ---------------------------------------------------------------------------

class TestStrongMatchFloor:
    """Floors guarantee passing confidence when title+author are clearly correct.

    Without ISBN the base score caps at 0.60; the floor mechanism prevents
    obviously correct matches from being sent to review.
    """

    # ── Common candidates ────────────────────────────────────────────────────
    _BLOOD_RIVER = BookCandidate(
        title="Blood River",
        authors=["Tim Butcher"],
        published_year=2007,
        source="google_books",
    )
    _ROTHFUSS = BookCandidate(
        title="The Name of the Wind",
        authors=["Patrick Rothfuss"],
        published_year=2007,
        source="google_books",
    )

    def test_strong_floor_applied_exact_title_and_surname(self):
        """Title 100% + exact surname → STRONG_MATCH_FLOOR (0.82)."""
        query = SearchQuery(clean_title="Blood River", author_hint="Tim Butcher")
        scored = score_candidate(query, self._BLOOD_RIVER)

        assert scored.confidence >= STRONG_MATCH_FLOOR
        assert "strong_match_floor" in scored.score_breakdown

    def test_strong_floor_confidence_is_exactly_floor_when_base_below(self):
        """When base is below the floor, confidence is pinned to STRONG_MATCH_FLOOR."""
        query = SearchQuery(clean_title="Blood River", author_hint="Tim Butcher")
        scored = score_candidate(query, self._BLOOD_RIVER)
        # Base (no ISBN, no year hint): 0.30 + 0.20 + 0.05 = 0.55 → floor lifts to 0.82
        assert scored.confidence == pytest.approx(STRONG_MATCH_FLOOR)

    def test_good_floor_applied_token_author_match(self):
        """Title 100% + author token (first-name only) → GOOD_MATCH_FLOOR (0.76)."""
        # author_hint="Patrick" → matches token in "Patrick Rothfuss" but NOT surname
        query = SearchQuery(clean_title="The Name of the Wind", author_hint="Patrick")
        scored = score_candidate(query, self._ROTHFUSS)

        assert scored.confidence >= GOOD_MATCH_FLOOR
        assert "strong_match_floor" in scored.score_breakdown

    def test_floor_not_applied_without_author_hint(self):
        """No author hint → neutral author score (0.5) → floor threshold not met."""
        query = SearchQuery(clean_title="Blood River")   # no author_hint
        scored = score_candidate(query, self._BLOOD_RIVER)

        # raw_author_score = 0.5 (neutral) < 0.7 — neither tier applies
        assert "strong_match_floor" not in scored.score_breakdown

    def test_floor_not_applied_wrong_author(self):
        """Good title but completely wrong author → no floor."""
        query = SearchQuery(clean_title="Blood River", author_hint="Tolkien")
        scored = score_candidate(query, self._BLOOD_RIVER)

        assert "strong_match_floor" not in scored.score_breakdown

    def test_floor_not_applied_when_base_already_above(self):
        """ISBN present → base already high; floor adds nothing (no key in breakdown)."""
        # ISBN match pushes base well above any floor
        query = SearchQuery(
            clean_title="Project Hail Mary",
            author_hint="Andy Weir",
            isbn="9780593135204",
        )
        scored = score_candidate(query, PROJECT_HAIL_MARY)

        # Floor should not appear because confidence >= floor already
        assert "strong_match_floor" not in scored.score_breakdown

    def test_floor_never_lowers_score(self):
        """Floor can only raise — it should never reduce an already-passing score."""
        query = SearchQuery(
            clean_title="Project Hail Mary",
            author_hint="Andy Weir",
            isbn="9780593135204",
            year_hint=2021,
        )
        scored_with_isbn = score_candidate(query, PROJECT_HAIL_MARY)
        query_no_isbn = SearchQuery(
            clean_title="Project Hail Mary",
            author_hint="Andy Weir",
            year_hint=2021,
        )
        scored_no_isbn = score_candidate(query_no_isbn, PROJECT_HAIL_MARY)

        # Both should be valid; no-ISBN score gets lifted by floor
        assert scored_no_isbn.confidence >= STRONG_MATCH_FLOOR
        # ISBN score should always be at least as high as (or higher than) no-ISBN score
        assert scored_with_isbn.confidence >= scored_no_isbn.confidence
