# Automatic PR review fixes

The `Review autofix` workflow in `KanopyAI/kanopy-python` checks eligible PRs every ten minutes.
All open, non-draft PRs from this repository's feature branches are included
without a label. Add `auto-fix-review-skip` to exclude a PR. Forks and PRs from
`dev`, `staging`, `main`, `master`, or the repository's default branch are excluded.

It investigates unresolved inline findings from Greptile, Sentry, and Codex,
and completed failed/timed-out GitHub Actions checks from the configured validation workflows,
fixes confirmed bugs with regression tests, reruns validation, and pushes a normal
commit to the same PR branch. A fresh publisher checks that the branch head and
eligibility have not changed. It requests a fresh Codex review after pushing;
configure other review bots to review every push if they should join later rounds.

## Validation for this repository

Run selected pytest modules and every changed regression module, then Ruff checks and formatting validation for src/tests/scripts. Never publish a package.

The trusted policy is `.github/review-autofix.json`. The fixer may change only its
listed source/test/documentation paths. CI, dependency manifests, AGENTS.md,
conftest.py, quarantine files, symlinks, and submodules need manual work. Reviewer bug fixes
must include a changed regression test. CI-only repairs can use the existing
failing check, which the trusted verifier reruns; unsupported environments are reported for follow-up.
The workflow never merges, deploys, applies infrastructure, or publishes releases.

## Failed CI checks

The fixer waits for checks to finish, then collects failure evidence only for the
current PR head and the latest matching Actions jobs. The allowlisted workflow/job
pairs are in `ci_workflows`; the fixer never executes commands taken from logs.
Logs are streamed with bounded memory to preserve their actual ending, with a
30-second download deadline. GitHub masking and additional common-credential
redaction apply before bounded excerpts reach the model. Expired or temporarily unavailable logs fall
back to check summaries and annotations. Missing evidence may require manual work.

It reruns the affected test shard or service, SDK Python version, frontend unit or
browser/build checks, simulator tests, or local Terraform validation. Environment-specific cloud plan failures are collected for a manual disposition;
local validate alone cannot prove those checks fixed. The fixer never runs a
cloud plan/apply. A PR with only these manual checks pauses without a model call. Infrastructure,
credential, dependency-policy, and runner problems that need protected changes
are reported for manual follow-up. Full remote CI must pass after a fix is pushed.

A failed verifier never publishes its patch. It saves a bounded diagnostic artifact,
and the fresh publisher records bounded evidence in the bot state comment.
Diagnostic budgets include JSON/HTML escaping, so Unicode-heavy patches remain
within GitHub comment limits. Only matching finding snapshots reuse that evidence. On the
next poll, a fresh checkout receives the previous failure and candidate patch as
context for a corrected attempt. This does not reset the three-attempt budget.

## One-time setup

1. Merge the workflow into `main`, the default branch.
2. Store `OPENAI_API_KEY` and `REVIEW_FIXER_TOKEN` as repository Actions secrets.
   The GitHub token needs Contents and Pull requests read/write access to this
   repository. Keep both values out of chat and version control.
3. Optionally set `REVIEW_AUTOFIX_MODEL` to an API model available to the OpenAI
   project; otherwise the Codex Action uses its default.
4. Run **Actions → Review autofix → Run workflow** with an optional PR number and
   **dry_run** checked. Preview mode collects findings without model calls or writes.
5. Set repository Actions variable `REVIEW_AUTOFIX_ENABLED=true`. Existing and
   future eligible PRs are included automatically. Schedules are best-effort;
   a manual run with **dry_run** unchecked can process one PR immediately.

The enabled variable defaults to off. API model usage is billed to the supplied
OpenAI project; polls with no new findings do not invoke the model. A ChatGPT
subscription does not fund this workflow.

## Stopping and results

- Add `auto-fix-review-skip` to a PR to prevent new attempts and publication of an
  in-progress fix when observed by the publisher. A label change cannot be atomic
  with a Git push; cancel the Actions run as well to stop an active model call.
- Set `REVIEW_AUTOFIX_ENABLED=false` to stop new automatic runs repository-wide.
- At most three attempts per PR, including failures. A human-decision finding
  pauses further attempts. Changing labels does not reset the budget.
- Bot-authored PR comments preserve attempt history, dispositions, validation,
  and links to the run. Keep those comments. No review thread is auto-resolved.
- Cancelled or abandoned attempts are reconciled on the next poll; inspect the
  linked run before continuing manually. Duplicate finding snapshots are skipped.
  A failed local validation preserves bounded diagnostics and a candidate patch
  for another attempt on the next poll. These retries share the three-attempt cap.
  Agent/setup failures and cancelled runs are not retried for the same snapshot.
  The full lifecycle is serialized, with up to two different PRs fixed
  in parallel. Each fixer job has a 90-minute timeout.
- Inputs include inline review threads and current-head Actions failures selected
  by `ci_workflows` in the trusted policy. Deployment/release workflows, cancelled
  jobs, external providers, and summary-only reviewer comments need manual triage.
  The old `auto-fix-review` label has no effect.
- Full CI and reviews of the newly pushed commit are still required before merging.

## Codex completion and recovery

The pinned official Codex action runs through a trusted execution adapter. Its
upstream `action.yml` checksum must match before the adapter changes the final
launch command. Authentication, API proxy isolation, sandboxing, and privilege
dropping remain in the official action. Codex CLI and its proxy are pinned to
`0.160.0` for reproducibility.

