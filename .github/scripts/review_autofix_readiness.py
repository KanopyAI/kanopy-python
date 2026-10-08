"""Wait for CI and configured reviewers on the current PR head."""

CONTROLLER_PATH = ".github/workflows/review-autofix.yml"
ACTIVE_STATUSES = ("in_progress", "queued", "requested", "waiting", "pending")
REVIEWERS = {"greptile-apps[bot]", "sentry[bot]", "chatgpt-codex-connector[bot]"}
FINISHED = {"success", "failure", "timed_out", "neutral", "skipped"}


def pages(gh, path, key, max_pages=100):
    items = []
    for page in range(1, max_pages + 1):
        separator = "&" if "?" in path else "?"
        result = gh.get(f"{path}{separator}per_page=100&page={page}")
        items.extend(result[key])
        # Counts can lag active-run transitions. An exhausted page, rather than
        # a stale total_count, establishes the end of this API listing.
        if len(result[key]) < 100:
            return items
    raise RuntimeError(f"Incomplete {key}; cannot establish completion")


def completed_pr_reviews(gh, pr, names):
    """The initial PR review need not be repeated after a later commit."""
    reviewed = []
    for review in reversed(list(gh.pages(f"pulls/{pr['number']}/reviews"))):
        sha = review.get("commit_id")
        if (review.get("user", {}).get("login") in REVIEWERS
                and review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
                and sha and sha != pr["head"]["sha"] and sha not in reviewed):
            reviewed.append(sha)
    completed = set()
    for sha in reviewed[:10]:
        checks = pages(gh, f"commits/{sha}/check-runs?filter=latest", "check_runs")
        completed.update(check["name"] for check in checks
                         if check.get("name") in names and check["status"] == "completed"
                         and check.get("conclusion") in FINISHED
                         and (check.get("completed_at") or "") >= pr.get("created_at", ""))
        if names <= completed:
            break
    return completed


def inspect(gh, pr, profile, *, require_reviews=True):
    sha = pr["head"]["sha"]
    checks = pages(gh, f"commits/{sha}/check-runs?filter=latest", "check_runs")
    statuses = pages(gh, f"commits/{sha}/status", "statuses")
    # Completed controller history grows forever on an unchanged default head.
    # Query only active runs, including workflows whose jobs have not registered.
    runs = [run for status in ACTIVE_STATUSES
            for run in pages(gh, f"actions/runs?head_sha={sha}&status={status}",
                             "workflow_runs", max_pages=10)]

    # On dev -> staging promotions the controller itself runs on the PR head.
    # Its own pending jobs must not make it wait forever for itself to finish.
    controller_suites = {
        run["check_suite_id"] for run in runs
        if run.get("head_sha") == sha
        and run.get("path", "").split("@", 1)[0] == CONTROLLER_PATH
        and run.get("check_suite_id") is not None
    }
    relevant = [check for check in checks if not (
        (check.get("app") or {}).get("slug") == "github-actions"
        and (check.get("check_suite") or {}).get("id") in controller_suites
    )]
    if any(check["status"] != "completed" for check in relevant):
        return relevant, "waiting for checks/reviewers to finish"
    if any(status["state"] == "pending" for status in statuses):
        return relevant, "waiting for commit statuses"
    if any(run.get("head_sha") == sha and run["status"] != "completed"
           and run.get("path", "").split("@", 1)[0] != CONTROLLER_PATH
           for run in runs):
        return relevant, "waiting for workflow runs to finish"

    # An empty list is not proof that reviewers have finished: their apps may
    # not have registered a check yet. Cancellation is not a review result.
    required = set(profile.get("required_completion_checks", []))
    review_names = set(profile.get("review_check_names", []))
    if not require_reviews:
        required -= review_names
    present = {check.get("name") for check in relevant if check.get("conclusion") in FINISHED}
    present.update(status.get("context") for status in statuses)
    missing = required - present
    if missing & review_names:
        missing -= completed_pr_reviews(gh, pr, missing & review_names)
    if missing:
        return relevant, "waiting for checks/reviewers to report or be rerun: " + ", ".join(sorted(missing))
    return relevant, None
