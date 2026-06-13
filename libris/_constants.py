"""Package-wide constants."""

# HTTP timeout buckets (seconds).
# Keep all timeout values here so they're easy to find and tune.
HTTP_TIMEOUT_SHORT: float = 8.0    # ntfy notifications, config probes, DDG queries
HTTP_TIMEOUT_COVER: float = 10.0   # cover image downloads
HTTP_TIMEOUT_API: float = 12.0     # metadata API calls (Google Books, OpenLibrary)
