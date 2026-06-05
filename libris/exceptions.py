"""Typed exception hierarchy for libris."""


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
