# Libris

**Automatic Calibre import with confidence-scored metadata matching.**

If you use Calibre to manage your ebook and audiobook library, you've likely hit these problems:
- Books imported with the wrong author or title — silently, with no warning
- Manually dragging files into Calibre one at a time
- Audiobooks arriving as split parts with no metadata
- `calibredb` commands that require you to be at the machine

Libris solves all of this. It watches a directory for new ebooks and audiobooks, automatically matches them to the correct metadata using Google Books and OpenLibrary, converts files to the right format (EPUB for ebooks, M4B with chapter markers for audiobooks), and imports them directly into your Calibre library via `calibredb`.

The key difference from a simple import script: Libris scores each metadata match for confidence. Files it's certain about are imported immediately. Files where the match is ambiguous are quarantined in a review folder and you're notified — so your library is never silently polluted with wrong metadata.

Works with local Calibre installations and with [calibre-web](https://github.com/janeczku/calibre-web) running in Docker. Pairs naturally with self-hosted download managers like LazyLibrarian, Readarr, and similar tools.

---

## Features

- **Automatic import** — drop a file, it appears in Calibre with correct metadata; startup scan catches files that arrived while the daemon was offline
- **Confidence scoring** — two independent metadata sources cross-checked before import
- **Full metadata** — title, author, cover art, description, publisher, series, language, ISBN all written to Calibre
- **Series detection** — extracts series name and index from filenames and API data; writes tags for Apple Books, Prologue, and AudioBookshelf
- **Review queue** — low-confidence matches held for your approval, never silently wrong
- **Interactive rematch** — re-query metadata APIs from the terminal with live score breakdowns
- **Web search fallback** — if both APIs return no results, DuckDuckGo Instant Answers is queried for author/ISBN hints and the search is retried automatically
- **Multi-part audiobooks** — parts held in staging until the complete set arrives, then combined into one M4B with chapter markers and imported automatically
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

### Config file discovery

Libris looks for a config file in order — the first one found is used:

1. `config.local.yaml` — in the current directory (git-ignored, ideal for local overrides)
2. `config.yaml` — in the current directory
3. `~/.config/libris/config.yaml` — user-level config

You can always override with `--config <path>` on any command. For most workflows, creating `config.local.yaml` next to the repo is the simplest setup.

```bash
cp config.example.yaml config.local.yaml
# Edit config.local.yaml with your paths
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
  library_path: ~/Calibre Library

metadata:
  confidence_threshold: 0.75

output:
  preferred_ebook_format: epub   # epub | mobi
  ebook_format_policy: preferred # preferred | all (see below)

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

```yaml
multipart:
  timeout_hours: 48   # default; set to 0 to disable automatic escalation
```

### Environment variable overrides

Any config value can be overridden with a `LIBRIS_` prefixed environment variable:

```bash
LIBRIS_CALIBRE_MODE=docker
LIBRIS_METADATA_CONFIDENCE_THRESHOLD=0.80
LIBRIS_NTFY_TOPIC=my-topic
LIBRIS_MULTIPART_TIMEOUT_HOURS=24
```

---

## Usage

### `check-config` — validate your setup

```bash
libris check-config
```

Prints all resolved config values, confirms calibredb is reachable, and sends a test ntfy notification if configured.

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

### `run` — start the daemon

```bash
libris run
```

Watches `incoming_dir` continuously and processes files as they arrive. Drop any ebook or audiobook into the directory and it will be imported automatically. Ctrl-C to stop.

On startup, the incoming folder is scanned immediately so any files that arrived while the daemon was offline are processed without waiting. A background thread re-scans the folder periodically (default every hour) as an additional safety net.

Configure the scan interval in your config:

```yaml
watcher:
  scan_interval_hours: 1.0   # set to 0 to disable the periodic scan
```

`libris check-config` shows the resolved scan setting:

```
  Folder scan:    on startup + every 1h
```

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

If there are also files in the failed state, a warning is shown — run `libris recover` to handle them.

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

```bash
# By review queue ID (from list-review)
libris review-accept --id 1

# All files at once
libris review-accept --accept-all

# By path (quote paths with spaces)
libris review-accept "/books/review/Caliban and the Witch.epub"
```

#### Accepting duplicates

If the file was flagged as a duplicate of an existing Calibre book, `review-accept` blocks by default and shows you the exact command to use:

```
  ⚠   Brisingr.m4b
       Already in Calibre — add --overwrite to import anyway:
       libris review-accept --id 1 --overwrite
```

Use `--overwrite` when you intentionally want to replace or supplement an existing entry:

```bash
libris review-accept --id 1 --overwrite
```

To delete the duplicate instead, use `review-discard`:

```bash
libris review-discard --id 1          # delete this one
libris review-discard --duplicates    # delete all duplicate-flagged items
libris review-discard --stale         # remove DB records where the file is already gone
```

---

### `rematch` — interactively fix a bad metadata match

When the auto-matched title or author is wrong, `rematch` lets you search the APIs yourself and pick the right result.

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

**No results?** If both APIs return nothing, Libris automatically queries DuckDuckGo Instant Answers for author and ISBN hints, then retries. In `rematch`, suggested search refinements are shown:

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

### `recover` — move failed files back to review

Files that fail processing (e.g. due to a network error or rate limit) are moved to `failed/`. Use `recover` to return them to `review/` so they can be rematched and imported.

```bash
# List failed files
libris recover

# Recover a specific file
libris recover --id 1

# Recover everything
libris recover --all
```

After recovery, files appear in `libris list-review` and can be fixed with `libris rematch`.

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
```

If a group times out before all parts arrive, the received parts are moved to `review/` with a note. They remain importable via `combine-parts`.

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

When the total is known (e.g. `part 1 of 3`), import is triggered automatically once all parts have arrived. When the total is unknown (e.g. `Part 1` only), use `libris combine-parts --id N` to import manually.

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

## Confidence scoring

Each file is scored against candidates from Google Books and OpenLibrary:

| Signal | Weight |
|--------|--------|
| ISBN match (extracted from filename) | 40% |
| Title similarity (fuzzy) | 30% |
| Author match | 20% |
| Publication year | 10% |

If both sources independently agree on the same book (titles > 85% similar, shared author surname), a cross-source agreement bonus of +0.08 is applied. Files scoring below `confidence_threshold` (default `0.75`) go to `review/` instead of being imported.

Duplicate candidates (same book, different editions) are deduplicated before scoring — the highest-confidence edition is kept.

---

## Supported formats

| Type | Formats |
|------|---------|
| Ebook | epub, mobi, pdf, azw, azw3, cbz, cbr, djvu, and more |
| Audiobook | mp3, m4a, m4b, flac, ogg, aac, opus, wav |

All ebook formats are converted to your `preferred_ebook_format` (default: epub) before import unless `ebook_format_policy: all` is set.

Multi-part audiobooks (split files with part markers in the filename) are automatically staged and combined. Audiobook folders (a directory of audio files) are also supported — drop the whole directory into `incoming/` and each file is dispatched individually through the normal pipeline.

Files without part markers are imported as standalone books. Files with matching part markers are automatically grouped and combined. A single directory can contain a mix of standalone books and multi-part series:

```
incoming/
  Christopher Paolini/
    Eragon.m4b                         → imported individually ✅
    Eldest.m4b                         → imported individually ✅
    Brisingr (part 1 of 3).m4b  ┐
    Brisingr (part 2 of 3).m4b  ├──── combined → Brisingr.m4b → Calibre ✅
    Brisingr (part 3 of 3).m4b  ┘
    Inheritance (part 1 of 3).m4b  ┐
    Inheritance (part 2 of 3).m4b  ├── combined → Inheritance.m4b → Calibre ✅
    Inheritance (part 3 of 3).m4b  ┘
```

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

-- Show pending multi-part groups
SELECT part_group_key, part_num, total_parts, current_path FROM files WHERE state='pending_parts' ORDER BY part_group_key, part_num;

-- Count by state
SELECT state, COUNT(*) FROM files GROUP BY state;
```

The CLI covers most day-to-day operations — direct SQL is only needed for bulk inspection or debugging.

---

## License

MIT
