"""Tests for libris.classifier."""

import pytest

from libris.classifier import Classifier, MediaType


@pytest.fixture
def clf():
    return Classifier()


@pytest.mark.parametrize("filename,expected", [
    ("book.epub", MediaType.EBOOK),
    ("book.EPUB", MediaType.EBOOK),
    ("book.mobi", MediaType.EBOOK),
    ("book.pdf", MediaType.EBOOK),
    ("book.azw", MediaType.EBOOK),
    ("book.azw3", MediaType.EBOOK),
    ("book.cbz", MediaType.EBOOK),
    ("book.cbr", MediaType.EBOOK),
    ("book.txt", MediaType.EBOOK),
    ("book.docx", MediaType.EBOOK),
    ("audio.mp3", MediaType.AUDIOBOOK),
    ("audio.m4a", MediaType.AUDIOBOOK),
    ("audio.m4b", MediaType.AUDIOBOOK),
    ("audio.flac", MediaType.AUDIOBOOK),
    ("audio.ogg", MediaType.AUDIOBOOK),
    ("audio.aac", MediaType.AUDIOBOOK),
    ("audio.opus", MediaType.AUDIOBOOK),
    ("audio.wav", MediaType.AUDIOBOOK),
    ("video.mp4", MediaType.UNKNOWN),
    ("image.jpg", MediaType.UNKNOWN),
    ("archive.zip", MediaType.UNKNOWN),
    ("noext", MediaType.UNKNOWN),
])
def test_classify_by_extension(clf, tmp_path, filename, expected):
    path = tmp_path / filename
    assert clf.classify(path) == expected


def test_is_ebook(clf, tmp_path):
    assert clf.is_ebook(tmp_path / "book.epub")
    assert not clf.is_ebook(tmp_path / "audio.mp3")


def test_is_audiobook(clf, tmp_path):
    assert clf.is_audiobook(tmp_path / "audio.m4b")
    assert not clf.is_audiobook(tmp_path / "book.epub")
