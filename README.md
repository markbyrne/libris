# Libris

[![CI](https://github.com/markbyrne/libris/actions/workflows/ci.yml/badge.svg)](https://github.com/markbyrne/libris/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pylibris)](https://pypi.org/project/pylibris/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

**Automatic Calibre import with confidence-scored metadata matching.**

If you use Calibre to manage your ebook and audiobook library, you've likely hit these problems:
- Books imported with the wrong author or title — silently, with no warning
- Manually dragging files into Calibre one at a time
- Audiobooks arriving as split parts with no metadata
- `calibredb` commands that require you to be at the machine

Libris solves all of this. It watches a directory for new ebooks and audiobooks, automatically matches them to the correct metadata using Google Books and OpenLibrary, converts files to the right format (EPUB for ebooks, M4B with chapter markers for audiobooks), and imports them directly into your Calibre library via `calibredb`.

The key difference from a simple import script: Libris scores each metadata match for confidence. Files it's certain about are imported immediately. Files where the match is ambiguous are quarantined in a review folder and you're notified — so your library is never silently polluted with wrong metadata.

Works with local Calibre installations and with [calibre-web](https://github.com/janeczku/calibre-web) running in Docker. Works with anything that places files in a watched folder — purchase downloads, Humble Bundle exports, library-management tools, or plain `cp`.

> **Content responsibility:** Libris organizes and imports files you already have. You are responsible for ensuring you have the legal right to the content you process with it.

---

## Recommended Stack

Libris is designed as one component in a self-hosted library pipeline:

```
Your files             Libris                   Calibre DB         Reader App
(any source        →  watches /incoming     →  metadata.db    →  calibre-web
 that writes to       scores + converts         book files         serves to
 a folder)            imports via calibredb                        devices
```

| Component | Role |
|-----------|------|
| **Your acquisition workflow** | Anything that places ebook/audiobook files in Libris's `incoming/` directory — store purchases, DRM-free bundles, or library tools such as Readarr and LazyLibrarian |
| **Libris** | Watches `incoming/`, scores metadata confidence, converts formats (EPUB, M4B), imports into Calibre; holds ambiguous matches for manual review |
| **Calibre** (`calibredb`) | Stores book metadata and files; the authoritative library database |
| **calibre-web** | Serves the library to devices via a web UI; supports OPDS for e-reader apps |

---

## Features

- **Automatic import** — drop a file, it appears in Calibre with correct metadata; startup scan catches files that arrived while the daemon was offline
- **Confidence scoring** — two independent metadata sources cross-checked before import
- **Full metadata** — title, author, cover art, description, publisher, series, language, ISBN all written to Calibre; placeholder/junk cover images are rejected via content-type, size, and dimension checks
- **Series detection** — extracts series name and index from filenames and API data; writes tags for Apple Books, Prologue, and AudioBookshelf
- **Structured filename parsing** — files named with the `title -- author -- year -- publisher -- md5` double-dash convention are parsed field-by-field; extracts ISBN, series/ordinal, narrator, and author initials (the convention encodes `D.J.` as `D_ J_`) before the metadata lookup, yielding near-perfect confidence scores for these files
- **Review queue** — low-confidence matches held for your approval, never silently wrong
- **Interactive rematch** — re-query metadata APIs from the terminal with live score breakdowns
- **Web search fallback** — if both APIs return no results, DuckDuckGo Instant Answers is queried for author/ISBN hints and the search is retried automatically
- **Multi-part audiobooks** — parts held in staging until the complete set arrives, then combined into one M4B with chapter markers and imported automatically
- **Directive API** — external tools (e.g. Librarr) can pre-register a metadata match for an incoming file via a small authenticated HTTP API, so Libris skips its own metadata lookups for that file (off by default; see [Directive API](#directive-api--external-tools-can-pre-register-a-match))
- **Push notifications** — ntfy.sh alerts when files need attention
- **Audiobook support** — converts to M4B, combines multi-part files with chapter markers
- **Ebook support** — converts any format to EPUB via Calibre's ebook-convert
- **Docker-aware** — works with calibre-web running in a container
- **Cross-platform** — macOS (fswatch) and Linux (inotifywait)
- **Crash-safe** — SQLite state store, source files only deleted after confirmed import

---

## Requirements

| Dependency | macOS | Linux |
|------------|-------|-------|
| **Python 3.10+** | `brew install python` or [python.org](https://python.org) | `sudo apt install python3` |
| **Calibre** (`calibredb`) | [calibre-ebook.com/download_osx](https://calibre-ebook.com/download_osx) | [calibre-ebook.com/download_linux](https://calibre-ebook.com/download_linux) |
| **ffmpeg** | `brew install ffmpeg` | `sudo apt install ffmpeg` |
| **fswatch** (macOS only) | `brew install fswatch` | — |
| **inotify-tools** (Linux only) | — | `sudo apt install inotify-tools` |

---

## Installation

### Automatic (recommended)

Clone the repo and run the installer — it handles dependencies, installs the package, and walks you through config creation:

```bash
git clone https://github.com/markbyrne/libris.git
cd libris
bash install.sh
```

The installer will:
- Check and install missing system dependencies (it asks before using `sudo`)
- Install the `libris` Python package (from the local checkout, or the latest [PyPI release](https://pypi.org/project/pylibris/) when run standalone)
- Create a config file at `~/.config/libris/config.yaml`
- Offer to add `LIBRIS_CONFIG` and `~/.local/bin` (PATH) to your shell profile — both prompts can be declined
- Optionally install a daemon service (LaunchAgent on macOS, systemd on Linux)
- Run `libris check-config` to verify everything works

The installer defaults to `~/libris/` as the root for all watch folders. Each directory prompt includes a description of its purpose so you know what you're configuring.

### Manual installation

From PyPI (the distribution is named `pylibris`; the installed command is `libris`):

```bash
pip install pylibris
```

Or from source:

```bash
git clone https://github.com/markbyrne/libris.git
cd libris
pip install .
```

For development (editable install with test dependencies):
```bash
pip install -e ".[dev,web]"
```
The `web` extra is required to run the full test suite (the web UI and directive-API tests skip themselves when `fastapi` isn't installed).

---

## Quick Start (manual setup)

**1. Create your config**

```bash
mkdir -p ~/.config/libris
cp config.example.yaml ~/.config/libris/config.yaml
```

Open `~/.config/libris/config.yaml` and set the paths for your setup:

```yaml
watcher:
  incoming_dir: ~/libris/incoming     # drop new downloads here; Libris watches this folder
  scan_interval_hours: 1.0            # re-scan on startup + every N hours

paths:
  staging_dir: ~/libris/staging       # temporary workspace for conversion
  review_dir:  ~/libris/review        # low-confidence matches land here for your approval
  failed_dir:  ~/libris/failed        # processing errors; inspect then recover or delete
  state_db:    ~/libris/libris.db     # Libris's own SQLite database (NOT the Calibre database)

calibre:
  mode: local
  library_db_path: ~/Calibre Library  # must match Calibre Preferences → Libraries

metadata:
  confidence_threshold: 0.75

ntfy:
  topic: my-libris-alerts             # optional — for push notifications
  enabled: false
```

Create the directories:

```bash
mkdir -p ~/libris/{incoming,staging,staging/pending,review,failed}
```

**2. Point your shell at the config**

Add this line to your `~/.zshrc` (or `~/.bashrc`) so `libris` commands work from any directory:

```bash
export LIBRIS_CONFIG=~/.config/libris/config.yaml
```

Then reload your shell:

```bash
source ~/.zshrc   # or source ~/.bashrc
```

**3. Verify the setup**

```bash
libris check-config
```

This validates the config, prints all resolved settings, and sends a test ntfy notification if configured.

**4. Start the daemon**

```bash
libris run
```

Drop any ebook or audiobook into `incoming_dir` and it will be imported automatically.

---

## Configuration

### Config file discovery

Libris resolves the config file in this order — the first match wins:

1. `--config <path>` CLI flag
2. `LIBRIS_CONFIG` environment variable ← **recommended; set in your shell profile**
3. `config.local.yaml` in the current directory
4. `config.yaml` in the current directory
5. `~/.config/libris/config.yaml`

If no config is found, the error message lists all locations tried and shows how to fix it.

#### Per-project override

If you prefer to keep the config alongside the repo (git-ignored):

```bash
cp config.example.yaml config.local.yaml
# Edit config.local.yaml — only used when running from this directory
```

### Minimal config (local Calibre)

```yaml
watcher:
  incoming_dir: ~/books/incoming
  scan_interval_hours: 1.0   # re-scan on startup + every N hours (0 to disable)

paths:
  staging_dir: ~/books/staging
  review_dir: ~/books/review
  failed_dir: ~/books/failed
  state_db: ~/books/libris.db

calibre:
  mode: local
  library_db_path: ~/Calibre Library   # where metadata.db lives

metadata:
  confidence_threshold: 0.75

output:
  preferred_ebook_format: epub   # epub | mobi
  ebook_format_policy: preferred # preferred | all (see below)

ntfy:
  topic: my-libris-alerts
  enabled: true
```

> **Note:** The legacy `library_path` key is still accepted and maps to `library_db_path`. Existing configs do not need to change.

### calibre-web: Separate Book Files from Library

If you use calibre-web's **"Separate Book Files from Library"** setting (where `metadata.db` is on a fast local disk but book files are on a NAS or external drive), configure both paths:

```yaml
calibre:
  mode: local
  library_db_path: /srv/calibre-db      # "Location of Calibre Database" in calibre-web
  book_file_path:  /mnt/nas/books       # "Separate Book Files from Library" in calibre-web
```

After each import Libris automatically moves all files in the book's directory from `library_db_path/Author/Title (id)/` into the matching path under `book_file_path` — this includes the format file (`.epub`, `.m4b`), `cover.jpg`, and `metadata.opf`. calibre-web needs all three in `book_file_path` to display covers and serve downloads correctly. If `book_file_path` is not set, behaviour is identical to the classic single-path setup.

### Coexisting with calibre-web (important — avoiding database corruption)

Calibre **does not support two programs using one `metadata.db` at the same time**. calibre-web keeps the database open continuously, so every Libris import (which writes via `calibredb`) accumulates in SQLite's write-ahead log *underneath* calibre-web's long-lived connection. Over days this both prevents WAL checkpointing (the WAL grows without bound) and can desync calibre-web's view into a `database disk image is malformed` error — even though the data on disk is still intact.

Three layers of protection, in order of value:

1. **`reconnect_url`** — set this and Libris pings calibre-web's `/reconnect` endpoint after every import and removal, making calibre-web drop and reopen its connection instead of going stale:

   ```yaml
   calibre:
     reconnect_url: http://192.168.1.10:8083/reconnect
   ```

   The endpoint requires calibre-web to be **started with the `-r` flag** ([calibre-web#2336](https://github.com/janeczku/calibre-web/issues/2336)) — without it the endpoint returns 404 and Libris logs a warning. Note the stock [linuxserver.io calibre-web image](https://github.com/linuxserver/docker-calibre-web/issues/182) provides no way to pass CLI flags; images such as calibre-web-automated enable it by default. The ping is best-effort: if calibre-web is down or the endpoint is missing, the import still succeeds.

2. **Restart calibre-web nightly** (cron: `0 4 * * * docker restart calibre-web`) — caps the connection age and forces a clean WAL checkpoint.

3. **Back up `metadata.db` nightly** using Python's `sqlite3` backup API (safe against a live database, unlike `cp`).

If you ever hit `database disk image is malformed`: **copy `metadata.db`, `metadata.db-wal`, and `metadata.db-shm` somewhere safe *before* stopping or restarting anything.** The WAL usually still holds your recent imports intact, but a stopping process can checkpoint its corrupted view into the main file and destroy a recoverable database. Then verify with `sqlite3 "file:copy.db?immutable=1&mode=ro" "PRAGMA integrity_check;"`.

### Docker config (e.g. calibre-web in a container)

```yaml
calibre:
  mode: docker
  docker_container: calibre-web
  path_map:
    /media/books: /books          # host path: container path
```

### Google Books API key

Libris works without an API key (unauthenticated, daily quota per IP), but adding a free API key is recommended for regular use (1,000 requests/day, more reliable).

```yaml
metadata:
  google_books_api_key: YOUR_KEY_HERE
```

To get a key:
1. Visit https://console.developers.google.com/
2. Create or select a project
3. Go to **APIs & Services** → **Enable APIs & Services**, search for "Books API" and enable it
4. Go to **Credentials** → **Create credentials** → **API key**

If you hit a rate limit during `libris rematch`, it will prompt you to add a key and save it to your config automatically.

> **Security note:** The API key is sent as an `X-goog-api-key` request header rather than a URL query parameter. This prevents the key from appearing in any URL-logging middleware or log files.

### Ebook format policy

`output.ebook_format_policy` controls how files that aren't already in the preferred format are handled:

| Policy | Behaviour |
|--------|-----------|
| `preferred` (default) | Convert to `preferred_ebook_format`, import the converted file, delete the original source |
| `all` | Import the file in whatever format it arrived — no conversion; Calibre stores the native format |

Examples:

```yaml
output:
  preferred_ebook_format: epub

  # Convert everything to EPUB, delete the source PDF/MOBI/etc.
  ebook_format_policy: preferred

  # OR: import as-is — PDF stays PDF, MOBI stays MOBI
  # ebook_format_policy: all
```

Environment variable: `LIBRIS_OUTPUT_EBOOK_FORMAT_POLICY`

`libris check-config` shows the resolved setting:

```
  Ebook format:   epub  (policy: preferred)
```

### Multi-part audiobook timeout

Parts are held in staging until the complete set is received. If parts are missing after a configurable timeout, they are escalated to the review queue:

### Disk space requirements for M4B combining

Before combining multi-part audiobooks, Libris checks that there is enough free space to complete the operation. Two intermediate files (each approximately the combined size of all parts) are written to a temp directory, and the final file is copied to the output location.

**Automatic temp-dir fallback:** Libris first tries the system temp dir (`/tmp`). If `/tmp` is tight, it automatically falls back to using the output directory's filesystem as the temp location — no configuration needed for most setups.

Minimum space requirements:
- **Temp dir** (`/tmp` or `$TMPDIR`): 2.1× the total size of all parts
- **Output dir**: 1.1× the total size of all parts
- **Output dir (when used as temp fallback)**: 3.2× the total size of all parts

If neither location has enough space, a clear error is shown before ffmpeg starts:

```
ConversionError: Insufficient disk space: need ~2.2 GB free in /tmp (temp dir), have 1.7 GB.
Set TMPDIR to a directory with more space, e.g.: TMPDIR=/mnt/media libris combine-parts …
```

To point Libris at a specific temp directory:

```bash
TMPDIR=/mnt/media/tmp libris process   # or any other libris command
```

```yaml
multipart:
  timeout_hours: 48   # default; set to 0 to disable automatic escalation
```

### Directive API — external tools can pre-register a match

An external tool (e.g. [Librarr](https://github.com/markbyrne/librarr)) can call Libris's HTTP API to **pre-register the correct metadata match** for a file it's about to drop into `incoming_dir`. When that file arrives, Libris looks up the directive by filename and, if found, imports with the directed metadata instead of running its own Google Books / OpenLibrary / DuckDuckGo lookups. If no directive exists, behaviour is unchanged — Libris falls back to its normal matching.

The API is **disabled by default** (fails closed): both `api.enabled: true` **and** a non-empty `api.api_key` are required before any request is served. Every request must carry a matching `X-Api-Key` header.

```yaml
api:
  enabled: true
  api_key: "a-long-random-shared-secret"
```

Or via environment variables:

```bash
LIBRIS_API_ENABLED=true
LIBRIS_API_KEY=a-long-random-shared-secret
```

**`GET /api/v1/ping`** — health check (used by Librarr's "Test connection" button):

```bash
curl -H "X-Api-Key: a-long-random-shared-secret" http://localhost:8000/api/v1/ping
# {"ok": true, "version": "0.3.18.dev3"}
```

**`GET /api/v1/config`** — lets Librarr auto-detect the paths it needs (`incoming_dir`, `state_db`) instead of the user hand-typing them from another machine:

```bash
curl -H "X-Api-Key: a-long-random-shared-secret" http://localhost:8000/api/v1/config
# {"incoming_dir": "/home/user/incoming", "state_db": "/home/user/.libris/state.db",
#  "review_dir": "/home/user/review", "version": "0.4.0"}
```

**`POST /api/v1/directives`** — register a match for a filename that will (or already did) land in `incoming_dir`. `filename` must be a bare basename (no path separators) and matches the file's *original* incoming name — Libris keys the lookup on that name even if conversion or multi-part staging later renames the on-disk file:

```bash
curl -X POST http://localhost:8000/api/v1/directives \
  -H "X-Api-Key: a-long-random-shared-secret" \
  -H "Content-Type: application/json" \
  -d '{
        "filename": "dune.epub",
        "title": "Dune",
        "author": "Frank Herbert",
        "isbn": "9780441013593",
        "year": 1965,
        "media_type": "ebook",
        "source": "librarr"
      }'
# {"id": "…", "status": "registered"}
```

A second directive posted for the same filename supersedes the first (newest wins). Unconsumed directives older than 48 hours are purged automatically during the daemon's periodic incoming-directory scan.

### Environment variable overrides

Any config value can be overridden with a `LIBRIS_` prefixed environment variable:

```bash
LIBRIS_CALIBRE_MODE=docker
LIBRIS_METADATA_CONFIDENCE_THRESHOLD=0.80
LIBRIS_NTFY_TOPIC=my-topic
LIBRIS_MULTIPART_TIMEOUT_HOURS=24
LIBRIS_API_ENABLED=true
LIBRIS_API_KEY=a-long-random-shared-secret
```

---

## Usage

### `check-config` — validate your setup

```bash
libris check-config
```

Prints all resolved config values — including Google Books API key status (enabled/disabled, key never printed) and `book_file_path` when in split-library mode — then checks every configured directory for reachability and (if an API key is set) probes the Google Books API. Sends a test ntfy notification if configured.

---

### `import-one` — process a single file

```bash
libris import-one /path/to/book.epub
```

Useful for testing without running the daemon. Output:

```
  ✅  Project Hail Mary.epub
  ──────────────────────────────────────────────────
  Result:  imported
  Title:   Project Hail Mary
  Author:  Andy Weir
  Score:   0.91
```

If the score is below the confidence threshold the file is moved to `review/` instead:

```
  🔍  some-obscure-title.epub
  ──────────────────────────────────────────────────
  Result:  review
  Title:   A Similar Title
  Author:  Unknown Author
  Score:   0.51
```

---

### `import-dir` — import a directory of audio files

```bash
libris import-dir /path/to/audiobook-directory
libris import-dir /path/to/audiobook-directory --combine-all
```

Imports all audio files in a directory as an audiobook.

**Without `--combine-all`** (default): each file is dispatched through the normal pipeline individually. Files whose names contain recognised part markers are grouped and combined; files without markers are imported as standalone books.

**With `--combine-all`**: every audio file in the directory is treated as a sequential part of *one* audiobook, regardless of filenames. Parts are assigned numbers in sorted filename order, combined into a single M4B with chapter markers, and imported as one book. Use this when the files have non-standard part notation that the automatic detector doesn't recognise, for example:

```
D.J. MacHale-Book01-The Merchant of Death/
  Book01-Merchant of Death-Disc01-001.mp3   ← part 1 of 100
  Book01-Merchant of Death-Disc01-002.mp3   ← part 2 of 100
  ...
  Book01-Merchant of Death-Disc10-010.mp3   ← part 100 of 100
```

```bash
libris import-dir "D.J. MacHale-Book01-The Merchant of Death" --combine-all
```

Output follows the same format as `import-one`.

---

### `run` — start the daemon

```bash
libris run
```

Watches `incoming_dir` continuously and processes files as they arrive. Drop any ebook or audiobook — or an entire directory tree — into the folder and it will be imported automatically. Ctrl-C to stop.

On startup, the incoming folder is scanned immediately so any files that arrived while the daemon was offline are processed without waiting. A background thread re-scans the folder periodically (default every hour) as an additional safety net.

Configure the scan interval in your config:

```yaml
watcher:
  scan_interval_hours: 1.0   # set to 0 to disable the periodic scan
```

`libris check-config` shows the resolved scan setting:

```
  Folder scan:    every 1h
```

(The incoming folder is always scanned once on startup regardless of this setting.)

---

### `list-review` — see what needs attention

```bash
libris list-review
```

```
  3 file(s) in review
  ──────────────────────────────────────────────────

  [1]  Caliban and the Witch.epub
        Matched:  Caliban and the Witch  by anarchivists
        Score:    0.52
        Info:     2004 · Penguin · ISBN 9781570270598
        Cover:    libris show-cover --id 1
        Path:     "/Users/you/books/review/Caliban and the Witch.epub"

  [2]  Brisingr.m4b
        ⚠  Duplicate: already in Calibre (IDs: 7)
           To import anyway: libris review-accept --id 2 --overwrite
           To delete:        libris review-discard --id 2
        Matched:  Brisingr  by Christopher Paolini
        Score:    0.94
        Path:     "/Users/you/books/review/Brisingr.m4b"

  [3]  unknown-audiobook.m4b
        [!] No match found
           Try:  libris rematch --id 3
        Path:     "/Users/you/books/review/unknown-audiobook.m4b"

  ──────────────────────────────────────────────────
  Accept by ID:    libris review-accept --id <N>
  Accept all:      libris review-accept --accept-all
  Accept by path:  libris review-accept "<path>"
  Fix bad match:   libris rematch --id <N>
  Preview cover:   libris show-cover --id <N>
  Discard:         libris review-discard --id <N>
  Discard dupes:   libris review-discard --duplicates
```

Items showing `[!] No match found` could not be matched by either API. Run `libris rematch --id <N>` to search manually. `review-accept` is blocked until a match is found.

Files that have been moved out of `review/` manually are automatically excluded — only files that still exist on disk are shown.

If there are files in PENDING or FAILED state, a warning is shown at the bottom of the review queue:

```
  ⚠   2 file(s) in PENDING state — run 'libris list-pending' to see them.
  ⚠   1 file(s) in FAILED state — run 'libris list-failed' to see them.
```

These counts are also shown when the review queue refreshes automatically after a `review-accept` or `rematch`.

---

### `show-cover` — preview a cover image

```bash
libris show-cover --id 1
```

Opens the matched cover image in your default browser. After opening, the full match details are re-displayed so you have context alongside the image.

```
  ✅  Cover opened in browser

  ──────────────────────────────────────────────────
  [1]  Eldest.m4b
        Matched:  Eldest  by Christopher Paolini
        Score:    0.91
        Info:     2005 · Knopf · ISBN 9780375826702
  ──────────────────────────────────────────────────
  Accept:      libris review-accept --id 1
  Fix match:   libris rematch --id 1
```

---

### `review-accept` — force-import a reviewed file

Accepts the current metadata match and imports the file into Calibre, bypassing the confidence threshold. Uses cached metadata — no API call required.

After a successful accept, the updated review queue is printed automatically — including the full action-hints footer — so you can see the new IDs and available commands without re-running `list-review`.

```bash
# By review queue ID (from list-review)
libris review-accept --id 1

# All files at once
libris review-accept --accept-all

# By path (quote paths with spaces)
libris review-accept "/books/review/Caliban and the Witch.epub"
```

#### Format merging

If a file arrives that matches a book already in Calibre **but in a different ebook format**, it is automatically added as a new format to the existing record — no review, no duplicate warning. A single Calibre book record will then hold both files. The existing record's cover and metadata are also refreshed with the freshly resolved API data.

```
incoming/ Eragon.epub   →  finds Eragon.mobi already in Calibre
                        →  calibredb add_format 42 Eragon.epub  ✅
                        →  Calibre book 42 now has: EPUB + MOBI
```

This applies whether the files arrive in the same directory drop or at different times, and regardless of `duplicate_action` — format merging always runs.

> **Ebook + audiobook of the same title** — When an M4B audiobook arrives and an EPUB of the same title already exists, Libris creates a **separate Calibre entry** for the audiobook rather than trying to merge them. `calibredb add_format` does not support audio formats, and ebook/audiobook records are better kept distinct in the library.

#### Duplicates stay in the review queue

When Libris detects the same format already exists in Calibre, the file **stays in the review queue** rather than failing. `list-review` marks it with a yellow `[!]` tag so you can see it immediately:

```
  [2] [!] Blood River.epub
        ⚠  Duplicate: already in Calibre as EPUB (ID: 21)
           Accept (overwrite): libris review-accept --id 2 --overwrite
           Discard:            libris review-discard --id 2
```

If you run `review-accept` on a file that turns out to be a duplicate (e.g. it was in review for low confidence, not because of duplication), Libris prompts you inline instead of failing:

```
  ⚠   Blood River.epub
       Duplicate: already in Calibre as EPUB (ID: 21)

       [o]  Overwrite — replace the existing Calibre entry
       [d]  Discard   — delete this file from the review queue
       [s]  Skip      — leave in review (use --overwrite later)
       [r]  Rematch   — find a different book match

       Choice [s]:
```

The same prompt appears during `rematch` if the candidate you pick is already in Calibre — you can overwrite, discard, or `[r]` go back and try a different match without losing your place.

With `duplicate_action: import` in your config, the same smart merge happens automatically — same format is replaced in-place, different format is added to the existing record. No second Calibre entry is ever created.

#### Near-match detection

When you run `review-accept`, Libris also checks for **near-matches** — books already in Calibre that are similar but not an exact title match (e.g. "Project Hail Mary: A Novel" vs "Project Hail Mary"). If a near-match is found, you're prompted with four options:

```
  ⚠  Near-match found in Calibre library (94% similar):
     "Project Hail Mary: A Novel" by Andy Weir  [ID 42]  formats: EPUB

  [m]  Merge    — add this format to the existing book
  [o]  Overwrite — replace the existing format
  [d]  Discard  — delete this file, keep existing book
  [n]  New entry — import as a separate book
  Choice [m/o/d/n]:
```

To batch-delete duplicates or chaff:

```bash
libris review-discard --duplicates    # delete all [!] items
libris review-discard --chaff         # delete known-clutter files (Read Me!, NFO, etc.)
libris review-discard --id 2         # delete one
libris review-discard --stale        # remove DB records where file is already gone
```

---

### `rematch` — interactively fix a bad metadata match

When the auto-matched title or author is wrong, `rematch` lets you search the APIs yourself and pick the right result.

After importing or quitting, the updated review queue is printed automatically so you can see the new IDs.

```bash
libris rematch --id 1
```

You'll see the current match and a query prompt. The most effective format is `Title by Author`:

```
  Query [Caliban and the Witch]: Caliban and the Witch by Silvia Federici

  Searching…

    Google Books   3 result(s)
    OpenLibrary    2 result(s)

  [1]  Caliban and the Witch
        Silvia Federici  ·  Google Books  ·  score 0.94
        Penguin Books  ·  2004  ·  ISBN 9781570270598
        Breakdown:  isbn 0.00/0.40 · title 0.28/0.30 · author 0.20/0.20 · year 0.05/0.10 · agreement +0.08

  [2]  Witches, Witch-Hunting, and Women
        Silvia Federici  ·  OpenLibrary  ·  score 0.61
        ...

  ──────────────────────────────────────────────────
  [1/2/3] import    [r] refine query    [q] quit

  Choice [1]: 1

  ✅  Caliban and the Witch
      Author:  Silvia Federici
      Score:   0.94 (manually selected)
```

**Tips:**
- `Title by Author` routes the author to the correct API field — much better results than a fused string
- Use an ISBN if you have it: `9780141439518`
- `/api google` or `/api openlibrary` to restrict to one source; `/api all` to restore both
- `/clear` to redraw the screen

**No results?** If both APIs return nothing, Libris automatically queries the [DuckDuckGo Instant Answer API](https://duckduckgo.com/api) for author and ISBN hints, then retries. (Results from this fallback include data provided by DuckDuckGo.) In `rematch`, suggested search refinements are shown:

```
  Web search suggests:
    Author:  Silvia Federici
    Try:     Caliban and the Witch by Silvia Federici
```

**Rate limits:** If Google Books is rate limited, the prompt offers:
- `[w]` wait the required time and retry automatically
- `[k]` add a Google Books API key (free, walks you through setup, saves to config)
- `[s]` skip Google Books and search OpenLibrary only

---

### `list-failed` — inspect failed files

Shows all files that failed processing, why they failed, and how long they have been there.

```bash
libris list-failed
```

```
  3 file(s) in failed state
  ──────────────────────────────────────────────────

  [1]  Read Me!.epub  (2h 14m ago)
        Error:  chaff detected — filename matches known non-book pattern

  [2]  corrupted.epub  (15m ago)
        Error:  calibredb exited with rc=1: Could not read ebook metadata

  [3]  mystery-novel.epub  (1d 3h ago)
        Error:  API lookup timed out after 3 retries

  ──────────────────────────────────────────────────
  Recover by ID:   libris recover --id <N>
  Recover all:     libris recover --all
  Remove by ID:    libris remove --id <N>
  Remove chaff:    libris remove --chaff
  Remove all:      libris remove --all
```

Stale records whose file is already gone from disk are shown dimmed with a suggested `libris remove --id <N>` hint.

---

### `recover` — move failed files back to review

Files that fail processing (e.g. due to a network error, rate limit, or chaff detection) are moved to `failed/`. Use `recover` to return them to `review/` so they can be rematched and imported.

Run `libris list-failed` first to see the current failed queue and IDs.

**Chaff detection:** Files with known non-book filenames (`Read Me!.epub`, `Downloaded from….epub`, `*.txt`, `*.nfo`, etc.) are automatically rejected and moved to `failed/` before any API call is made. If a file was incorrectly flagged, use `libris recover --id N` to move it back to review.

```bash
# Recover a specific file (use list-failed to find IDs)
libris recover --id 1

# Recover everything
libris recover --all
```

After recovery, files appear in `libris list-review` and can be fixed with `libris rematch`. The updated failed queue is reprinted automatically so you can see what still needs attention.

#### Deleting unrecoverable files

If a file is gone from disk (e.g. already deleted manually) but is still listed as failed, use `--delete` to clean up the stale record:

```bash
# Delete all failed records whose file is already missing
libris recover --delete

# Delete a specific stale record (with or without a file present)
libris recover --delete --id 1

# Delete all failed records (removes files from disk if still present)
libris recover --delete --all
```

This marks the record as resolved in the state database without trying to move anything.

---

### `remove` — permanently delete failed files

Permanently deletes failed file(s) from disk **and** removes their database records. Unlike `recover`, this is destructive — use it for files you are certain you do not want.

```bash
# Remove a single failed file by list-failed ID
libris remove --id 1

# Remove every file in the failed queue
libris remove --all

# Remove all failed files that match known chaff patterns (README, NFO, images, etc.)
libris remove --chaff
```

Each deleted filename is echoed so you have a record of what was removed. A summary count is printed at the end, followed by the updated failed queue so you can see what still needs attention.

---

### `prune` — remove stale database records

When files are deleted manually from `failed/` or `staging/pending/` (outside of Libris), the database still holds records for them. `prune` finds and removes those orphaned entries.

```bash
libris prune --dry-run   # preview what would be removed
libris prune             # apply
```

Both FAILED and PENDING_PARTS records are scanned. Only records whose `current_path` no longer exists on disk are removed.

---

### `list-pending` — check multi-part audiobooks in progress

Shows all multi-part audiobooks currently waiting for their sibling parts. Once all parts have arrived they are combined automatically.

```bash
libris list-pending
```

```
  2 pending group(s)
  ──────────────────────────────────────────────────

  [1]  inheritance cycle 3 brisingr
        Parts:    2 of 3 received  (missing: 3)
        Age:      2h 14m  (times out in 45h 46m)
        ✓ part 1  Inheritance Cycle 3 - Brisingr (part 1 of 3).m4b
        ✓ part 2  Inheritance Cycle 3 - Brisingr (part 2 of 3).m4b

  [2]  name of the wind
        Parts:    1 of 2 received  (missing: 2)
        Age:      0m  (times out in 48h 0m)
        ✓ part 1  Name of the Wind Disc 1 of 2.m4b

  ──────────────────────────────────────────────────
  Force-combine:  libris combine-parts --id <N>
  Combine all:    libris combine-parts --all
  Discard group:  libris pending-discard --id <N>
  Merge groups:   libris pair-pending --id1 <N> --id2 <M>
```

If a group times out before all parts arrive, the received parts are moved to `review/` with a note. They remain importable via `combine-parts`.

---

### `pending-discard` — move a pending group back to review

If files were incorrectly grouped as multi-part or you want to restart the process, `pending-discard` moves every file in the group back to `review/` as individual items.

Part markers are stripped from the filenames on the way out so they look clean in the review queue.

```bash
# Find the group ID
libris list-pending

# Move group [1] back to review/
libris pending-discard --id 1
```

After discarding, files appear in `libris list-review` and can be rematched or manually re-grouped with `mark-as-part`.

---

### `pair-pending` — merge two pending groups into one

If two groups actually belong to the same audiobook (e.g. parts arrived under different group keys), merge them with `pair-pending`.  Group 2 is absorbed into group 1, parts are re-sequenced, and `total_parts` is updated.  If the merged group is now complete, combine + import is triggered automatically.

```bash
# Find the two group IDs
libris list-pending

# Merge group [2] into group [1]
libris pair-pending --id1 1 --id2 2
```

If all parts are present after the merge, the set is combined and imported immediately — the same behaviour as `combine-parts --id N`.  If parts are still missing, a warning is shown and you can trigger manually when ready.

---

### `mark-as-part` — manually flag a review-queue file as part of a set

If a multi-part audiobook arrived in `review/` (e.g. because the filename had no part marker), you can manually register each file as a numbered part and trigger auto-combine:

```bash
# Flag item [1] in review/ as part 1 of 2
libris mark-as-part --id 1 --part 1 --total 2

# Flag item [2] as part 2 of 2 — triggers automatic combine + import
libris mark-as-part --id 2 --part 2 --total 2

# Override the group name (default: derived from filename)
libris mark-as-part --id 3 --part 1 --group "Eragon"
```

The `list-review` command hints this option when audiobook files are in the queue.  Once all parts are staged, the set is combined and imported automatically — the same as if the part markers had been detected from the filename.

---

### `combine-parts` — force-import a partial set

Combine and import a pending group immediately, without waiting for missing parts.

```bash
# Combine a specific group
libris combine-parts --id 1

# Combine all pending groups with whatever parts are available
libris combine-parts --all
```

Useful when you know the remaining parts won't arrive, or when you want to import a two-part book that was only partially downloaded.

---

### `search` — search your Calibre library

```bash
libris search "Caliban"
libris search "authors:Federici"
libris search "title:Dune"
```

Uses the library path from your config — no `--with-library` flag needed. Book IDs shown here can be used with `revert-import`.

---

### `revert-import` — undo an import

Exports a book from Calibre, removes it from the library, and returns it to `review/` for re-processing.

```bash
# By Calibre book ID
libris revert-import 42

# Find the ID first, then revert
libris revert-import --search "Caliban"
```

---

### `clean-library` — deduplicate, fix Unknown books, reconcile DB against disk

Scans the Calibre library in four passes:

1. **Dedup** — groups books by title + author; for each group with more than one entry, merges all formats into the first (lowest-ID) book and removes the extras.

2. **Unknown** — finds books whose title or every author is `Unknown`; exports the file(s) and drops them into `incoming/` so the normal pipeline can re-match and import them with correct metadata.

3. **Missing files** — finds Calibre entries whose book files no longer exist on disk (e.g. files moved or deleted by hand) and removes the dead entries from the database. This pass always asks for confirmation before removing anything; pass `--yes` to skip the prompt. Entries that still have at least one format on disk are never removed — a missing format is just reported.

4. **Orphan files** — finds book files in the library tree that no Calibre entry points to and moves them to `review/`, where `libris list-review` / `libris rematch` / `libris review-accept` handle re-import. Calibre's internal files (`metadata.db`, covers, `.caltrash`) are ignored.

Passes 3 and 4 require `calibre.mode: local` — in docker mode the library paths are inside the container and cannot be checked from the host. In split-library mode (`book_file_path` set), files are checked at the book-files location, and a file stranded at the metadata.db location (e.g. by a crash between add and relocation) still counts as present.

```bash
# Preview what will change (safe — no modifications)
libris clean-library --dry-run

# Apply (prompts before removing missing-file entries)
libris clean-library

# Apply without the pass-3 confirmation prompt
libris clean-library --yes
libris run              # re-imports anything moved to incoming/
```

Example output:

```
  14 book(s) in Calibre library

  ── Pass 1: Dedup ──
    remove duplicate book 18 ('Brisingr' by Christopher Paolini) [M4B]
    remove duplicate book 19 ('Eldest' by Christopher Paolini) [M4B]
    remove duplicate book 20 ('Eragon' by Christopher Paolini) [M4B]

  ── Pass 2: Unknown metadata ──
    re-queue book 14 (EPUB)
       → incoming/Unknown.epub

  1 book(s) moved to incoming/. Run 'libris run' to re-import them.

  ── Pass 3: Missing files ──
    book 21 ('Old Title' by Some Author) — no files on disk

  Remove 1 Calibre entr(y/ies) with no files on disk? [y/N]: y
       removed book 21

  ── Pass 4: Orphan files ──
    orphan Unknown/Stray Book (37)/Stray Book.m4b → review/Stray Book.m4b
  1 orphan file(s) moved to review/. Run 'libris list-review' to triage, 'libris rematch' to match them.
```

---

### `get-covers` — backfill missing cover.jpg files

Every import saves the matched cover as `cover.jpg` in the book's library directory (this is independent of `output.embed_cover_art`, which controls only the art embedded inside the audio file). For books imported before this behaviour — or whose cover download failed — `get-covers` backfills them:

```bash
# See which books are missing covers (no changes)
libris get-covers --dry-run

# Fetch and save them
libris get-covers
```

For each book missing a `cover.jpg`, the cover is fetched from the URL recorded when the book was matched at import time, falling back to a fresh Google Books / OpenLibrary lookup by title and author. Covers are saved through `calibredb` so the database `has_cover` flag and the directory stay in sync, including split-library relocation. Requires `calibre.mode: local`.

Book locations come from Calibre's database (`books.path`), not from `calibredb list` output — `calibredb list --fields formats` only reports a format when the file exists under the *metadata.db* directory, which in split-library mode is true for no properly-relocated book. (Before v0.3.13 this silently hid split-mode libraries from `get-covers` and `clean-library`'s reconciliation passes; before v0.3.14 it also made cover relocation, the rename sync, and split-mode export silently no-op for relocated books.)

```
  3 of 93 book(s) missing cover.jpg:

  ✓ book 41 ('Project Hail Mary' by Andy Weir)
  ✓ book 87 ('Mort' by Terry Pratchett)
  ⚠ book 90 ('Obscure Self-Published Thing' by Unknown) — no cover found

  2 cover(s) fetched, 1 not found
```

If a cover is found but calibredb cannot write it (`✗ ... could not save the cover`), check the ownership and permissions of the book's directory — containerized calibre-web instances can chown library directories to a container-mapped uid that the host user cannot write into. The command exits non-zero when any save fails.

---

### `migrate-libris` — move Libris dirs and DB to a new root

Moves all of Libris's operational directories (`incoming/`, `staging/`, `review/`, `failed/`) and the state database to a new location, then updates the config file in-place. Existing files at the destination are preserved (merge-safe).

```bash
# Preview what would move (no changes)
libris migrate-libris ~/new-libris --dry-run

# Execute the migration
libris migrate-libris ~/new-libris

# Verify the config updated correctly
libris check-config
```

The command:
1. Detects the common root of all Libris directories (e.g. `~/libris/`) and preserves the relative structure under the new root
2. Prompts for confirmation before making any changes
3. Copies each directory with merge semantics (`dirs_exist_ok=True`), then removes the originals
4. Moves the state DB file
5. Rewrites the affected config keys in-place (preserving inline YAML comments)

---

### `migrate-library` — move the Calibre library

Moves Calibre library files to a new location and updates the config. Supports three modes:

| Mode | What moves | Config change |
|------|-----------|---------------|
| (default) | Everything (`metadata.db` + book files) | `library_db_path → to_path` |
| `--books-only` | Book files only; `metadata.db` stays | enables split-library mode |
| `--db-only` | `metadata.db` only; book files stay | `library_db_path → to_path` |

**Primary use case — move book files to an external drive:**

```bash
# Preview
libris migrate-library /calibre-db /Volumes/ExtDrive/books --books-only --dry-run

# Execute
libris migrate-library /calibre-db /Volumes/ExtDrive/books --books-only

# Verify — config now shows split-library mode
libris check-config
```

After `--books-only`, the config transitions from a flat `library_path` to split mode:

```yaml
calibre:
  mode: local
  library_db_path: /calibre-db       # renamed from library_path; metadata.db is here
  book_file_path: /Volumes/ExtDrive/books   # physical EPUB/M4B files
```

This matches calibre-web's **"Separate Book Files from Library"** setting and pairs directly with Libris's split-library support (Issue #18). After migration, new imports are automatically placed under `book_file_path`.

**Conflict resolution (`--books-only`):** When files already exist at the destination, you are prompted to choose:

| Option | Behaviour |
|--------|-----------|
| `skip` | Leave both source and destination unchanged (default) |
| `overwrite` | Replace the destination file with the source |
| `remove` | Delete the source file, keep the destination — useful when the dest already has the correct copy and you want to clean up the source |
| `abort` | Stop immediately, no files moved |

**Full library move:**

```bash
libris migrate-library ~/calibre ~/new-location/calibre
```

---

### `reset` — unstick processing records

If Libris crashes mid-import, files can be left in `PROCESSING` state and skipped on re-run. This command resets them to `INCOMING` so they'll be processed next time.

```bash
libris reset
```

---

## Multi-part audiobooks

Libris detects split audiobooks by filename pattern and holds them in staging until the complete set arrives, then combines them into a single M4B with chapter markers before importing.

### Recognised filename patterns

| Filename | Detected as |
|----------|-------------|
| `Brisingr (part 1 of 3).m4b` | Part 1 of 3 |
| `Brisingr (part 1.3).m4b` | Part 1 of 3 |
| `Brisingr (part 1/3).m4b` | Part 1 of 3 |
| `Name of the Wind Disc 1 of 2.m4b` | Part 1 of 2 |
| `Eragon Part 1.m4b` | Part 1 (total unknown) |
| `Eragon (1 of 2).mp3` | Part 1 of 2 (no keyword needed) |
| `Eragon (1/2).mp3` | Part 1 of 2 (slash form) |
| `Eragon (1).mp3` | Part 1 (total unknown) |
| `Book01-Merchant of Death-Disc01-001.mp3` | Part 1 (compact DiscNN form) |
| `Book01-Merchant of Death-CD03.mp3` | Part 3 (compact CDnn form) |
| `Title-01-46.m4b` | Part 1 of 46 (bare trailing pair) |

The `part`/`disc`/`cd` keyword is optional — bare sequential numbers in parentheses at the end of a filename are also recognised. This covers the common convention of downloaders naming files `Book Title (1).mp3`, `Book Title (2).mp3`, etc.

The bare trailing pair form (`Title-NN-NN`) is only treated as a part marker when it is plausible: the first number must be between 1 and the second, and the second must be at least 2. Implausible pairs (`Title-46-01`), trailing dates (`Show-2024-12-25`), and pairs directly preceded by a digit (`Catch-22-01-46`) are deliberately not matched — a missed part marker just means the file lands in `review/` where `libris mark-as-part` can fix it, whereas a false positive would hold a standalone book in staging forever waiting for parts that don't exist.

When the total is known (e.g. `1 of 3`), import is triggered automatically once all parts have arrived. When the total is unknown (e.g. `(1)` only), use `libris combine-parts --id N` to import manually.

### Flow

```
incoming/  Brisingr (part 1 of 3).m4b  →  staging/pending/  [waiting 1/3]
incoming/  Brisingr (part 2 of 3).m4b  →  staging/pending/  [waiting 2/3]
incoming/  Brisingr (part 3 of 3).m4b  →  staging/pending/  [complete!]
                                            ↓ ffmpeg concat (chapter-aware)
                                           staging/Brisingr.m4b
                                            ↓ metadata lookup + tagging
                                           Calibre  ✅
```

### Timeout

If the complete set hasn't arrived after `multipart.timeout_hours` (default 48h), the received parts are moved to `review/` with an explanatory note. Run `libris combine-parts --id N` to import whatever arrived.

---

## Series detection

Libris extracts series names and indices from filenames and API metadata, and writes them as tags that major audiobook apps understand.

### Filename patterns recognised

| Filename | Series | Index |
|----------|--------|-------|
| `Inheritance Cycle 1 - Eragon.m4b` | Inheritance Cycle | 1 |
| `Eragon (Inheritance Cycle, #1).epub` | Inheritance Cycle | 1 |
| `Harry Potter (Book 3).m4b` | Harry Potter | 3 |

### Tags written

| Tag | Used by |
|-----|---------|
| `grouping` | Apple Books, Prologue, Overcast, most M4B players |
| `series` + `series-part` | AudioBookshelf custom tags |
| Calibre series field | Calibre library |

---

## Filename conventions

Search queries are built by stripping noise from the filename (format tags, quality markers, part numbers, years, content hashes). Two structured conventions get first-class parsing:

| Convention | Example | Extracted |
|------------|---------|-----------|
| `Title - Author` | `Caliban and the Witch - Silvia Federici.epub` | title + author hint |
| `Title -- Author -- Year -- Publisher -- Hash` | `The Vegetarian -- Han Kang -- 2016 -- Hogarth -- 9daef8….epub` | title + author + year hints |

In the double-dash convention the fields are treated as authoritative: the title and author drive the search directly, the year becomes a scoring hint, and the publisher and content hash are discarded as query noise. Trailing md5/sha1/sha256 hashes are stripped from *any* filename, structured or not.

---

## Confidence scoring

Each file is scored against candidates from Google Books and OpenLibrary:

| Signal | Weight |
|--------|--------|
| ISBN match (extracted from filename) | 40% |
| Title similarity (fuzzy) | 30% |
| Author match | 20% |
| Publication year | 10% |

If both sources independently agree on the same book (titles > 85% similar, shared author surname), a cross-source agreement bonus of **+0.12** is applied. Files scoring below `confidence_threshold` (default `0.75`) go to `review/` instead of being imported.

### Strong-match floor

Without an ISBN in the filename, the maximum achievable base score is only 0.60 (title + author + year all perfect) or 0.72 with the agreement bonus — both below the default 0.75 threshold. Libris applies a confidence floor when title and author are both clearly correct, so an obvious match doesn't get sent to review just because the filename lacks an ISBN:

| Tier | Title score | Author score | Minimum confidence |
|------|-------------|--------------|-------------------|
| Strong | ≥ 90% (≤ a few chars off) | Exact surname | 0.82 |
| Good | ≥ 85% | First-name / token match | 0.76 |

Floors only raise confidence — they never lower an already-high score. When applied, a `strong_match_floor` entry appears in the score breakdown in the `rematch` UI.

Duplicate candidates (same book, different editions) are deduplicated before scoring — the highest-confidence edition is kept.

---

## Supported formats

| Type | Formats |
|------|---------|
| Ebook | epub, mobi, pdf, azw, azw3, cbz, cbr, djvu, and more |
| Audiobook | mp3, m4a, m4b, flac, ogg, aac, opus, wav |

All ebook formats are converted to your `preferred_ebook_format` (default: epub) before import unless `ebook_format_policy: all` is set.

Multi-part audiobooks (split files with part markers in the filename) are automatically staged and combined. Dropping a whole directory into `incoming/` is fully supported — Libris walks the entire directory tree recursively and dispatches every book file it finds.

**How directories are handled:**

- Audio files are grouped by the directory that directly contains them:
  - **One audio file** in a directory → imported as a standalone audiobook
  - **Multiple audio files** in the same directory → treated as parts of one audiobook and combined into a single M4B before import
- Ebook files at any depth are each dispatched individually through the ebook pipeline (conversion + Calibre import), regardless of how many share a directory
- The original directory tree is removed once all files have been extracted

This means you can drop an entire author or series folder — even with multiple levels of nesting — and every file is routed correctly:

```
incoming/
  Christopher Paolini/                    ← drop this whole folder
    Eragon.m4b                            → standalone audiobook import ✅
    Eldest/
      Eldest.m4b                          → standalone audiobook import ✅
    Brisingr/
      Brisingr - Part 1.m4b  ┐
      Brisingr - Part 2.m4b  ├─ combined → Brisingr.m4b → Calibre ✅
      Brisingr - Part 3.m4b  ┘
    Inheritance Cycle/
      Inheritance - Part 1.m4b  ┐
      Inheritance - Part 2.m4b  ├─ combined → Inheritance.m4b → Calibre ✅
      Inheritance - Part 3.m4b  ┘
    Eragon.epub                           → ebook import ✅
    Extras/
      Eragon (Special Edition).epub       → ebook import ✅
```

Ebook-only directories are handled the same way — each ebook file at any depth is extracted and processed individually.

---

## Notifications

Libris uses [ntfy.sh](https://ntfy.sh) for push notifications — a free, open-source service (or self-hostable) that sends alerts to your phone or desktop.

Notifications fire when:
- A file is quarantined to `review/` (low confidence match)
- A file fails processing and moves to `failed/`
- A part of a multi-part audiobook is staged (waiting for siblings)

### Setup

**1. Install the ntfy app**

| Platform | Link |
|----------|------|
| iOS | [App Store](https://apps.apple.com/app/ntfy/id1625396347) |
| Android | [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [F-Droid](https://f-droid.org/en/packages/io.heckel.ntfy/) |
| macOS / Windows / Linux | [ntfy.sh/docs/subscribe/web/](https://ntfy.sh/docs/subscribe/web/) |

**2. Choose a topic name**

A topic is just a string — anyone who knows it can subscribe, so make it something unguessable:

```
libris-abc123-yourname
```

No sign-up required for public topics on ntfy.sh.

**3. Subscribe in the app**

Open the ntfy app → **Add subscription** → enter your topic name. Leave the server as `https://ntfy.sh` unless you're self-hosting.

**4. Add to your config**

```yaml
ntfy:
  topic: libris-abc123-yourname   # your topic name
  enabled: true
  base_url: https://ntfy.sh       # default; change if self-hosting
```

**5. Test the connection**

```bash
libris check-config
```

This sends a test notification and reports success or the exact error if it fails.

### Private topics (optional)

For a private channel that requires authentication:

1. Create a free account at [ntfy.sh](https://ntfy.sh)
2. Generate an access token in your account settings
3. Add it to your config:

```yaml
ntfy:
  topic: my-private-topic
  auth_token: tk_yourtoken
  enabled: true
```

### Self-hosting ntfy

If you run your own ntfy server:

```yaml
ntfy:
  topic: libris
  base_url: https://ntfy.yourdomain.com
  auth_token: tk_yourtoken   # if your server requires auth
  enabled: true
```

See the [ntfy self-hosting docs](https://docs.ntfy.sh/install/) for server setup.

---

## Running as a daemon

### macOS — LaunchAgent

Create `~/Library/LaunchAgents/com.libris.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>              <string>com.libris</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/libris</string>
        <string>run</string>
        <string>--config</string>
        <string>/Users/yourname/.config/libris/config.yaml</string>
    </array>
    <key>RunAtLoad</key>          <true/>
    <key>KeepAlive</key>          <true/>
    <key>StandardOutPath</key>    <string>/Users/yourname/Library/Logs/libris/libris.log</string>
    <key>StandardErrorPath</key>  <string>/Users/yourname/Library/Logs/libris/libris.error.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LIBRIS_CONFIG</key>  <string>/Users/yourname/.config/libris/config.yaml</string>
    </dict>
</dict>
</plist>
```

```bash
mkdir -p ~/Library/Logs/libris
launchctl load ~/Library/LaunchAgents/com.libris.plist   # start now + on login
launchctl unload ~/Library/LaunchAgents/com.libris.plist  # stop
tail -f ~/Library/Logs/libris/libris.log                  # follow logs
```

> **Tip:** Replace `/usr/local/bin/libris` with the output of `which libris`.
> The `install.sh` script generates and loads this plist automatically.

---

### Linux — systemd user service

Create `~/.config/systemd/user/libris.service`:

```ini
[Unit]
Description=Libris book importer daemon
After=network.target

[Service]
Type=simple
ExecStart=/home/yourname/.local/bin/libris run --config /home/yourname/.config/libris/config.yaml
Restart=on-failure
RestartSec=10
Environment=LIBRIS_CONFIG=/home/yourname/.config/libris/config.yaml

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now libris       # start now + on login
systemctl --user status libris            # check status
journalctl --user -u libris -f            # follow logs
systemctl --user stop libris             # stop
```

> **Tip:** Replace paths with the output of `which libris` and `echo $HOME`.
> The `install.sh` script generates and enables this service automatically.

To start the service automatically even when you're not logged in (server setups):

```bash
sudo loginctl enable-linger "$USER"
```

---

## State database

Libris keeps a SQLite database to track every file it has seen. Useful queries:

```sql
-- Show files in review
SELECT current_path, matched_title, matched_author, confidence FROM files WHERE state='review';

-- Show failed files and why
SELECT original_path, error_msg FROM files WHERE state='failed';

-- Show pending multi-part groups
SELECT part_group_key, part_num, total_parts, current_path FROM files WHERE state='pending_parts' ORDER BY part_group_key, part_num;

-- Count by state
SELECT state, COUNT(*) FROM files GROUP BY state;
```

The CLI covers most day-to-day operations — direct SQL is only needed for bulk inspection or debugging.

---

## Troubleshooting

### `libris: command not found`

pip installs scripts to a location that may not be on your `PATH`. Find the correct bin directory and add it:

```bash
python3 -m site --user-base   # prints something like /Users/you/Library/Python/3.12
# Add <user-base>/bin to your PATH:
export PATH="$PATH:$(python3 -m site --user-base)/bin"
```

Add the `export` line to your shell profile (`~/.zshrc` or `~/.bashrc`) to make it permanent.

---

### `calibredb: command not found`

On macOS, Calibre installs as an app bundle. Add it to your PATH:

```bash
export PATH="$PATH:/Applications/calibre.app/Contents/MacOS"
```

On Linux, if you installed via the official installer: `~/.local/share/calibre/bin` or `/opt/calibre`. Check the Calibre [download page](https://calibre-ebook.com/download_linux) for the exact path.

---

### Confidence scores are always low (0.00–0.45)

This usually means Google Books is rate-limiting you (HTTP 429). Open Library is the fallback — it works but scores lower without the cross-source agreement bonus.

Fix options:
1. **Add a Google Books API key** — 1,000 requests/day, free:
   ```yaml
   metadata:
     google_books_api_key: YOUR_KEY_HERE
   ```
   Or let `libris rematch` walk you through it interactively (`[k]` at the rate-limit prompt).

2. **Lower the confidence threshold temporarily** for testing:
   ```yaml
   metadata:
     confidence_threshold: 0.50
   ```
   Reset to `0.75` for normal use.

3. **Use `libris rematch`** for files already in the review queue — it prompts interactively and lets you wait for the rate limit to clear.

---

### Files keep going to review instead of auto-importing

Check the score in `list-review`. If matches look correct but scores are low, see the rate-limit advice above. If matches are wrong (wrong title/author):

1. Run `libris rematch --id N` to search manually with a corrected query.
2. Try `Title by Author` format — routing the author to the API's author field gives much better results.
3. If the filename is the problem, rename it to a recognisable title before importing.

---

### State DB is corrupt

If Libris reports a corrupt state database:

```
❌  State DB at '.../libris.db' is corrupt or unreadable
```

The database is a rebuildable cache — it's safe to delete and start fresh:

```bash
mv ~/books/libris.db ~/books/libris.db.bak   # keep a backup just in case
libris run                                    # rebuilds from scratch
```

Files already imported into Calibre are unaffected. Files in `review/` and `failed/` will reappear on the next run or scan.

---

### `revert-import` / `audit-library` reports "export returned no files"

Two known causes:

**Wrong book ID** — confirm the ID with `libris revert-import --search`.

**Missing `--single-dir` flag** — without it, calibredb creates per-book subdirectories inside the export destination; in certain conditions no files appear at the top level with rc=0. Fixed in v0.3.4b0.

**Unsupported export format** — calibredb exports the book successfully but the file filter didn't recognise the extension (e.g. `.txt`, `.azw`, `.lit`, `.fb2`, `.rtf`, `.doc`, `.docx`). Fixed in v0.3.6b0: `_BOOK_EXTENSIONS` is now derived directly from the same extension sets the classifier uses, so all recognised formats are covered.

**Split-library mode (fixed v0.3.7b0)** — if `calibre.book_file_path` is set separately from `library_db_path`, Libris moves book files to `book_file_path` after import. But `calibredb export` constructs the export path relative to `library_db_path` and finds no files there (rc=0, empty result). Fixed: in split-library mode `export_book` now asks calibredb where files *should* be, remaps those paths from `library_db_path → book_file_path`, and copies the files directly. A name-based fallback scan handles books imported before this fix.

**Split-library revert-import blindness (fixed v0.3.17b0)** — a second code path in `_export_from_book_files` also queried `calibredb list --fields formats`, which always returns an empty formats list for relocated books. `revert-import` would log "calibredb export returned no files" even though the book existed. Fixed by applying the same `_db_format_paths()` fallback used in `_get_format_paths()`.

---

### Book imported to wrong directory structure

If a book imported to a path like `Books/Brisingr/Inheritance Cycle 3 (100)/` or `Books/Unknown/Book01-Merchant of Death (102)/` instead of `Books/{Author Sort}/{Title} (id)/`, you hit a bug fixed in v0.3.8b0.

**The real mechanism (fixed v0.3.8b0):** `calibredb add` builds the book directory by parsing the **filename** as `{title} - {author}` — it never reads embedded M4B audio tags. So `Inheritance Cycle 3 - Brisingr.m4b` became title "Inheritance Cycle 3" by author "Brisingr", and `Book01-Merchant of Death.m4b` (no ` - ` separator) became author "Unknown". Normally `calibredb set_metadata` repairs this by renaming the directory afterwards — but in split-library mode (`book_file_path` set separately) Libris had already moved the physical files to `book_file_path`, so the rename was a silent no-op and the wrong directory persisted, desynced from the database.

Two fixes in v0.3.8b0:
1. Libris now passes the resolved metadata to `calibredb add` via `--title`/`--authors`, so the directory is correct from the moment it is created. This covers both audiobooks and ebooks.
2. In split-library mode, `set_metadata` now mirrors any calibredb directory rename under `book_file_path`, keeping the physical files in sync with the database for all later metadata updates.

(Historical note: v0.3.4b0 and v0.3.7b0 attributed this bug to stale *embedded tags*. That explanation was wrong for the directory structure — Calibre doesn't read audio tags at add time. The embed step is still performed because the tags matter for audiobook players such as Audiobookshelf and Apple Books. v0.3.7b0's `-map_metadata 0:c` flag also caused ffmpeg to fail outright on chapterless M4Bs; v0.3.8b0 replaces it with `-map_chapters 0` and scopes the tag clear to global metadata so chapter titles survive.)

If you have already-imported books at a wrong path, use `libris revert-import` to remove them and re-import, or rename the directory in your Calibre library and run `calibredb check_library` to rescan.

---

### Duplicate not detected when one entry has a series prefix

Libris uses calibredb's exact-title search (`title:"=..."`) to spot duplicates. If one entry has a series prefix (`Pendragon: The Merchant of Death`) and the other doesn't (`The Merchant of Death`), the exact search misses the match and a duplicate is created.

Fixed in v0.3.17b0: when the primary exact search returns nothing, a secondary contains-mode search (no `=` prefix) is run against the bare title. This catches series-prefix mismatches in both directions.

If you already have duplicates from before this fix, use `libris revert-import` to remove the newer entry and re-import — Libris will then find the existing entry and merge the format.

---

### `import-one` says "refusing to import symlink"

Libris rejects symlinks in the incoming directory as a security measure — a symlink could point to arbitrary files on the host. Copy the actual file instead of symlinking it.

---

### Multi-part audiobook won't combine

- Check that part files have consistent markers in their names (`part 1 of 3`, `disc 1 of 2`, etc.)
- Run `libris list-pending` to see the current state of each group
- Use `libris combine-parts --id N` to force-combine whatever parts have arrived
- If a part is stuck in `failed/`, run `libris recover` to move it back to review first

---

### ntfy notifications not arriving

Run `libris check-config` — it sends a test notification and reports the exact error if it fails. Common issues:

- **Topic typo** — the topic in config must match exactly what you subscribed to in the ntfy app
- **Auth token required** — if you use a private topic, set `auth_token` in config
- **Self-hosted server** — update `base_url` to point to your server

---

## v0.3.18 (dev)

### Fixed: audiobook duplicates marked FAILED instead of prompting

`review-accept` on an audiobook already in Calibre could fail hard with a raw
`calibredb refused to add … already exist in the database` error and land the
file in the failed queue. calibredb's add-time duplicate detection matches on
**title alone**, so it rejects files that Libris's author-aware duplicate
search misses (e.g. when the existing book's author is stored differently).
The pipeline now catches that rejection and routes the file back to `review/`
flagged as a duplicate, so the CLI shows the overwrite / discard / skip /
rematch prompt as intended.

---

### New: `libris library --embed-cover`

Embeds a cover image directly into an ebook or audiobook file:

```bash
libris library --embed-cover book.epub            # uses cover.jpg from the same directory
libris library --embed-cover book.m4b cover.jpg   # uses the specified image
```

- **Ebooks** (epub, mobi, azw3, …) — delegates to `ebook-meta` (Calibre), which handles all OPF/ZIP bookkeeping inside the ebook container
- **Audiobooks** (m4b, mp3, m4a, flac, …) — uses `ffmpeg` to replace or add the cover stream; audio is stream-copied (no re-encode), existing tags and chapter markers are preserved
- If no cover path is given, `cover.jpg` in the same directory as the book is used automatically

---

### New: Web UI (Phases 1 & 2)

`libris web` starts a local web dashboard at `http://127.0.0.1:8888`.

```bash
pip install pylibris[web]
libris web                         # uses auto-discovered config
libris web --config /path/to/config.yaml --host 0.0.0.0 --port 9000
```

**Phase 1 — read-only dashboard:**
- **Config viewer** — all settings displayed with path-exists checks and the Google Books API key masked
- **Review queue** — lists all files awaiting human sign-off with match title, author, confidence score, and age
- **Failed queue** — lists failed files with error messages and missing-file indicators
- **Pending parts** — shows multi-part audiobook groups with which parts have and haven't arrived yet
- Live badge counts in the nav update on every page load

**Phase 2 — config editor:**
- **Full editable form** — every config setting (watcher, paths, calibre, metadata, output, ntfy, multipart) editable in-browser
- **Directory browser modal** — click Browse on any path field to navigate the filesystem and select or create directories without leaving the page
- **Validate before save** — config is parsed against the full dataclass schema before the file is written; validation errors are shown inline
- **Test Config** — one-click check that verifies all directories exist, `calibredb` is reachable (or the Docker container is running), the `reconnect_url` responds, and a live ntfy notification is sent

**Phases 3 & 4 — queue actions:**
- **Accept** — one-click force-import from the review queue using cached metadata (no API call, no new quota used)
- **Discard** — permanently delete a review-queue file and mark it so it won't be re-imported
- **Recover** — move a failed file back to `review/` for rematching and re-import
- **Remove** — permanently delete a failed file and clear its record
- Actions update the table row inline via HTMX; no full-page reload required
- Greyed-out Accept button indicates no metadata match yet — use `libris rematch --id N` then refresh

Combine-parts actions for the pending queue are planned for a future phase.

Requires: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `aiofiles` (all installed with `pip install pylibris[web]`).

### Internal / code quality

- **cli subpackage** — `libris/cli.py` split into `libris/cli/__init__.py`, `_helpers.py` (pure rendering), and `_setup.py` (config/IO). No user-visible changes; purely structural cleanup to keep the module manageable.
- **HTTP timeout constants** — `libris/_constants.py` centralises `HTTP_TIMEOUT_SHORT` (8 s), `HTTP_TIMEOUT_COVER` (10 s), `HTTP_TIMEOUT_API` (12 s) used by notifier, resolver, and CLI commands.
- **Unified `_fmt_age` helper** — duplicate age-formatting logic merged into a single `_fmt_age(delta)` function in `_helpers.py`.
- **`_apply_metadata_to_record` helper** — three identical 8-line metadata-assignment blocks in `pipeline.py` collapsed into one helper function.
- **`DoubleDashResult` TypedDict** — `parse_double_dash` in `cleaner.py` now returns a typed dict instead of a plain `dict`.

---

## Support

If Libris saved your library some chaos, you can support development:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/F6A4219OBI) [![GitHub Sponsors](https://img.shields.io/badge/GitHub-Sponsor-ea4aaa?logo=githubsponsors)](https://github.com/sponsors/markbyrne)

---

## License

[MIT](LICENSE)
