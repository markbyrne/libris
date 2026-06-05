"""Docker calibredb backend — wraps calibredb inside a running container.

Path translation:
  The host filesystem path must be translated to the container-internal path
  before passing it to calibredb. This is configured via calibre.path_map:

    path_map:
      /media/pidrive/Books: /books
      /media/pidrive/completed/books: /add

  The longest matching prefix wins (most-specific match).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from ..config import CalibreConfig
from ..exceptions import CalibreImportError, ConversionError
from ..metadata.base import MetadataResult
from .base import CalibreBackend
from .local import _metadata_flags, _parse_book_id

log = logging.getLogger(__name__)


class DockerCalibre(CalibreBackend):
    """Runs calibredb and ebook-convert via `docker exec <container>`."""

    def __init__(self, config: CalibreConfig) -> None:
        self._config = config
        self._container = config.docker_container
        # Sort by prefix length descending so longest match wins
        self._path_map = sorted(
            config.path_map.items(),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )

    def add_book(self, file_path: Path) -> int:
        container_path = self._translate(file_path)
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "add",
            container_path,
            "--automerge", "ignore",
        ]
        log.debug("calibre.docker.add", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.error(
                "calibre.docker.add_failed",
                extra={"stderr": result.stderr, "stdout": result.stdout},
            )
            raise CalibreImportError(
                f"docker calibredb add failed (rc={result.returncode}): {result.stderr.strip()}"
            )

        book_id = _parse_book_id(result.stdout)
        log.info("calibre.docker.added", extra={"file": str(file_path), "book_id": book_id})
        return book_id

    def set_metadata(self, book_id: int, result: MetadataResult) -> None:
        if book_id < 0:
            return
        cmd = ["docker", "exec", self._container, "calibredb", "set_metadata", str(book_id)]
        for flag in _metadata_flags(result):
            cmd += flag
        log.debug("calibre.docker.set_metadata", extra={"cmd": cmd})
        result_proc = subprocess.run(cmd, capture_output=True, text=True)
        if result_proc.returncode != 0:
            log.warning("calibre.docker.set_metadata_failed", extra={"stderr": result_proc.stderr})

    def set_cover(self, book_id: int, cover_path: Path) -> None:
        if book_id < 0 or not cover_path.exists():
            return
        container_path = self._translate(cover_path)
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "set_cover", str(book_id), container_path,
        ]
        log.debug("calibre.docker.set_cover", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("calibre.docker.set_cover_failed", extra={"stderr": result.stderr})
        else:
            log.info("calibre.docker.cover_set", extra={"book_id": book_id})

    def convert_ebook(self, input_path: Path, output_path: Path) -> None:
        container_input = self._translate(input_path)
        container_output = self._translate(output_path)
        cmd = [
            "docker", "exec", self._container,
            "ebook-convert", container_input, container_output,
        ]
        log.debug("calibre.docker.convert", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.error("calibre.docker.convert_failed", extra={"stderr": result.stderr})
            raise ConversionError(
                f"docker ebook-convert failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        log.info("calibre.docker.converted", extra={"output": str(output_path)})

    def _translate(self, host_path: Path) -> str:
        """Translate a host absolute path to its container equivalent."""
        host_str = str(host_path)
        for host_prefix, container_prefix in self._path_map:
            if host_str.startswith(host_prefix):
                return container_prefix + host_str[len(host_prefix):]
        # No mapping found — pass through as-is (may fail inside container)
        log.warning(
            "calibre.docker.no_path_mapping",
            extra={"path": host_str, "map_keys": [k for k, _ in self._path_map]},
        )
        return host_str
