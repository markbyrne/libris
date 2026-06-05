"""Configuration dataclasses, YAML loader, and LIBRIS_ environment variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


@dataclass
class PathsConfig:
    staging_dir: Path     # intermediate converted files
    review_dir: Path      # quarantine for low-confidence matches
    failed_dir: Path      # processing errors
    state_db: Path        # SQLite database path


@dataclass
class CalibreConfig:
    mode: Literal["local", "docker"] = "local"
    library_path: Path | None = None          # local mode: --with-library
    docker_container: str = "calibre-web"     # docker mode: container name
    # Maps host path prefixes → container path prefixes for docker mode.
    # Example: {"/media/pidrive/Books": "/books"}
    path_map: dict[str, str] = field(default_factory=dict)


@dataclass
class MetadataConfig:
    confidence_threshold: float = 0.75
    google_books_api_key: str | None = None   # None = unauthenticated (60 req/min)
    mock_mode: bool = False                    # True = return fixture data, no HTTP
    overwrite_existing: bool = True            # always overwrite embedded metadata


@dataclass
class OutputConfig:
    preferred_ebook_format: str = "epub"      # epub | mobi
    preferred_audio_format: str = "m4b"       # m4b (only recommended option)
    embed_cover_art: bool = True              # download and embed cover images


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

    lib_path_raw = os.environ.get("LIBRIS_CALIBRE_LIBRARY_PATH") or calibre_raw.get("library_path")
    calibre = CalibreConfig(
        mode=calibre_mode,
        library_path=Path(lib_path_raw).expanduser() if lib_path_raw else None,
        docker_container=os.environ.get("LIBRIS_CALIBRE_DOCKER_CONTAINER")
                         or calibre_raw.get("docker_container", "calibre-web"),
        path_map=calibre_raw.get("path_map") or {},
    )

    if calibre.mode == "local" and calibre.library_path is None:
        raise ConfigError("calibre.library_path is required when calibre.mode is 'local'")
    if calibre.mode == "docker" and not calibre.docker_container:
        raise ConfigError("calibre.docker_container is required when calibre.mode is 'docker'")

    # ---- metadata ----
    metadata_raw = raw.get("metadata", {})
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
    if preferred_ebook not in ("epub", "mobi"):
        raise ConfigError(f"output.preferred_ebook_format must be 'epub' or 'mobi', got: {preferred_ebook!r}")
    if preferred_audio not in ("m4b",):
        raise ConfigError(f"output.preferred_audio_format must be 'm4b', got: {preferred_audio!r}")
    output = OutputConfig(
        preferred_ebook_format=preferred_ebook,
        preferred_audio_format=preferred_audio,
        embed_cover_art=_bool_env("LIBRIS_OUTPUT_EMBED_COVER_ART", output_raw.get("embed_cover_art", True)),
    )

    # ---- ntfy ----
    ntfy_raw = raw.get("ntfy", {})
    ntfy = NtfyConfig(
        topic=os.environ.get("LIBRIS_NTFY_TOPIC") or ntfy_raw.get("topic", ""),
        base_url=os.environ.get("LIBRIS_NTFY_BASE_URL") or ntfy_raw.get("base_url", "https://ntfy.sh"),
        enabled=_bool_env("LIBRIS_NTFY_ENABLED", ntfy_raw.get("enabled", True)),
        auth_token=os.environ.get("LIBRIS_NTFY_AUTH_TOKEN") or ntfy_raw.get("auth_token"),
    )

    log_level = os.environ.get("LIBRIS_LOG_LEVEL") or raw.get("log_level", "INFO")
    if log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"Invalid log_level: {log_level!r}")

    return Config(
        watcher=watcher,
        paths=paths,
        calibre=calibre,
        metadata=metadata,
        output=output,
        ntfy=ntfy,
        log_level=log_level.upper(),
    )


def _bool_env(env_key: str, default: bool) -> bool:
    """Resolve a boolean from an environment variable or fallback default."""
    val = os.environ.get(env_key)
    if val is None:
        return bool(default)
    return val.lower() in ("1", "true", "yes", "on")
