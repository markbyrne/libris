"""Tests for cover download validation — rejecting placeholder/junk images.

Cover APIs serve junk with HTTP 200: OpenLibrary returns an "image not
available" placeholder JPEG for missing covers, blank 1x1 GIFs exist, and
failed CDNs return HTML error pages.  Two defence layers:

1. OpenLibrary cover URLs carry ?default=false → missing covers are 404s
   (the placeholder is a full-size real JPEG that content inspection cannot
   reliably distinguish from an actual cover).
2. _download_cover validates content-type, byte size, and pixel dimensions.
"""

from __future__ import annotations

import struct
import zlib
from unittest.mock import MagicMock

import httpx

from libris.metadata.resolver import _download_cover, _image_dimensions

# ---------------------------------------------------------------------------
# Image fixtures (handcrafted headers — no imaging library)
# ---------------------------------------------------------------------------

def _png(width: int, height: int, pad_to: int = 2048) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    chunk = b"IHDR" + ihdr
    data = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr)) + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )
    return data + b"\x00" * max(0, pad_to - len(data))


def _gif(width: int, height: int, pad_to: int = 2048) -> bytes:
    data = b"GIF89a" + struct.pack("<HH", width, height)
    return data + b"\x00" * max(0, pad_to - len(data))


def _jpeg(width: int, height: int, pad_to: int = 2048) -> bytes:
    # SOI + APP0 (skipped by the scanner) + SOF0 carrying the dimensions
    app0_payload = b"JFIF\x00" + b"\x00" * 9
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload
    sof_payload = b"\x08" + struct.pack(">HH", height, width) + b"\x03"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    data = b"\xff\xd8" + app0 + sof0
    return data + b"\x00" * max(0, pad_to - len(data))


def _client_returning(content: bytes, content_type: str = "image/jpeg") -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.content = content
    response.headers = {"content-type": content_type}
    response.raise_for_status = MagicMock()
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = response
    return client


# ---------------------------------------------------------------------------
# _image_dimensions
# ---------------------------------------------------------------------------

class TestImageDimensions:
    def test_png(self):
        assert _image_dimensions(_png(330, 500)) == (330, 500)

    def test_gif(self):
        assert _image_dimensions(_gif(1, 1)) == (1, 1)

    def test_jpeg_with_app0_segment(self):
        assert _image_dimensions(_jpeg(308, 475)) == (308, 475)

    def test_unknown_format_returns_none(self):
        assert _image_dimensions(b"not an image at all" * 10) is None


# ---------------------------------------------------------------------------
# _download_cover validation
# ---------------------------------------------------------------------------

class TestDownloadCoverValidation:
    def test_real_cover_accepted(self):
        client = _client_returning(_jpeg(330, 500))
        path = _download_cover("http://covers/x.jpg", client)
        assert path is not None
        path.unlink()

    def test_html_error_page_rejected(self):
        client = _client_returning(b"<html>not found</html>" * 100, "text/html")
        assert _download_cover("http://covers/x.jpg", client) is None

    def test_tiny_body_rejected(self):
        """Blank 1x1 GIF stubs are well under the size floor."""
        client = _client_returning(_gif(1, 1, pad_to=0), "image/gif")
        assert _download_cover("http://covers/x.jpg", client) is None

    def test_tracking_pixel_dimensions_rejected(self):
        """Large-bodied but 1x1 image still rejected on dimensions."""
        client = _client_returning(_gif(1, 1, pad_to=4096), "image/gif")
        assert _download_cover("http://covers/x.jpg", client) is None

    def test_unknown_format_fails_open(self):
        """A big image/* body the sniffer can't parse is accepted (WebP etc.)."""
        client = _client_returning(b"RIFF" + b"\x00" * 4096, "image/webp")
        path = _download_cover("http://covers/x.jpg", client)
        assert path is not None
        path.unlink()

    def test_http_error_rejected(self):
        """?default=false turns OL placeholders into 404s → rejected here."""
        response = MagicMock(spec=httpx.Response)
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )
        client = MagicMock(spec=httpx.Client)
        client.get.return_value = response
        assert _download_cover("http://covers/x.jpg?default=false", client) is None


# ---------------------------------------------------------------------------
# OpenLibrary URLs carry ?default=false
# ---------------------------------------------------------------------------

class TestOpenLibraryCoverUrls:
    def _fetch_candidates(self, doc: dict):
        import json

        from libris.metadata.base import SearchQuery
        from libris.metadata.open_library import fetch

        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {"docs": [doc]}
        response.headers = {}
        client = MagicMock(spec=httpx.Client)
        client.get.return_value = response
        assert json  # imported for parity with other tests
        return fetch(SearchQuery(clean_title="The Vegetarian"), client=client)

    def test_cover_i_url_has_default_false(self):
        scored = self._fetch_candidates({
            "title": "The Vegetarian", "author_name": ["Han Kang"], "cover_i": 12345,
        })
        assert scored[0].candidate.cover_url == (
            "https://covers.openlibrary.org/b/id/12345-L.jpg?default=false"
        )

    def test_isbn_fallback_url_has_default_false(self):
        scored = self._fetch_candidates({
            "title": "The Vegetarian", "author_name": ["Han Kang"],
            "isbn": ["9780553448184"],
        })
        assert scored[0].candidate.cover_url == (
            "https://covers.openlibrary.org/b/isbn/9780553448184-L.jpg?default=false"
        )
