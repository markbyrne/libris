# Libris v0.3.0 Beta

## What's new

### Bug fix — multi-part audiobook recognition (#1)

Files named with bare sequential numbers in parentheses — the most common downloader convention — were not being grouped and were imported as separate single-file audiobooks. `extract_part()` now recognises:

- `Book Title (1).m4b` / `Book Title (2).m4b` — bare number in parens
- `Book Title (1 of 3).m4b` — bare "N of M" in parens (no keyword required)
- `Book Title (1/3).m4b` — bare "N/M" in parens

Year false-positives (`Book (2021)`) are not affected — the pattern is capped at 3 digits and end-anchored.

### New command — `mark-as-part` (#2)

Manually flag a file in the review queue as part N of an audiobook group. Useful when a multi-file audiobook arrives without recognisable part markers.

```
libris mark-as-part --id <N> --part <num> --total <total> [--group <name>]
```

When all parts of the group have been marked, the combine-and-import step runs automatically. `list-review` now shows a hint for audio items explaining when to use this command.

### Auto-refresh queue after accept/rematch (#3)

Running `review-accept` or `rematch` used to silently renumber the review queue. You now see the updated queue immediately after each action — no need to run `list-review` again.

### Chaff filtering (#4)

Non-book files (READMEs, cover images, `.txt`/`.nfo`/`.url` sidecars, zero-length stubs) are now detected before any metadata lookup and sent directly to the failed queue. This prevents them cluttering the review queue.

A `--chaff` batch-discard flag has been added to `review-discard` for cleaning up any that slipped through before upgrading:

```
libris review-discard --chaff
```

### Cleaner installer defaults (#5)

`install.sh` now defaults to `~/libris/` as the root for all watch folders (previously `~/books/`). Each directory prompt includes a short description of its purpose, and the generated `config.yaml` carries inline comments on every path entry.

---

## Upgrade notes

No database schema changes. Drop-in upgrade:

```bash
pip install --upgrade git+https://github.com/markbyrne/libris.git@v0.3.0-beta
libris check-config
```

---

## Test coverage

135 unit tests — all passing.
