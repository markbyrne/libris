# Libris Nightly Agent Team — Handoff Task

This document is a self-contained runbook for a Claude session to execute the libris nightly engineering workflow. Read it fully before taking any action.

---

## Setup

You will be given a `LIBRIS_PAT` — a GitHub fine-grained personal access token for the `markbyrne/libris` repository. Store it in your environment and authenticate the `gh` CLI before doing anything else:

```bash
export LIBRIS_PAT="<token provided by user>"
echo "$LIBRIS_PAT" | gh auth login --with-token --hostname github.com
gh auth status   # verify it worked
```

If `gh` is not installed:
```bash
# macOS
brew install gh
# Linux
sudo apt install gh -y
```

---

## Project Context

**Repo:** https://github.com/markbyrne/libris  
**Language:** Python 3.10+  
**Purpose:** Watches a directory for new ebooks/audiobooks, matches them to metadata via Google Books and OpenLibrary (confidence-scored), converts files, and imports them into Calibre. Designed to pair with LazyLibrarian / Readarr / calibre-web ecosystems.

**Key source modules:**
- `libris/pipeline.py` — main import pipeline
- `libris/metadata/` — resolver, scorer, Google Books, OpenLibrary, DuckDuckGo fallback
- `libris/audio/` — converter, tagger (M4B, chapter markers)
- `libris/ebook/` — converter (EPUB)
- `libris/calibre/` — local and Docker calibredb integration
- `libris/classifier.py` — ebook vs audiobook detection
- `libris/state.py` — SQLite state store
- `libris/cli.py` — Click CLI entry point
- `libris/config.py` — YAML config handling
- `libris/notifier.py` — ntfy.sh push notifications
- `libris/watcher/` — fswatch (macOS) and inotify (Linux) watchers

**Tests:** `tests/` directory. Run with `pytest` from repo root.  
**Install for dev:** `pip install -e ".[dev]"`

---

## Your Role: Team Lead Agent

You are the **Team Lead**. You orchestrate a team of specialist sub-agents using the `Agent` tool. You do not write code yourself — you delegate, review outcomes, and make final decisions.

**Your responsibilities:**
1. Fetch open issues authored by `markbyrne` on the repo
2. Triage and prioritise: **bugs first**, then enhancements
3. Cap at **3 issues per run** to avoid runaway sessions
4. For each issue, coordinate the full pipeline below
5. Post a summary comment on each issue and close it if the fix was merged
6. If anything fails, rollback and comment explaining what failed

---

## Issue Fetch & Triage

```bash
gh issue list \
  --repo markbyrne/libris \
  --author markbyrne \
  --state open \
  --json number,title,body,labels,createdAt \
  --limit 50
```

Sort results: issues labelled `bug` or `type: bug` first, then `enhancement` or `type: enhancement`. Take the top 3 after sorting. Skip any issue where a branch `fix/issue-{N}` or `feat/issue-{N}` already exists **and** has an open PR — it is in-flight from a prior run.

Check for existing branches:
```bash
gh pr list --repo markbyrne/libris --state open --json headRefName,number
```

---

## Per-Issue Pipeline

For each selected issue, run the following stages **in order**. Each stage is a sub-agent spawned with the `Agent` tool. Pass each sub-agent its full context — it has no memory of this session.

### Branch naming
- Bug → `fix/issue-{number}`
- Enhancement → `feat/issue-{number}`

### Stage 0 — Branch Setup (you do this, not a sub-agent)

```bash
cd /path/to/cloned/libris

# Clone if not already present
gh repo clone markbyrne/libris libris-workspace || true
cd libris-workspace
git fetch origin

BRANCH="fix/issue-42"   # or feat/issue-42
# If branch already exists locally or remotely, check it out and pull
if git ls-remote --exit-code origin "$BRANCH"; then
  git checkout "$BRANCH"
  git pull origin "$BRANCH"
else
  git checkout -b "$BRANCH" origin/main
fi
```

---

### Stage 1 — Engineer Agent

**Spawn with Agent tool. Prompt must include:**
- Full issue title, body, and number
- Branch name to work on
- Repo path on disk
- Project context from the "Project Context" section above
- Instruction: implement the fix or enhancement described in the issue
- Must write or update tests in `tests/` covering the change
- Must not break existing tests (`pytest` must pass)
- Must commit with message: `fix(#{number}): <short description>` or `feat(#{number}): ...`
- Must push the branch: `git push origin {branch}`

The engineer agent should:
1. Read relevant source files before making changes
2. Make targeted, minimal changes — do not refactor unrelated code
3. Write at least one test for the new behaviour
4. Run `pytest` and confirm it passes before pushing
5. If `pytest` is not runnable (missing deps, config), note this clearly in its response

---

### Stage 2 — Code Review Agent

