"""ntfy.sh push notifications.

Notification failures are always logged at WARNING and swallowed —
they must never propagate and crash the pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx

from .config import NtfyConfig
from .metadata.base import MetadataResult
from .state import FileRecord

log = logging.getLogger(__name__)


class Notifier:
    """Send push notifications via ntfy.sh."""

    def __init__(self, config: NtfyConfig) -> None:
        self._config = config
        self._client = httpx.Client(timeout=8.0)

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    def send_review_alert(self, record: FileRecord, result: MetadataResult) -> None:
        """Notify that a file was quarantined due to low metadata confidence."""
        if not self._config.enabled or not self._config.topic:
            return

        confidence_pct = f"{result.confidence:.0%}"
        filename = Path(record.original_path).name
        body = (
            f"File: {filename}\n"
            f"Best match: {result.title}"
            + (f" by {result.author}" if result.author else "")
            + f"\n"
            f"Confidence: {confidence_pct} (below threshold)\n"
            f"Moved to: review/"
        )
        self._post(
            title=f"📚 Low confidence match — {confidence_pct}",
            body=body,
            priority="default",
            tags=["books", "warning"],
        )

    def send_error_alert(self, record: FileRecord, error: Exception) -> None:
        """Notify that a file failed processing."""
        if not self._config.enabled or not self._config.topic:
            return

        filename = Path(record.original_path).name
        body = (
            f"File: {filename}\n"
            f"Error: {type(error).__name__}: {str(error)[:200]}\n"
            f"Moved to: failed/"
        )
        self._post(
            title="❌ Import failed",
            body=body,
            priority="high",
            tags=["books", "error"],
        )

    def send_pending_parts_alert(self, record: FileRecord) -> None:
        """Notify that a part was staged and we're waiting for siblings."""
        if not self._config.enabled or not self._config.topic:
            return
        filename = Path(record.current_path).name
        received = 1  # at least the part we just stored
        total = record.total_parts or "?"
        self._post(
            title=f"⏳ Part {record.part_num} of {total} received",
            body=(
                f"File: {filename}\n"
                f"Waiting for remaining parts before import.\n"
                f"Run 'libris list-pending' to check status."
            ),
            priority="low",
            tags=["books", "hourglass_flowing_sand"],
        )

    def send_imported_alert(self, record: FileRecord, result: Optional[MetadataResult]) -> None:
        """Notify of a successful import (optional; keeps ntfy noise low)."""
        if not self._config.enabled or not self._config.topic:
            return
        filename = Path(record.original_path).name
        title_str = result.title if result else filename
        author_str = f" by {result.author}" if result and result.author else ""
        self._post(
            title=f"✅ Imported: {title_str}{author_str}",
            body=f"File: {filename}",
            priority="low",
            tags=["books", "white_check_mark"],
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(
        self,
        title: str,
        body: str,
        priority: str = "default",
        tags: Optional[list[str]] = None,
    ) -> None:
        """POST to ntfy.sh. Swallows all exceptions."""
        url = f"{self._config.base_url.rstrip('/')}/{self._config.topic}"
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": ",".join(tags or []),
        }
        if self._config.auth_token:
            headers["Authorization"] = f"Bearer {self._config.auth_token}"

        try:
            response = self._client.post(url, content=body.encode(), headers=headers)
            response.raise_for_status()
            log.debug("notifier.sent", extra={"title": title, "url": url})
        except Exception as exc:
            # Include the error in the message itself — the standard log format
            # doesn't render extra= fields, so they would be silently invisible.
            log.warning("notifier.failed: %s", exc, extra={"title": title, "url": url})
