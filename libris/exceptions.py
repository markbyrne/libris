"""Typed exception hierarchy for libris."""

from __future__ import annotations


class BookPipelineError(Exception):
    """Base exception for all libris errors."""


class ConfigError(BookPipelineError):
    """Raised when configuration is invalid or missing required fields."""


class ClassificationError(BookPipelineError):
    """Raised when a file cannot be classified as ebook or audiobook."""


class ConversionError(BookPipelineError):
    """Raised when format conversion (ffmpeg or ebook-convert) fails."""


class MetadataError(BookPipelineError):
    """Raised when metadata lookup fails unrecoverably."""


class CalibreError(BookPipelineError):
    """Raised when calibredb import or conversion fails."""


class CalibreImportError(CalibreError):
    """Raised when calibredb add returns a non-zero exit code."""


class WatcherError(BookPipelineError):
    """Raised when the file watcher subprocess fails to start or crashes."""


class NotifierError(BookPipelineError):
    """Raised internally by notifier; always swallowed by the pipeline."""


class RateLimitError(BookPipelineError):
    """Raised when a metadata API returns HTTP 429 Too Many Requests.

    Callers (e.g. the rematch CLI loop) catch this separately from generic
    fetch errors so they can offer the user a wait/key/skip choice rather
    than silently returning no results.
    """

    def __init__(
        self,
        source: str,
        retry_after: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.source = source            # "google_books" | "open_library"
        self.retry_after = retry_after  # seconds from Retry-After header, or None
        self.reason = reason            # e.g. "rateLimitExceeded", "dailyLimitExceeded"
        msg = f"{source} rate limited"
        if reason:
            msg += f" ({reason})"
        if retry_after:
            msg += f" (retry after {retry_after}s)"
        super().__init__(msg)
