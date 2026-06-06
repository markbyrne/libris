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
    """Classify a file or directory by its extension / contents."""

    def classify(self, path: Path) -> MediaType:
        if path.is_dir():
            return self._classify_directory(path)
        ext = path.suffix.lstrip(".").lower()
        if ext in EBOOK_EXTENSIONS:
            return MediaType.EBOOK
        if ext in AUDIO_EXTENSIONS:
            return MediaType.AUDIOBOOK
        return MediaType.UNKNOWN

    def _classify_directory(self, path: Path) -> MediaType:
        """Classify a directory by inspecting its contents.

        Audio files take priority — a directory is treated as an audiobook
        if it contains any recognised audio format.  Returns UNKNOWN if the
        directory is empty or contains no supported files.
        """
        has_ebook = False
        try:
            for child in path.rglob("*"):
                if not child.is_file():
                    continue
                ext = child.suffix.lstrip(".").lower()
                if ext in AUDIO_EXTENSIONS:
                    return MediaType.AUDIOBOOK   # short-circuit on first audio file
                if ext in EBOOK_EXTENSIONS:
                    has_ebook = True
        except OSError:
            return MediaType.UNKNOWN
        return MediaType.EBOOK if has_ebook else MediaType.UNKNOWN

    def is_ebook(self, path: Path) -> bool:
        return self.classify(path) == MediaType.EBOOK

    def is_audiobook(self, path: Path) -> bool:
        return self.classify(path) == MediaType.AUDIOBOOK
