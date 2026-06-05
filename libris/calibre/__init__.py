"""Calibre backend factory."""

from ..config import CalibreConfig
from .base import CalibreBackend


def get_calibre(config: CalibreConfig) -> CalibreBackend:
    """Return the appropriate CalibreBackend based on config.mode."""
    if config.mode == "local":
        from .local import LocalCalibre
        return LocalCalibre(config)
    elif config.mode == "docker":
        from .docker import DockerCalibre
        return DockerCalibre(config)
    else:
        raise ValueError(f"Unknown calibre mode: {config.mode!r}")
