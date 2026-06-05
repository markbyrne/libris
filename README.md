# Libris

**Automatic Calibre import with confidence-scored metadata matching.**

If you use Calibre to manage your ebook and audiobook library, you've likely hit these problems:
- Books imported with the wrong author or title — silently, with no warning
- Manually dragging files into Calibre one at a time
- Audiobooks arriving as a folder of MP3 parts with no metadata
- `calibredb` commands that require you to be at the machine

Libris solves all of this. It watches a directory for new ebooks and audiobooks, automatically matches them to the correct metadata using Google Books and OpenLibrary, converts files to the right format (EPUB for ebooks, M4B with chapter markers for audiobooks), and imports them directly into your Calibre library via `calibredb`.

The key difference from a simple import script: Libris scores each metadata match for confidence. Files it's certain about are imported immediately. Files where the match is ambiguous are quarantined in a review folder and you're notified — so your library is never silently polluted with wrong metadata.

Works with local Calibre installations and with [calibre-web](https://github.com/janeczku/calibre-web) running in Docker. Pairs naturally with self-hosted download managers like LazyLibrarian, Readarr, and similar tools.

---

## Features

- **Automatic import** — drop a file, it appears in Calibre with correct metadata
- **Confidence scoring** — two independent metadata sources cross-checked before import
- **Review queue** — low-confidence matches held for your approval, never silently wrong
- **Push notifications** — ntfy.sh alerts when files need attention
- **Audiobook support** — converts to M4B, combines multi-part files with chapter markers
- **Ebook support** — converts any format to EPUB via Calibre's ebook-convert
- **Docker-aware** — works with calibre-web running in a container
- **Cross-platform** — macOS (fswatch) and Linux (inotifywait)
- **Crash-safe** — SQLite state store, source files only deleted after confirmed import

---

## Requirements

### macOS
```bash
brew install fswatch ffmpeg calibre
```

### Linux
```bash
sudo apt install inotify-tools ffmpeg
# Calibre: https://calibre-ebook.com/download_linux
```

### Python
```
Python 3.10+
```

---

## Installation

```bash
git clone https://github.com/markbyrne/libris.git
cd libris
pip install .
```

For development (editable install with test dependencies):
```bash
pip install -e ".[dev]"
```

---

## Configuration

Copy the example config and edit it:

```bash
cp config.example.yaml config.yaml
```

### Minimal config (local Calibre)

```yaml
watcher:
  incoming_dir: ~/books/incoming

paths:
  staging_dir: ~/books/staging
  review_dir: ~/books/review
  failed_dir: ~/books/failed
  state_db: ~/books/libris.db

calibre:
  mode: local
  library_path: ~/Calibre Library

metadata:
  confidence_threshold: 0.75

ntfy:
  topic: my-libris-alerts
  enabled: true
```

### Docker config (e.g. calibre-web in a container)

```yaml
calibre:
  mode: docker
  docker_container: calibre-web
  path_map:
    /media/books: /books          # host path: container path
```

### Environment variable overrides

Any config value can be overridden with a `LIBRIS_` prefixed environment variable:

```bash
LIBRIS_CALIBRE_MODE=docker
LIBRIS_METADATA_CONFIDENCE_THRESHOLD=0.80
LIBRIS_NTFY_TOPIC=my-topic
```

---

## Usage

### Validate your config

```bash
libris check-config --config config.yaml
```

### Process a single file (test without running the daemon)

```bash
libris import-one /path/to/book.epub --config config.yaml
```

Output:
```
Result: imported
Title:  Project Hail Mary
Author: Andy Weir
Score:  0.91
```

### Start the daemon

```bash
libris run --config config.yaml
```

Libris will watch the `incoming_dir` and process files as they arrive. Drop any ebook or audiobook into the directory and it will be imported automatically.

### Check what's waiting in review

Files below the confidence threshold are moved to `review_dir` and never deleted. Check them with:

```bash
libris list-review --config config.yaml
```

```
2 file(s) in review:

  some-obscure-title.epub
    Matched: A Similar Title by Unknown Author
    Score:   0.51
    Path:    ~/books/review/some-obscure-title.epub
```

Import them manually once you've verified the match, or rename the file and drop it back into `incoming_dir` with a clearer filename.

---

## Confidence scoring

Each file is scored against candidates from Google Books and OpenLibrary:

| Signal | Weight |
|--------|--------|
| ISBN match (extracted from filename) | 40% |
| Title similarity (fuzzy) | 30% |
| Author match | 20% |
| Publication year | 10% |

If both sources independently agree on the same book, a bonus is applied. Files scoring below `confidence_threshold` (default `0.75`) go to `review/` instead of being imported.

---

## Supported formats

| Type | Formats |
|------|---------|
| Ebook | epub, mobi, pdf, azw, azw3, cbz, cbr, lit, fb2, djvu, doc, docx, txt |
| Audiobook | mp3, m4a, m4b, flac, ogg, aac, opus, wav |

Multi-part audiobooks (a folder of files) are automatically combined into a single M4B with chapter markers.

---

## Notifications

Libris uses [ntfy.sh](https://ntfy.sh) for push notifications. Set your topic in config and install the ntfy app on your phone.

Notifications fire when:
- A file is quarantined to `review/` (low confidence match)
- A file fails processing and moves to `failed/`

---

## Run on startup (Linux)

Add to crontab (`crontab -e`):

```
@reboot libris run --config /home/user/libris.yaml >> /home/user/libris.log 2>&1 &
```

---

## State database

Libris keeps a SQLite database to track every file it has seen. Useful queries:

```sql
-- Show files in review
SELECT current_path, matched_title, matched_author, confidence FROM files WHERE state='review';

-- Show failed files and why
SELECT original_path, error_msg FROM files WHERE state='failed';

-- Reset a file stuck in processing (e.g. after a crash)
UPDATE files SET state='incoming' WHERE state='processing';
```

---

## Roadmap

- [ ] Cover art embedding for M4B audiobooks
- [ ] Series metadata support (Calibre series + series index)
- [ ] Automatic retry with backoff for failed imports
- [ ] Web UI for reviewing and approving low-confidence matches
- [ ] ISBN lookup as primary metadata source when barcode scanning

---

<!-- TODO: MARKETING
When making this repo public:
- Post to r/Calibre and r/selfhosted
- Post to MobileRead forums (mobileread.com) — largest Calibre community
- Submit PR to awesome-selfhosted list (github.com/awesome-selfhosted/awesome-selfhosted)
- Publish to PyPI: pip install libris
- Add GitHub Sponsors / Ko-fi if there's interest
-->

## License

MIT
