"""libris — self-hosted book/audiobook import pipeline for Calibre."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Distribution name is pylibris (the import package remains libris)
    __version__ = version("pylibris")
except PackageNotFoundError:
    # Editable/source checkouts installed under the old name, or not installed
    try:
        __version__ = version("libris")
    except PackageNotFoundError:
        __version__ = "0.0.0+unknown"
