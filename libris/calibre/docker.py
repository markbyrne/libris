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
from .local import _BOOK_EXTENSIONS, _metadata_flags, _normalise_book_entry, _parse_book_id, _parse_formats

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

    def add_book(
        self,
        file_path: Path,
        title: str | None = None,
        authors: str | None = None,
    ) -> int:
        container_path = self._translate(file_path)
        # Do NOT pass --automerge ignore — see LocalCalibre.add_book for the
        # full explanation.  Libris handles duplicates before calling add_book.
        # --title/--authors control the directory calibredb creates — see
        # LocalCalibre.add_book.
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "add",
            container_path,
        ]
        if title:
            cmd += ["--title", title]
        if authors:
            cmd += ["--authors", authors]
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

        if "were not added" in result.stderr or "was not added" in result.stderr:
            raise CalibreImportError(
                f"calibredb refused to add {file_path.name}: {result.stderr.strip()}"
            )

        book_id = _parse_book_id(result.stdout)
        if book_id < 0:
            raise CalibreImportError(
                f"docker calibredb add returned no book ID for {file_path.name}. "
                f"stdout: {result.stdout.strip()!r}"
            )
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
        # --field cover: expects a path INSIDE the container.  Copy the image
        # into a container-side temp location and reference that path.
        # The OPF approach (calibredb set_metadata BOOK_ID opf) stores OPF XML
        # as text, not image bytes — it does NOT set the cover image.
        ext = cover_path.suffix.lstrip(".").lower()
        cover_name = f"cover.{ext}"
        container_tmp = "/tmp/libris_cover_tmp"

        try:
            subprocess.run(
                ["docker", "exec", self._container, "mkdir", "-p", container_tmp],
                capture_output=True,
            )
            cp = subprocess.run(
                ["docker", "cp", str(cover_path),
                 f"{self._container}:{container_tmp}/{cover_name}"],
                capture_output=True, text=True,
            )
            if cp.returncode != 0:
                log.warning("calibre.docker.set_cover_failed: docker cp: %s", cp.stderr.strip())
                return

            cmd = [
                "docker", "exec", self._container,
                "calibredb", "set_metadata", str(book_id),
                "--field", f"cover:{container_tmp}/{cover_name}",
            ]
            log.debug("calibre.docker.set_cover", extra={"cmd": cmd})
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                log.warning("calibre.docker.set_cover_failed: %s", result.stderr.strip())
            else:
                log.info("calibre.docker.cover_set", extra={"book_id": book_id})
        finally:
            subprocess.run(
                ["docker", "exec", self._container, "rm", "-rf", container_tmp],
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
            "--single-dir",          # force flat output — without this calibredb creates
                                     # per-book subdirectories and may write 0 files to dest_dir
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
        exported = [f for f in dest_dir.rglob("*") if f.is_file() and f.suffix.lower() in _BOOK_EXTENSIONS]
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

    def add_format(self, book_id: int, file_path: Path) -> None:
        container_path = self._translate(file_path)
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "add_format",
            str(book_id),
            container_path,
        ]
        log.debug("calibre.docker.add_format", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CalibreImportError(
                f"docker calibredb add_format failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        log.info("calibre.docker.format_added", extra={"book_id": book_id, "file": str(file_path)})

    def get_formats(self, book_id: int) -> set[str]:
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "list",
            "--search", f"id:{book_id}",
            "--fields", "formats",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return _parse_formats(result.stdout)

    def list_books(self) -> list[dict]:
        import json as _json
        cmd = [
            "docker", "exec", self._container,
            "calibredb", "list",
            "--for-machine",
            "--fields", "id,title,authors,formats",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("calibre.docker.list_books_failed", extra={"stderr": result.stderr})
            return []
        try:
            raw = _json.loads(result.stdout)
        except _json.JSONDecodeError:
            log.warning("calibre.docker.list_books_parse_error", extra={"stdout": result.stdout[:200]})
            return []
        return [_normalise_book_entry(b) for b in raw]

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
