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
Logs use GitHub's existing masking, common credential patterns are additionally
redacted, and excerpts are bounded before they reach the model. Expired logs fall
back to check summaries and annotations. Missing evidence may require manual work.

It reruns the affected test shard or service, SDK Python version, frontend unit or
browser/build checks, simulator tests, or local Terraform validation. Cloud plans
are diagnosed from logs; the fixer never runs a cloud plan/apply. Infrastructure,
credential, dependency-policy, and runner problems that need protected changes
are reported for manual follow-up. Full remote CI must pass after a fix is pushed.

A failed verifier never publishes its patch. It saves a bounded diagnostic artifact,
and the fresh publisher records that evidence in the bot state comment. On the
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

## Controller tests

```bash
python3 -m unittest discover -s .github/scripts -p 'test_review_autofix*.py' -v
```

As in the repository's existing CI, dependency installation and test code from
same-repository PRs are trusted to execute on the runner. Fork PRs are excluded.
The agent is sandboxed to its source checkout, and push credentials are present
only on the fresh publisher runner.

These tests cover eligibility, attempt limits, publication checks, test failure
handling, and project-specific validation commands. They do not substitute for
a live Codex run with the repository's configured credentials.
