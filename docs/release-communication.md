# Reviewed customer-impact records

Every PR records its full source impact in `.release-notes/<slug>.json`. The shared
validator requires an assessment of API and behavioral compatibility, permissions,
availability, dependencies, existing data and processing/rerun effects, plus an
explicit `developer_notice` decision. Internal tooling changes use `type: internal`,
`api.impact: none` and `developer_notice.publish: false` with an explanation.

Use the `ENTRY_SCHEMA` in the release engine's `scripts/release_notes.py` and
`.release-notes/autofix-impact-rollout.json` as a complete internal example. Change
the id to match the filename and describe the actual change; do not copy an
internal assessment onto a customer-visible change. Initially use a 64-zero
`source_digest` and an empty `source_files` array. The stamp command fills them.

Assess client method signatures, request/response behavior, authentication,
permissions, retry/error handling, pagination, upload/download behavior and supported
Python versions. Describe whether existing integrations need to change. Keep private
implementation details out of customer notices. Required actions and API behavior
changes cannot be hidden by marking the developer notice unpublished.

Source review is separate from publishing an SDK version. Use `coordinated`
availability and explain the required versioned package publication for runtime
changes. A merge or successful test run does not prove that users can install the
change. This repository does not enable automatic customer-notice publication or
verified package-release tracking through this gate.

After reviewing the entire PR delta, stage source and stamp the new record:

```bash
git fetch origin main
git add <source-files> .release-notes/<slug>.json
python3 "$RELEASE_ENGINE/scripts/release_notes.py" stamp --base origin/main --feature --file .release-notes/<slug>.json
git add .release-notes/<slug>.json
# Commit the reviewed change, then validate that committed candidate:
python3 "$RELEASE_ENGINE/scripts/release_notes.py" check --base origin/main --feature
python3 "$RELEASE_ENGINE/scripts/release_notes.py" preflight --head HEAD --target origin/main --base origin/main
```

The `Customer impact recorded` PR check validates both the authored feature and
the exact merged candidate with the pinned release engine; the engine's own
tests run in its repository. Here `--base origin/main` is the PR integration baseline, not evidence
of a published package. Always supply an explicit base in this repository.
Affected SDK tests, Ruff checks and package validation remain required;
the preflight can run an optional command with `--run`, using an isolated checkout. Install any test dependencies there; common
API and publisher tokens are removed from the test environment. The autofixer never publishes SDK packages.

When main moves, a clean merge can combine source into a file blob that neither
parent assessment covers. Reconcile main, review the combined behavior, and stamp
again before review. Rerun preflight whenever either branch changes. Entries
already on main are immutable; add a fresh assessment for corrections or reverts.
The `supersedes` notice mechanism is reserved for repositories with a verified
publication baseline; use a fresh assessment here.

The autofixer pins the PR head and merge base, requires an explicit review of every
current-PR record, stamps the staged candidate, and independently validates it
again before publishing a patch. Historical records and arbitrary metadata paths
remain protected. Missing current-head impact checks block model work; failed
impact checks are eligible for a reviewed metadata repair. Source fixes retain
the existing application checks and the three-attempt budget.

The release engine lives in [KanopyAI/kanopy-release-engine](https://github.com/KanopyAI/kanopy-release-engine). This
repository's workflows run it from a pinned commit, never from this checkout, so a PR
cannot change the validator that judges it. Change the engine with a PR there, then
update the checkout's `ref` and version comment here by hand, with a stamped internal
impact entry.
For local commands, check out the engine at that pin and point `RELEASE_ENGINE` at it:
`gh repo clone KanopyAI/kanopy-release-engine ~/kanopy-release-engine`, then
`git -C ~/kanopy-release-engine checkout <pin>` and
`export RELEASE_ENGINE=~/kanopy-release-engine`. Its `scripts/kanopy_release.py pin`
prints the pin, and `kanopy_release.py stamp|check` default the base branch.
The autofix controller
and report schema live in [KanopyAI/kanopy-autofix](https://github.com/KanopyAI/kanopy-autofix).
Consume engine fixes through reviewed version-update PRs; see the autofix runbook.
