# Libris Release Handover

## What a clean release looks like

```
pyproject.toml version:  0.2.0b1        ← PEP 440 (not "0.2.0-beta")
git tag:                 v0.2.0-beta     ← GitHub pre-release convention
GitHub release flag:     pre-release ✓
```

## Steps (in order)

```bash
# 1. Confirm version in pyproject.toml is PEP 440 (e.g. 0.2.0b1, not 0.2.0-beta)

# 2. Build
python -m build

# 3. Tag — delete first if recycling a tag
git tag -d v0.2.0-beta 2>/dev/null; git push origin :refs/tags/v0.2.0-beta 2>/dev/null
git tag v0.2.0-beta
git push origin master
git push origin v0.2.0-beta

# 4. GitHub release
gh release delete v0.2.0-beta --yes 2>/dev/null
gh release create v0.2.0-beta \
  --title "Libris v0.2.0 Beta" \
  --notes-file RELEASE-NOTES-v0.2.0-beta.md \
  --prerelease \
  dist/libris-0.2.0b1.tar.gz \
  dist/libris-0.2.0b1-py3-none-any.whl

# 5. Smoke test
pip install git+https://github.com/markbyrne/libris.git@v0.2.0-beta
libris --version   # expect: libris, version 0.2.0b1

# 6. Bump version for next cycle
# pyproject.toml → version = "0.3.0.dev0"
git commit -am "chore: bump to 0.3.0.dev0" && git push origin master
```

## Gotchas we hit

| Problem | Fix |
|---|---|
| `gh release edit --tag` detaches the release | Always delete + recreate instead |
| Recycled tag points at wrong commit | Delete tag locally and remotely before re-tagging |
| GitHub push rejected (email privacy) | Use `3760653+markbyrne@users.noreply.github.com` as commit author |
| Git lock files left behind by sandbox | `find .git -name "*.lock" -delete` |
| `install.sh` prompts invisible (hang) | `printf` in `ask()` must go to stderr (`>&2`) — stdout is captured by `$()` |
| curl error 43 downloading from private repo | Pass auth via `--config -` stdin, not embedded in URL |
| Token captured in `ask_secret` output | Prompt text was being pasted with token; use a minimal prompt + validate `ghp_`/`github_pat_` prefix |

## Post-release checklist

- [ ] Close milestone / issues
- [ ] Version bumped to `x.y.z.dev0` on master
- [ ] `RELEASE-NOTES-v*.md` committed or archived
- [ ] PAT rotated if it appeared in any logs
