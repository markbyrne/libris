"""Metadata resolution: multi-source lookup with confidence scoring."""

from .base import BookCandidate, MetadataResult, ScoredCandidate, SearchQuery
from .resolver import resolve_metadata

__all__ = [
    "BookCandidate",
    "MetadataResult",
    "ScoredCandidate",
    "SearchQuery",
    "resolve_metadata",
]