**Spawn with Agent tool. Prompt must include:**
- Issue number and title
- Branch name
- Instruction: review the diff between `origin/main` and the branch

```bash
git diff origin/main...origin/{branch}
```

Review for:
- Correctness relative to the issue description
- Edge cases not covered
- Code style consistency with the existing codebase (PEP 8, docstrings where present)
- N+1 or performance issues
- Error handling gaps

Return: **PASS** or **FAIL** with specific line-level findings. If FAIL, list exactly what must be fixed before proceeding.

If FAIL → stop the pipeline for this issue, rollback (see Rollback section), comment on issue.

---

### Stage 3 — Security Audit Agent

**Spawn with Agent tool. Prompt must include:**
- Issue number and title  
- Branch name
- The full diff (same command as above)
- Project context: this project makes HTTP requests to Google Books, OpenLibrary, DuckDuckGo APIs; reads/writes files on disk; executes shell commands (`calibredb`, `ffmpeg`, `fswatch`/`inotifywait`); stores state in SQLite

Audit for:
- Injection risks (shell, SQL, path traversal)
- Untrusted input handled unsafely (filenames, API responses, config values)
- Secrets or credentials leaked into logs or state
- Unsafe use of `subprocess` (shell=True without sanitisation)
- Dependency changes introducing known vulnerabilities

Return: **PASS** or **FAIL** with specific findings. If FAIL → rollback and comment on issue.

---

### Stage 4 — QA Agent

**Spawn with Agent tool. Prompt must include:**
- Issue number, title, and full body
- Branch name
- Repo path on disk
- Instruction: verify the fix actually addresses the issue

The QA agent should:
1. Run `pytest tests/` and confirm all tests pass
2. If the issue describes a specific failure, write or run a targeted test that demonstrates the fix
3. Check that no regressions exist in related modules
4. If tests cannot run (environment issue), explicitly state this — do not assume pass

Return: **PASS** or **FAIL** with test output. If FAIL → rollback and comment on issue.

---

### Stage 5 — Documentation Agent

**Spawn with Agent tool. Prompt must include:**
- Issue number and description of the change
- Branch name
- Repo path on disk
- Instruction: update `README.md` if the change affects user-facing behaviour, CLI flags, config keys, or installation steps

Rules:
- Only update README if the change is user-visible. Internal refactors do not require README changes.
- Keep the existing README style and formatting
- Commit with message: `docs(#{number}): update README for <change>`
- Push the branch after committing

---

### Stage 6 — Merge (you do this, Team Lead)

Only proceed if all four stages returned PASS.

1. Create a PR if one doesn't exist:
```bash
gh pr create \
  --repo markbyrne/libris \
  --base main \
  --head {branch} \
  --title "fix(#{number}): <title>" \
  --body "Closes #{number}\n\nAutomated fix by libris nightly agent team."
```

2. Merge it:
```bash
gh pr merge {pr-number} \
  --repo markbyrne/libris \
  --squash \
  --delete-branch
```

3. Post a closing comment on the issue:
```bash
gh issue comment {number} \
  --repo markbyrne/libris \
  --body "$(cat <<'EOF'
**Nightly Agent Team — Automated Resolution**

- **Engineer:** Implemented fix on branch \`{branch}\`
- **Code Review:** PASS
- **Security Audit:** PASS
- **QA:** All tests passed
- **Docs:** README updated (if applicable)
- **Merged:** PR #{pr-number} squash-merged to main

Closing this issue.
EOF
)"
```

4. Close the issue:
```bash
gh issue close {number} --repo markbyrne/libris
```

---

## Rollback

If any stage returns FAIL:

```bash
# Delete remote branch
git push origin --delete {branch}

# Post failure comment
gh issue comment {number} \
  --repo markbyrne/libris \
  --body "**Nightly Agent Team — Pipeline Failure**

Stage **{stage name}** returned FAIL for this issue. The branch \`{branch}\` has been deleted.

Findings:
{paste agent findings here}

This issue remains open for manual review."
```

Do **not** close the issue. Move on to the next issue in the queue.

---

## Rules Summary

| Rule | Detail |
|------|--------|
| Max issues per run | 3 |
| Priority order | Bugs before enhancements |
| Skip condition | Branch + open PR already exists |
| Merge strategy | Squash merge, delete branch |
| Rollback trigger | Any stage FAIL |
| README update | Only for user-visible changes |
| Commit style | `fix(#{N}):` / `feat(#{N}):` / `docs(#{N}):` |

---

## End of Run

After all issues are processed (pass or fail), print a plain-text summary to the session:

```
=== Libris Nightly Agent Run — {date} ===

Issues processed: {N}

{For each issue}
  #{number} "{title}" — MERGED / FAILED ({stage})
```

That's it. No email, no external notifications.
