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
- **Full metadata** — title, author, cover art, description, publisher, series, language, ISBN all written to Calibre
- **Review queue** — low-confidence matches held for your approval, never silently wrong
- **Interactive rematch** — re-query metadata APIs from the terminal with live score breakdowns
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

### Google Books API key

Libris works without an API key (unauthenticated, ~60 requests/minute per IP), but adding a free API key is recommended for regular use (1,000 requests/day, more reliable).

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

### Environment variable overrides

Any config value can be overridden with a `LIBRIS_` prefixed environment variable:

```bash
LIBRIS_CALIBRE_MODE=docker
LIBRIS_METADATA_CONFIDENCE_THRESHOLD=0.80
LIBRIS_NTFY_TOPIC=my-topic
```

---

## Usage

### `check-config` — validate your setup

```bash
libris check-config
```

Prints all resolved config values and confirms calibredb is reachable.

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

---

### `list-review` — see what needs attention

```bash
libris list-review
```

```
  2 file(s) in review
  ──────────────────────────────────────────────────

  [1]  Caliban and the Witch.epub
        Matched:  Caliban and the Witch  by anarchivists
        Score:    0.52
        Path:     "/Users/you/books/review/Caliban and the Witch.epub"

  [2]  some-audiobook.m4b
        Matched:  (unknown)
        Score:    n/a
        Path:     "/Users/you/books/review/some-audiobook.m4b"

  ──────────────────────────────────────────────────
  Accept by ID:    libris review-accept --id <N>
  Accept all:      libris review-accept --accept-all
  Accept by path:  libris review-accept "<path>"
  Fix bad match:   libris rematch --id <N>
```

If there are also files in the failed state, a warning is shown at the bottom — run `libris recover` to handle them.

---

### `review-accept` — force-import a reviewed file

Accepts the current metadata match and imports the file into Calibre, bypassing the confidence threshold.

```bash
# By review queue ID (from list-review)
libris review-accept --id 1

# All files at once
libris review-accept --accept-all

# By path (quote paths with spaces)
libris review-accept "/books/review/Caliban and the Witch.epub"
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

## Confidence scoring

Each file is scored against candidates from Google Books and OpenLibrary:

| Signal | Weight |
|--------|--------|
| ISBN match (extracted from filename) | 40% |
| Title similarity (fuzzy) | 30% |
| Author match | 20% |
| Publication year | 10% |

If both sources independently agree on the same book (titles > 85% similar, shared author surname), a cross-source agreement bonus of +0.08 is applied. Files scoring below `confidence_threshold` (default `0.75`) go to `review/` instead of being imported.

---

## Supported formats

| Type | Formats |
|------|---------|
| Ebook | epub, mobi, pdf, azw, azw3, cbz, cbr, djvu |
| Audiobook | mp3, m4a, m4b, flac, ogg, aac, opus, wav |

Multi-part audiobooks (a folder of files) are automatically combined into a single M4B with chapter markers.

---

## Notifications

Libris uses [ntfy.sh](https://ntfy.sh) for push notifications — a free, open-source service (or self-hostable) that sends alerts to your phone or desktop.

Notifications fire when:
- A file is quarantined to `review/` (low confidence match)
- A file fails processing and moves to `failed/`

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

If you run your own ntfy server (e.g. on Plexi):

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

-- Count by state
SELECT state, COUNT(*) FROM files GROUP BY state;
```

The CLI covers most day-to-day operations — direct SQL is only needed for bulk inspection or debugging.

---

## License

MIT