An upstream [inherited-output-stream hang](https://github.com/openai/codex-action/issues/169)
can leave an Actions step running after Codex returns its final report. The
adapter puts the official wrapper and its descendants behind private log files,
streams progress to Actions, and emits a heartbeat every 30 seconds. Children
cannot keep the runner's log transport open. Cleanup stops the private process
group and tracked descendants; Linux also adopts orphaned descendants.

If a valid report for the current findings is stable for 60 seconds but the
wrapper has not exited, the adapter stops its processes before proceeding to
independent validation. A missing or invalid report fails after 30 minutes.
An explicit cancellation or nonzero process exit is never converted to success.
The publisher does not run on cancelled workflow runs. The existing 90-minute
job limit also covers setup and independent validation.

Every supervised attempt saves `agent-run.json`, a bounded redacted log tail,
the available `agent-report.json`, and a candidate patch (up to 2 MB) in the run's
`review-autofix-<PR>` artifact. These are diagnostic files, not a publishable
patch. Only independent policy checks and successful validation produce
`change.patch` and `report.json`, which the fresh publisher can consume. A hard
agent timeout stays a failed attempt under the existing three-attempt budget.

## Report validation and diagnostics

The report's `tests` array accepts literal existing test files, optionally with
pytest `::node` selectors. Put outcomes and timeout explanations in the summary
or finding explanations. Invalid selectors fail before tests execute and retain
the candidate patch for the existing bounded validation retry (three attempts).

Other packaging failures preserve a bounded, redacted `packaging-failure.json`.
The publisher reports that original error instead of looking for an absent
validated report. Policy violations and unexpected packaging errors remain
failed attempts requiring manual follow-up. Neither diagnostic artifact permits
publication; partial `change.patch` and `report.json` files are removed on failure.

## Shared engine and updates

The controller, prompts, runner adapter and regression suite are maintained in
[KanopyAI/kanopy-autofix](https://github.com/KanopyAI/kanopy-autofix). This repository
keeps its policy in `.github/review-autofix.json` and an immutable engine commit in
`.github/autofix-engine.json`. The caller workflows are generated from that engine.
Fix shared automation bugs and add their regression tests centrally; do not restore
local controller copies or shared-core parity manifests.

Private repositories call the pinned reusable workflow. The public Python SDK
uses a generated bootstrap from the same job definition because GitHub cannot
call a private reusable workflow from a public repository. It downloads the same
pinned engine without publishing private engine bundles as artifacts.

Engine checkout uses `AUTOFIX_ENGINE_READ_TOKEN` when configured, otherwise the
existing `REVIEW_FIXER_TOKEN`. The credential needs read access to the private
engine repository. Every checkout disables credential persistence, and downloads
finish before PR dependencies or tests execute. The model and verifier receive
neither credential. Publication runs on a fresh runner.

After central tests and a frontend dry-run, a maintainer promotes a merged engine
commit to the central `stable` channel. The weekly **Update autofix engine** job
(or a manual dispatch) opens a normal PR with the new immutable pin, generated
workflows and a stamped internal impact record. It never merges automatically,
never resets attempt history, and respects a previously closed update PR. `stable`
is used only to discover updates; live jobs execute the reviewed commit pin.

Review the engine change and consumer checks before merging an update. To roll
back, regenerate the callers and lock using a previously tested engine commit,
add a new impact record, and review that PR normally. Configure or change local
policy here, then regenerate using the pinned engine's `scripts/render_consumer.py`.
Keep the generated files together; CI rejects inconsistent pins or workflows.

The central README documents controller tests and Linux/macOS process regressions.
Consumer CI validates the policy, pin and generated workflows without executing
candidate caller scripts. Application CI and customer-impact validation remain
required. Existing bot comments, finding fingerprints, three-attempt limits,
opt-outs, human pauses and per-repository concurrency survive migration.

## Customer-impact records travel with fixes

The trusted `customer_impact.required` policy enables impact validation in all
six autofixer repositories: backend, frontend, Powerline, iOS, infrastructure and
the Python SDK. They use the shared autofix engine and retain their own trusted release engine;
do not add a blanket `.release-notes/*` exception to `allowed_paths`.
Infrastructure and SDK PRs validate against their explicit main-branch merge base
and combined merge candidate. Their gate does not claim that infrastructure has
been applied or that an SDK package has been published; those remain separate
release processes. See the repository's release-communication guide.

Before the model runs, the controller pins the PR head and merge base and lists
only impact entries added by this PR. Historical entries stay immutable. A PR
without an entry receives one deterministic allowed filename. Promotion fixes
receive a new record against the head of their separate fix PR; an existing
promotion release-policy failure requires manual assessment.

Every published fix requires the model's explicit, head-specific review of each
entry against the full PR delta, including the original author change. The model
updates prose when behavior changes and records evidence when wording remains
accurate. Only then does trusted code stamp the staged source digest and blob
fingerprints. It validates the entry, reruns applicable application checks, and
rejects test mutations. The clean publisher validates again, without restamping
or executing PR scripts, and rechecks the PR base and head before pushing.

`Customer impact recorded` is a required completion check and a supported CI
repair target when this policy is enabled. A metadata-only repair runs release
validation; a source repair also runs application validation. The optional release
drafting bot still cannot overwrite author-written entries. Missing assessments,
protected-file edits and invalid records produce diagnostic artifacts and no push.
Attempts still have the same three-run budget; this change does not reset failed
or paused PRs or declare them ready based on a previous head's green checks.

Live runs use the default branch's reviewed engine pin and policy. Merging a
migration or update there activates that version for subsequent attempts; updating
a feature branch alone does not. Existing exhausted or needs_human attempts retain
their state and require manual follow-up. Application deployments are unaffected.
