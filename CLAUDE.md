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
# package job's installed-wheel check, in a throwaway venv so the editable install stays
w=$(mktemp -d) && python -m venv "$w" && "$w/bin/pip" install -q --disable-pip-version-check dist/*.whl \
  && "$w/bin/python" -c "import importlib.metadata, kanopy; assert kanopy.__version__ == importlib.metadata.version('kanopy-ai')"
```

Customer impact (`customer-impact.yml`): stage the change, then

```bash
python3 "$RELEASE_ENGINE/scripts/kanopy_release.py" stamp .release-notes/<slug>.json
git commit ...   # check reads HEAD, not the working tree
python3 "$RELEASE_ENGINE/scripts/kanopy_release.py" check
```

The engine is a private repository: a session without access to
KanopyAI/kanopy-release-engine gets a warning and no `$RELEASE_ENGINE`, so add that
repository to the session (or its cloud environment) to stamp and check.
