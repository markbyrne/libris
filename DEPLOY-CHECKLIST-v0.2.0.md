# Deploy checklist: Libris v0.2.0 Beta

**Date:** 2026-06-07 | **Release type:** Standard · GitHub Package (initial Python release)

## Pre-deploy

- [ ] All tests passing in CI (`pytest`)
- [ ] Code reviewed and approved
- [ ] `pyproject.toml` version confirmed as `0.2.0b1` (PEP 440 beta; git tag will be `v0.2.0-beta`)
- [ ] `CHANGELOG` / release notes written
- [ ] No uncommitted changes on the release branch
- [ ] `build` and `twine` installed (`pip install build twine`)
- [ ] GitHub token has `packages: write` permission (or PyPI token configured)
- [ ] `.gitignore` excludes `dist/`, `*.egg-info/`
- [ ] `pyproject.toml` metadata reviewed (description, keywords, license, classifiers)
- [ ] Rollback plan documented (see below)

## Deploy

- [ ] Run tests: `pytest`
- [ ] Build distributions: `python -m build`
- [ ] Inspect artifacts: `ls dist/` — confirm `.tar.gz` and `.whl` present
- [ ] Check package contents: `tar tzf dist/libris-0.2.0b1.tar.gz | head -30`
- [ ] Tag the release: `git tag v0.2.0-beta && git push origin v0.2.0-beta`
- [ ] Publish to GitHub Packages (or PyPI):
  - GitHub Packages: `twine upload --repository-url https://upload.pypi.org/legacy/ dist/* ` *(with `__token__` + GitHub PAT)*
  - PyPI: `twine upload dist/*`
- [ ] Verify package appears on the registry
- [ ] Install from registry in a clean environment and smoke-test: `pip install libris==0.2.0b1`

## Post-deploy

- [ ] Create GitHub Release from the `v0.2.0-beta` tag — mark as **pre-release** in GitHub UI
- [ ] Mark related issues / milestone as closed
- [ ] Update `README` if install instructions reference a version
- [ ] Notify stakeholders / announce
- [ ] Bump `pyproject.toml` version to `0.3.0.dev0` on `main`

## Rollback triggers

- Package installs but crashes on import
- Critical bug found that breaks core functionality
- Wrong files included in the published artifact

**Rollback action:**
- PyPI: use `pip` yanking via PyPI UI (packages cannot be deleted after 24 h, but can be yanked so `pip install libris` skips the version)
- GitHub Packages: delete version via GitHub UI → Settings → Packages
- Then patch and re-publish as `0.2.1`
