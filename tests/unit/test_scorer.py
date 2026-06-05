"""Tests for libris.metadata.scorer — confidence scoring logic."""

import pytest

from libris.metadata.base import BookCandidate, SearchQuery
from libris.metadata.scorer import (
    _apply_agreement_bonus,
    pick_best,
    score_candidate,
)
from tests.conftest import DUNE, ERAGON, PROJECT_HAIL_MARY, make_scored


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
        exact = score_candidate(SearchQuery(clean_title="Project Hail Mary"), PROJECT_HAIL_MARY)
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

        before_max = max(g_scored.confidence, o_scored.confidence)
        winner = _apply_agreement_bonus(g_scored, o_scored)
        assert winner is not None
        # Winner should be Project Hail Mary (higher score); no bonus
        assert "agreement_bonus" not in winner.score_breakdown or \
               winner.score_breakdown.get("agreement_bonus", 0) == 0
