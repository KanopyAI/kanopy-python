# Releasing `kanopy-ai`

Publishing is tag-driven and uses PyPI Trusted Publishing. No long-lived PyPI
token belongs in GitHub, a local environment, or this repository.

## One-time repository setup

1. Create the public SDK repository and make this directory its root.
2. Protect `main` and require the `Python SDK CI` checks.
3. Create a GitHub environment named `pypi` with a required human reviewer.
4. Register the PyPI Trusted Publisher for package `kanopy-ai`, workflow
   `release.yml`, and environment `pypi`.
5. Protect release tags matching `v*`.

## Release checklist

1. Choose the version and update `src/kanopy/_version.py`.
2. From the private Kanopy development repository, run
   `./scripts/sync_public_openapi.sh`, then review and commit the SDK fixture.
3. Run `./scripts/run_local_smoke.sh` against the current backend Docker build.
4. Run `python -m pytest`, Ruff checks, and a clean package build.
5. Merge through the protected `main` branch and confirm CI passes.
6. Confirm that `Auto-tag SDK release` created the matching tag and dispatched
   `Release to PyPI`. The workflow fails instead of silently skipping when
   release-relevant files changed without a version bump.
7. Review and approve the `pypi` deployment environment. This is the only
   manual publishing step.
8. Install the exact published version into a clean environment and run the
   read-only identity/project-list smoke check against staging.

If automatic dispatch fails after the tag is created, run `Release to PyPI`
manually against that tag from the GitHub Actions page. A direct `v*` tag push
also remains supported.

The release job rebuilds nothing after approval: the publishing job downloads
the exact wheel and source distribution produced and inspected by the build
job. Trusted Publishing also creates PyPI attestations by default.
