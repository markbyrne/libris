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
from .local import _BOOK_EXTENSIONS, _metadata_flags, _parse_book_id

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
        # Cover is a host-side temp file — copy it into the container first,
        # set it via set_metadata --cover, then remove the container copy.
        # (calibredb has no standalone set_cover subcommand.)
        container_cover = f"/tmp/libris_cover_{cover_path.name}"
        try:
            cp = subprocess.run(
                ["docker", "cp", str(cover_path), f"{self._container}:{container_cover}"],
                capture_output=True, text=True,
            )
            if cp.returncode != 0:
                log.warning(
                    "calibre.docker.set_cover_failed",
                    extra={"stage": "docker_cp", "stderr": cp.stderr},
                )
                return

            cmd = [
                "docker", "exec", self._container,
                "calibredb", "set_metadata",
                str(book_id),
                "--cover", container_cover,
            ]
            log.debug("calibre.docker.set_cover", extra={"cmd": cmd})
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                log.warning("calibre.docker.set_cover_failed", extra={"stderr": result.stderr})
            else:
                log.info("calibre.docker.cover_set", extra={"book_id": book_id})
        finally:
            # Always clean up the container-side temp file
            subprocess.run(
                ["docker", "exec", self._container, "rm", "-f", container_cover],
                capture_output=True,
            )

    def export_book(self, book_id: int, dest_dir: Path) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        container_tmp = "/tmp/libris_export"
        # Export inside the container
        export_cmd = [
            "docker", "exec", self._container,
            "calibredb", "export",
            "--to-dir", container_tmp,
            "--dont-save-cover", "--dont-write-opf",
            "--template", "{title}",
            str(book_id),
        ]
        log.debug("calibre.docker.export", extra={"cmd": export_cmd})
        result = subprocess.run(export_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CalibreImportError(
                f"docker calibredb export failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        # Copy files from container to host dest_dir
        cp_cmd = ["docker", "cp", f"{self._container}:{container_tmp}/.", str(dest_dir)]
        subprocess.run(cp_cmd, capture_output=True, text=True, check=True)
        # Clean up container temp dir
        subprocess.run(
            ["docker", "exec", self._container, "rm", "-rf", container_tmp],
            capture_output=True,
        )
        exported = [f for f in dest_dir.rglob("*") if f.suffix.lower() in _BOOK_EXTENSIONS]
        log.info("calibre.docker.exported", extra={"book_id": book_id, "files": [str(f) for f in exported]})
        return exported

    def remove_book(self, book_id: int) -> None:
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "remove", str(book_id),
        ]
        log.debug("calibre.docker.remove", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CalibreImportError(
                f"docker calibredb remove failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        log.info("calibre.docker.removed", extra={"book_id": book_id})

    def search(self, query: str) -> list[int]:
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "search", query,
        ]
        log.debug("calibre.docker.search", extra={"query": query})
        result = subprocess.run(cmd, capture_output=True, text=True)
        raw = result.stdout.strip()
        if not raw:
            return []
        try:
            return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
        except ValueError:
            log.warning("calibre.docker.search_parse_failed", extra={"raw": raw})
            return []

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
