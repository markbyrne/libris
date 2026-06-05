"""File type classification: extension → EBOOK | AUDIOBOOK | UNKNOWN."""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class MediaType(str, Enum):
    EBOOK     = "ebook"
    AUDIOBOOK = "audiobook"
    UNKNOWN   = "unknown"


# Supported extensions (lowercase, no leading dot)
EBOOK_EXTENSIONS: frozenset[str] = frozenset({
    "epub", "mobi", "pdf", "azw", "azw3", "djvu",
    "cbz", "cbr", "lit", "fb2", "lrf", "odt", "rtf",
    "doc", "docx", "txt",
})

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    "mp3", "m4a", "m4b", "flac", "ogg", "aac", "opus", "wav",
})


class Classifier:
    """Classify a file by its extension."""

    def classify(self, path: Path) -> MediaType:
        ext = path.suffix.lstrip(".").lower()
        if ext in EBOOK_EXTENSIONS:
            return MediaType.EBOOK
        if ext in AUDIO_EXTENSIONS:
            return MediaType.AUDIOBOOK
        return MediaType.UNKNOWN

    def is_ebook(self, path: Path) -> bool:
        return self.classify(path) == MediaType.EBOOK

    def is_audiobook(self, path: Path) -> bool:
        return self.classify(path) == MediaType.AUDIOBOOK
