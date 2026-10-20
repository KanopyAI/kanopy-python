@AGENTS.md

## Before you push

In cloud sessions the SessionStart hook puts a Python 3.13 venv with `.[dev]`, `build`
and `twine` first on `PATH` and exports `RELEASE_ENGINE` (the engine commit
`customer-impact.yml` pins). Run what `.github/workflows/ci.yml` runs:

```bash
python -m ruff check src tests scripts          # quality job
python -m ruff format --check src tests scripts
python scripts/check_version.py
python -m pytest -q                             # test job (CI adds 3.10-3.12, 3.14)
rm -rf dist && python -m build && python -m twine check dist/* \
  && python scripts/check_dist.py dist          # package job
```

Customer impact (`customer-impact.yml`): stage the change, then

```bash
python3 "$RELEASE_ENGINE/scripts/kanopy_release.py" stamp .release-notes/<slug>.json
git commit ...   # check reads HEAD, not the working tree
python3 "$RELEASE_ENGINE/scripts/kanopy_release.py" check
```
