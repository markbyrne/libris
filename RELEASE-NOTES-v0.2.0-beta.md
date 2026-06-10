# Libris v0.2.0-beta

Libris is an intelligent book and audiobook organiser for self-hosted Calibre libraries. Drop a file into your incoming folder — Libris handles metadata lookup, deduplication, format conversion, and Calibre import automatically.

This is the first public beta release.

---

## What's new since v0.1.0

### Installation

- **Interactive installer** (`install.sh`) for macOS and Linux — checks and installs system dependencies, writes your config, and optionally sets up a LaunchAgent or systemd user service so Libris runs in the background automatically.
- `LIBRIS_CONFIG` environment variable support for locating your config from any working directory.

### Audiobook pipeline

- **Multi-part audiobook support** — part files are held in a pending stage and combined into a single M4B once all parts arrive.
- **Series detection** — series name and index are detected from metadata and written to M4B tags and Calibre.
- **Audiobook directory drops** — drop a directory of audio files into `incoming/` and Libris dispatches them correctly.
- **Format merging** — new formats (e.g. an MP3 alongside an existing M4B) are merged into the existing Calibre book record instead of creating duplicates.

### Ebook pipeline

- **Format policy** — configurable preferred format; Libris converts to it or imports as-is based on your settings.
- **Mixed incoming directories** — directories containing both audiobook and ebook files are handled correctly.

### Metadata

- **Full metadata pipeline** — cover art, publisher, description, series, and language are all written on import.
- **DuckDuckGo fallback** — when both Google Books and OpenLibrary return zero results, a DDG search is tried automatically.
- **Strong-match floor** — a minimum confidence floor prevents low-quality matches from being auto-accepted.
- **Rate limit handling** — Google Books rate limits are detected and surfaced clearly, with per-API result counts shown during rematch.

### Review workflow

- **`libris rematch`** — interactively re-query metadata for any item in review, with an API status panel and query tips before each search.
- **`libris review-discard`** — delete unwanted review items; `--stale` prunes orphaned records automatically.
- **`libris review-accept --overwrite`** — accept a duplicate import and overwrite the existing Calibre record.
- **`libris show-cover`** — open a review item's cover image in the browser.
- **Duplicate detection** — Calibre is checked before import; duplicates surface in review with an `[!]` label and an overwrite/discard/keep prompt.
- Renamed prompt actions for clarity: `[s]` skip, `[r]` rematch.

### CLI & UX

- **`libris clean-library`** — remove corrupt or unrecoverable records from the Calibre database.
- **`--delete` flag on `recover`** — permanently remove unrecoverable failed records.
- **`--search` flag on `revert-import`** — interactive book lookup when reverting.
- **`/api` and `/clear` slash commands** in the rematch prompt.
- **`--version` flag** now works correctly.
- Startup scan and periodic re-scan of `incoming_dir` — no need to restart the daemon after adding files.
- General CLI output polish across all commands.

### Security & reliability

- Symlink check before `resolve()` in `import-one` — prevents path traversal via malicious symlinks.
- `calibredb` availability is checked at startup with a clear error if not found.
- `pip` guard prevents accidental system Python modifications.
- Fixed `--automerge` duplicate-ID corruption.
- Fixed `set_cover` resetting title/authors to Unknown after import.
- Fixed stale review IDs shown after `review-accept --accept-all`.
- Fixed orphaned part files in `staging/pending/` after review.
- Fixed M4B part combination encoder error (stream copy instead of re-encode).

---

## Known limitations

- Integration tests require `ffmpeg` and `calibredb` in `PATH` and are skipped in CI by default.
- Live metadata tests require network access and are opt-in (`-m live`).

---

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/markbyrne/libris/v0.2.0-beta/install.sh | bash
```

Or manually:

```bash
pip install libris==0.2.0b1
```

See the [README](README.md) for full setup instructions.
