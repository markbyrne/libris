"""Configuration dataclasses, YAML loader, and LIBRIS_ environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field  # noqa: F401 — field used in Config
from pathlib import Path
from typing import Literal

import yaml

from .exceptions import ConfigError

# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

@dataclass
class WatcherConfig:
    incoming_dir: Path
    poll_interval_seconds: float = 2.0  # fswatch --latency on macOS
    # How often to re-scan incoming_dir for files that arrived while the
    # daemon was offline.  Also runs once at startup.  Set to 0 to disable.
    scan_interval_hours: float = 1.0


@dataclass
class PathsConfig:
    staging_dir: Path     # intermediate converted files
    review_dir: Path      # quarantine for low-confidence matches
    failed_dir: Path      # processing errors
    state_db: Path        # SQLite database path


@dataclass
class CalibreConfig:
    mode: Literal["local", "docker"] = "local"
    # library_db_path — where metadata.db lives; used with --with-library.
    # Also accepts the legacy YAML key 'library_path' (see load_config).
    library_db_path: Path | None = None
    # book_file_path — where physical book files (EPUB, M4B, etc.) should live.
    # Used with calibre-web "Separate Book Files from Library" feature.
    # If unset, defaults to library_db_path (i.e. no split — classic setup).
    book_file_path: Path | None = None
    docker_container: str = "calibre-web"     # docker mode: container name
    # Maps host path prefixes → container path prefixes for docker mode.
    # Example: {"/media/pidrive/Books": "/books"}
    path_map: dict[str, str] = field(default_factory=dict)
    # reconnect_url — calibre-web /reconnect endpoint, pinged after each
    # import so calibre-web drops its (possibly stale) DB connection instead
    # of holding it open across external calibredb writes.  Requires
    # calibre-web started with the -r flag.  None = disabled.
    # Example: "http://192.168.1.10:8083/reconnect"
    reconnect_url: str | None = None

    @property
    def library_path(self) -> Path | None:
        """Backward-compat alias for library_db_path.

        Code that accesses config.calibre.library_path continues to work
        without changes; update to library_db_path when convenient.
        """
        return self.library_db_path

    @property
    def effective_book_path(self) -> Path | None:
        """Path where physical book files are stored.

        Returns book_file_path if set; otherwise falls back to library_db_path.
        Use this wherever book file I/O is needed (exports, format adds, etc.).
        """
        return self.book_file_path or self.library_db_path


@dataclass
class MetadataConfig:
    confidence_threshold: float = 0.75
    google_books_api_key: str | None = None   # None = unauthenticated (60 req/min)
    mock_mode: bool = False                    # True = return fixture data, no HTTP
    overwrite_existing: bool = True            # always overwrite embedded metadata
    # What to do when an incoming file matches a book already in Calibre:
    #   review — move to review/ with a duplicate warning (default, safest)
    #   skip   — discard silently; mark IMPORTED so it won't be re-processed
    #   import — always import regardless (current behaviour before this feature)
    duplicate_action: str = "review"


@dataclass
class OutputConfig:
    preferred_ebook_format: str = "epub"      # epub | mobi
    preferred_audio_format: str = "m4b"       # m4b (only recommended option)
    embed_cover_art: bool = True              # download and embed cover images
    # Controls how non-preferred-format ebooks are handled on import:
    #   preferred — convert to preferred_ebook_format, import the converted
    #               file only, then delete the original source file
    #   all       — import the file in whatever format it arrived (no
    #               conversion); Calibre stores the native format as-is
    ebook_format_policy: str = "preferred"


@dataclass
class MultiPartConfig:
    """Settings for multi-part audiobook handling."""
    # After this many hours with an incomplete set, escalate parts to review/
    # so the user can decide whether to force-combine or wait longer.
    timeout_hours: float = 48.0


@dataclass
class ApiConfig:
    """Directive API — lets an external tool (e.g. Librarr) pre-register a
    metadata match for an incoming file so the pipeline skips its own
    Google Books / OpenLibrary / DDG lookups. Off by default (fail closed):
    both enabled=True AND a non-empty api_key are required to serve requests.
    """
    enabled: bool = False
    api_key: str = ""


@dataclass
class NtfyConfig:
    topic: str = ""
    base_url: str = "https://ntfy.sh"
    enabled: bool = True
    auth_token: str | None = None             # optional Bearer token for private channels


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    watcher: WatcherConfig
    paths: PathsConfig
    calibre: CalibreConfig
    metadata: MetadataConfig
    output: OutputConfig
    ntfy: NtfyConfig
    multipart: MultiPartConfig = field(default_factory=MultiPartConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> Config:
    """Load config from YAML file, then apply LIBRIS_ environment variable overrides.

    Resolution order: YAML file → environment variables → dataclass defaults.
    """
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open() as f:
        raw: dict = yaml.safe_load(f) or {}

    # ---- watcher ----
    watcher_raw = raw.get("watcher", {})
    incoming_raw = watcher_raw.get("incoming_dir") or os.environ.get("LIBRIS_WATCHER_INCOMING_DIR")
    if not incoming_raw:
        raise ConfigError("watcher.incoming_dir is required")
    watcher = WatcherConfig(
        incoming_dir=Path(incoming_raw).expanduser(),
        poll_interval_seconds=float(
            os.environ.get("LIBRIS_WATCHER_POLL_INTERVAL_SECONDS",
                           watcher_raw.get("poll_interval_seconds", 2.0))
        ),
        scan_interval_hours=float(
            os.environ.get("LIBRIS_WATCHER_SCAN_INTERVAL_HOURS",
                           watcher_raw.get("scan_interval_hours", 1.0))
        ),
    )

    # ---- paths ----
    paths_raw = raw.get("paths", {})

    def _path(env_key: str, yaml_key: str, required: bool = True) -> Path | None:
        val = os.environ.get(env_key) or paths_raw.get(yaml_key)
        if required and not val:
            raise ConfigError(f"paths.{yaml_key} is required")
        return Path(val).expanduser() if val else None

    paths = PathsConfig(
        staging_dir=_path("LIBRIS_PATHS_STAGING_DIR", "staging_dir"),
        review_dir=_path("LIBRIS_PATHS_REVIEW_DIR", "review_dir"),
        failed_dir=_path("LIBRIS_PATHS_FAILED_DIR", "failed_dir"),
        state_db=_path("LIBRIS_PATHS_STATE_DB", "state_db"),
    )

    # ---- calibre ----
    calibre_raw = raw.get("calibre", {})
    calibre_mode = os.environ.get("LIBRIS_CALIBRE_MODE") or calibre_raw.get("mode", "local")
    if calibre_mode not in ("local", "docker"):
        raise ConfigError(f"calibre.mode must be 'local' or 'docker', got: {calibre_mode!r}")

    # Accept library_db_path (new) or library_path (legacy) — both map to library_db_path.
    lib_db_raw = (
        os.environ.get("LIBRIS_CALIBRE_LIBRARY_DB_PATH")
        or os.environ.get("LIBRIS_CALIBRE_LIBRARY_PATH")
        or calibre_raw.get("library_db_path")
        or calibre_raw.get("library_path")
    )
    book_file_raw = (
        os.environ.get("LIBRIS_CALIBRE_BOOK_FILE_PATH")
        or calibre_raw.get("book_file_path")
    )
    calibre = CalibreConfig(
        mode=calibre_mode,
        library_db_path=Path(lib_db_raw).expanduser() if lib_db_raw else None,
        book_file_path=Path(book_file_raw).expanduser() if book_file_raw else None,
        docker_container=os.environ.get("LIBRIS_CALIBRE_DOCKER_CONTAINER")
                         or calibre_raw.get("docker_container", "calibre-web"),
        path_map=calibre_raw.get("path_map") or {},
        reconnect_url=os.environ.get("LIBRIS_CALIBRE_RECONNECT_URL")
                      or calibre_raw.get("reconnect_url") or None,
    )

    if calibre.mode == "local" and calibre.library_db_path is None:
        raise ConfigError(
            "calibre.library_db_path (or legacy calibre.library_path) is required "
            "when calibre.mode is 'local'"
        )
    if calibre.mode == "docker" and not calibre.docker_container:
        raise ConfigError("calibre.docker_container is required when calibre.mode is 'docker'")

    # ---- metadata ----
    metadata_raw = raw.get("metadata", {})
    dup_action = (
        os.environ.get("LIBRIS_METADATA_DUPLICATE_ACTION")
        or metadata_raw.get("duplicate_action", "review")
    )
    if dup_action not in ("review", "skip", "import"):
        raise ConfigError(
            f"metadata.duplicate_action must be 'review', 'skip', or 'import', got: {dup_action!r}"
        )
    metadata = MetadataConfig(
        confidence_threshold=float(
            os.environ.get("LIBRIS_METADATA_CONFIDENCE_THRESHOLD",
                           metadata_raw.get("confidence_threshold", 0.75))
        ),
        google_books_api_key=os.environ.get("LIBRIS_METADATA_GOOGLE_BOOKS_API_KEY")
                              or metadata_raw.get("google_books_api_key"),
        mock_mode=_bool_env("LIBRIS_METADATA_MOCK_MODE", metadata_raw.get("mock_mode", False)),
        overwrite_existing=_bool_env(
            "LIBRIS_METADATA_OVERWRITE_EXISTING",
            metadata_raw.get("overwrite_existing", True),
        ),
        duplicate_action=dup_action,
    )

    if not (0.0 <= metadata.confidence_threshold <= 1.0):
        raise ConfigError(
            f"metadata.confidence_threshold must be between 0.0 and 1.0, "
            f"got: {metadata.confidence_threshold}"
        )

    # ---- output ----
    output_raw = raw.get("output", {})
    preferred_ebook = os.environ.get("LIBRIS_OUTPUT_PREFERRED_EBOOK_FORMAT") \
                      or output_raw.get("preferred_ebook_format", "epub")
    preferred_audio = os.environ.get("LIBRIS_OUTPUT_PREFERRED_AUDIO_FORMAT") \
                      or output_raw.get("preferred_audio_format", "m4b")
    ebook_format_policy = (
        os.environ.get("LIBRIS_OUTPUT_EBOOK_FORMAT_POLICY")
        or output_raw.get("ebook_format_policy", "preferred")
    )
    if preferred_ebook not in ("epub", "mobi"):
        raise ConfigError(f"output.preferred_ebook_format must be 'epub' or 'mobi', got: {preferred_ebook!r}")
    if preferred_audio not in ("m4b",):
        raise ConfigError(f"output.preferred_audio_format must be 'm4b', got: {preferred_audio!r}")
    if ebook_format_policy not in ("preferred", "all"):
        raise ConfigError(
            f"output.ebook_format_policy must be 'preferred' or 'all', got: {ebook_format_policy!r}"
        )
    output = OutputConfig(
        preferred_ebook_format=preferred_ebook,
        preferred_audio_format=preferred_audio,
        embed_cover_art=_bool_env("LIBRIS_OUTPUT_EMBED_COVER_ART", output_raw.get("embed_cover_art", True)),
        ebook_format_policy=ebook_format_policy,
    )

    # ---- ntfy ----
    ntfy_raw = raw.get("ntfy", {})
    ntfy = NtfyConfig(
        topic=os.environ.get("LIBRIS_NTFY_TOPIC") or ntfy_raw.get("topic", ""),
        base_url=os.environ.get("LIBRIS_NTFY_BASE_URL") or ntfy_raw.get("base_url", "https://ntfy.sh"),
        enabled=_bool_env("LIBRIS_NTFY_ENABLED", ntfy_raw.get("enabled", True)),
        auth_token=os.environ.get("LIBRIS_NTFY_AUTH_TOKEN") or ntfy_raw.get("auth_token"),
    )

    # ---- multipart ----
    mp_raw = raw.get("multipart", {})
    multipart = MultiPartConfig(
        timeout_hours=float(
            os.environ.get("LIBRIS_MULTIPART_TIMEOUT_HOURS",
                           mp_raw.get("timeout_hours", 48.0))
        ),
    )

    log_level = os.environ.get("LIBRIS_LOG_LEVEL") or raw.get("log_level", "INFO")
    if log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"Invalid log_level: {log_level!r}")

    # ---- api (directive API — off by default) ----
    api_raw = raw.get("api", {})
    api = ApiConfig(
        enabled=_bool_env("LIBRIS_API_ENABLED", api_raw.get("enabled", False)),
        api_key=os.environ.get("LIBRIS_API_KEY") or api_raw.get("api_key", ""),
    )

    return Config(
        watcher=watcher,
        paths=paths,
        calibre=calibre,
        metadata=metadata,
        output=output,
        ntfy=ntfy,
        multipart=multipart,
        api=api,
        log_level=log_level.upper(),
    )


def _bool_env(env_key: str, default: bool) -> bool:
    """Resolve a boolean from an environment variable or fallback default."""
    val = os.environ.get(env_key)
    if val is None:
        return bool(default)
    return val.lower() in ("1", "true", "yes", "on")
