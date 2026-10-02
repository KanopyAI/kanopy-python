#!/usr/bin/env python3
"""Trusted controller for automatic PR review fixes. Uses only the standard library."""

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_autofix_project as project
import review_autofix_ci as ci

PROFILE = json.loads((Path(__file__).resolve().parents[1] / "review-autofix.json").read_text())


SKIP_LABEL = "auto-fix-review-skip"
REVIEWERS = {"greptile-apps", "sentry", "chatgpt-codex-connector"}
PREFIX = "<!-- review-autofix:v1 "
MAX_ATTEMPTS = 3
DISPOSITIONS = {"fixed", "not_valid", "already_addressed", "needs_human"}


class GitHub:
    def __init__(self, repo, token):
        self.repo = repo
        self.token = token

    def api(self, path, data=None, method=None):
        req = urllib.request.Request(
            "https://api.github.com/" + path,
            data=None if data is None else json.dumps(data).encode(),
            method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.load(response)

    def get(self, path):
        return self.api(f"repos/{self.repo}" + (f"/{path}" if path else ""))

    def pages(self, path):
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            batch = self.get(f"{path}{separator}per_page=100&page={page}")
            yield from batch
            if len(batch) < 100:
                return
        raise RuntimeError("Pagination limit reached; manual review required")

    def comment(self, number, body, comment_id=None):
        path = f"issues/comments/{comment_id}" if comment_id else f"issues/{number}/comments"
        return self.api(f"repos/{self.repo}/{path}", {"body": body},
                        "PATCH" if comment_id else "POST")

    def findings(self, number):
        owner, name = self.repo.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              pageInfo { hasNextPage endCursor }
              nodes { id isResolved isOutdated path line
                comments(first:100) { pageInfo { hasNextPage endCursor } nodes {
                  id body url author { login } originalCommit { oid }
                } }
              }
            }
          } }
        }"""
        cursor = None
        found = []
        while True:
            result = self.api("graphql", {"query": query, "variables": {
                "owner": owner, "name": name, "number": number, "cursor": cursor}})
            if result.get("errors"):
                raise RuntimeError("Unable to read review threads: " + str(result["errors"]))
            threads = result["data"]["repository"]["pullRequest"]["reviewThreads"]
            for thread in threads["nodes"]:
                if not thread["isResolved"] and thread["comments"]["pageInfo"]["hasNextPage"]:
                    self.complete_thread(thread)
                finding = select_finding(thread)
                if finding:
                    found.append(finding)
            if not threads["pageInfo"]["hasNextPage"]:
                return found
            cursor = threads["pageInfo"]["endCursor"]


    def complete_thread(self, thread):
        query = """query($id:ID!,$cursor:String!) {
          node(id:$id) { ... on PullRequestReviewThread {
            comments(first:100,after:$cursor) { pageInfo { hasNextPage endCursor }
              nodes { id body url author { login } originalCommit { oid } }
            }
          } }
        }"""
        comments = thread["comments"]
        for _ in range(100):
            if not comments["pageInfo"]["hasNextPage"]:
                return
            result = self.api("graphql", {"query": query, "variables": {
                "id": thread["id"], "cursor": comments["pageInfo"]["endCursor"]}})
            if result.get("errors"):
                raise RuntimeError("Unable to read complete review discussion")
            page = result["data"]["node"]["comments"]
            comments["nodes"].extend(page["nodes"])
            comments["pageInfo"] = page["pageInfo"]
        raise RuntimeError("Review discussion pagination limit reached; manual triage required")


def select_finding(thread):
    comments = thread["comments"]["nodes"]
    if thread["isResolved"] or not comments:
        return None
    root = comments[0]
    login = (root.get("author") or {}).get("login", "").removesuffix("[bot]")
    if login not in REVIEWERS:
        return None
    if thread["comments"]["pageInfo"]["hasNextPage"]:
        raise RuntimeError("Review thread exceeds 100 comments; manual triage required")
    digest = hashlib.sha256(json.dumps(
        [(comment["id"], comment["body"]) for comment in comments],
        separators=(",", ":")).encode()).hexdigest()
    return {"key": digest, "thread": thread["id"], "path": thread["path"],
            "line": thread["line"], "outdated": thread["isOutdated"],
            "reviewer": login, "url": root["url"], "discussion": comments}


def eligible(pr, repo, default_branch):
    return (pr["state"] == "open" and not pr["draft"]
            and (pr["head"].get("repo") or {}).get("full_name") == repo
            and pr["head"]["ref"] not in {default_branch, "dev", "staging", "main", "master"}
            and SKIP_LABEL not in {x["name"] for x in pr["labels"]})


def claims(comments):
    result = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") != "github-actions[bot]":
            continue
        line = comment["body"].splitlines()[0] if comment["body"] else ""
        if not line.startswith(PREFIX) or not line.endswith(" -->"):
            continue
        state = json.loads(line[len(PREFIX):-4])
        state["comment_id"] = comment["id"]
        result.append(state)
    return result


def pending_findings(findings, history):
    processed = {key for run in history for key in run.get("processed", [])}
    return [finding for finding in findings if finding["key"] not in processed]


def fingerprint(sha, findings):
    value = sha + ":" + ":".join(sorted(f["key"] for f in findings))
    return hashlib.sha256(value.encode()).hexdigest()


def claim_body(state, message):
    data = {k: v for k, v in state.items() if k != "comment_id"}
    return PREFIX + ci.safe_json(data) + " -->\n" + message


def output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"{name}={value}\n")


def summary(message):
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(message + "\n")


def scan(gh, args):
    default_branch = gh.get("")["default_branch"]
    prs = [gh.get(f"pulls/{args.number}")] if args.number else gh.pages("pulls?state=open")
    numbers = [pr["number"] for pr in prs
               if eligible(pr, gh.repo, default_branch)]
    output("matrix", json.dumps(numbers))
    summary(f"Eligible PRs: {numbers}")


def prepare(gh, args):
    output("ready", "false")
    pr = gh.get(f"pulls/{args.number}")
    if not eligible(pr, gh.repo, gh.get("")["default_branch"]):
        summary(f"PR #{args.number}: not eligible (skip label, draft, fork, or protected branch).")
        return
    history = claims(gh.pages(f"issues/{args.number}/comments"))
    for previous in history:
        if previous["status"] != "running" or previous["run_id"] == os.environ.get("GITHUB_RUN_ID"):
            continue
        run = gh.get(f"actions/runs/{previous['run_id']}")
        if run["status"] == "completed":
            previous["status"] = "failed"
            if not args.dry_run:
                link = f"https://github.com/{gh.repo}/actions/runs/{previous['run_id']}"
                gh.comment(args.number, claim_body(previous,
                    f"Review autofix stopped when its run ended ({run.get('conclusion')}). "
                    f"[Inspect run]({link}) before continuing manually; a commit may already have been pushed. "
                    "This attempt remains counted and the same snapshot is not retried."), previous["comment_id"])
    if any(c["status"] == "needs_human" for c in history):
        summary(f"PR #{args.number}: paused for a human decision; continue manually.")
        return
    sha = pr["head"]["sha"]
    if len(history) >= MAX_ATTEMPTS:
        summary(f"PR #{args.number}: three-attempt limit reached; manual follow-up required.")
        return
    checks = gh.get(f"commits/{sha}/check-runs?per_page=100&filter=latest")
    if checks["total_count"] > 100:
        raise RuntimeError("More than 100 checks; cannot establish completion")
    if any(c["status"] != "completed" for c in checks["check_runs"]):
        summary(f"PR #{args.number}: waiting for checks/reviewers to finish.")
        return
    statuses = gh.get(f"commits/{sha}/status")
    if statuses["total_count"] and statuses["state"] == "pending":
        summary(f"PR #{args.number}: waiting for commit statuses.")
        return
    findings = pending_findings([
        *gh.findings(args.number), *ci.collect(gh, pr, checks["check_runs"], PROFILE)], history)
    snapshot = fingerprint(sha, findings)
    if not findings:
        summary(f"PR #{args.number}: no new reviewer findings or supported CI failures. This is not a merge approval.")
        return
    matches = [c for c in history if c["fingerprint"] == snapshot]
    if matches and not (matches[-1]["status"] == "failed" and matches[-1].get("retryable_validation")):
        summary(f"PR #{args.number}: this head and finding set already attempted; skipping.")
        return
    state = {"run_id": os.environ.get("GITHUB_RUN_ID", "preview"),
             "attempt": len(history) + 1, "sha": sha, "branch": pr["head"]["ref"],
             "fingerprint": snapshot, "keys": [f["key"] for f in findings],
             "urls": {f["key"]: f["url"] for f in findings},
             "ci_keys": [f["key"] for f in findings if f.get("kind") == "ci"],
             "status": "running", "processed": []}
    context = {"pr": args.number, "title": pr["title"], "state": state, "project": PROFILE,
               "findings": findings}
    prior = [c for c in history if c.get("sha") == sha and c.get("fingerprint") == snapshot
             and c.get("retryable_validation") and c.get("validation_failure")]
    if prior:
        context["previous_validation_failure"] = prior[-1]["validation_failure"]
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(context, indent=2)
    if len(serialized) > 200_000:
        message = (f"PR #{args.number}: review context exceeds 200,000 characters. "
                   "Automatic fixing is paused; manually triage the review threads.")
        summary(message)
        if not args.dry_run:
            # Keep the pause comment bounded even when there are many findings.
            state.update(status="needs_human", keys=[], urls={})
            gh.comment(args.number, claim_body(state, message))
        return
    (directory / "context.json").write_text(serialized)
    summary(f"PR #{args.number}: {len(findings)} findings, attempt {state['attempt']}/3, head {sha[:12]}.")
    if args.dry_run:
        summary("Preview only: no API call to Codex, comment, branch change, or push.")
        return
    if os.environ.get("HAS_OPENAI_KEY") != "true" or os.environ.get("HAS_PUSH_TOKEN") != "true":
        raise RuntimeError("Configure OPENAI_API_KEY and REVIEW_FIXER_TOKEN repository secrets first")
    link = f"https://github.com/{gh.repo}/actions/runs/{state['run_id']}"
    comment = gh.comment(args.number, claim_body(state,
        f"Review autofix attempt {state['attempt']}/3: investigating {len(findings)} findings. [Run]({link})."))
    state["comment_id"] = comment["id"]
    (directory / "context.json").write_text(json.dumps(context, indent=2))
    policy = Path(__file__).parents[1] / "prompts/review-autofix.md"
    (directory / "prompt.md").write_text(policy.read_text() +
        f"\nRead the review context from {directory / 'context.json'}.\n")
    output("sha", sha)
    output("ready", "true")


def validate_report(report, keys):
    if not isinstance(report.get("tests"), list) or not all(isinstance(t, str) for t in report["tests"]):
        raise ValueError("Report must include a list of test selectors")
    results = report["findings"]
    if (len(results) != len(keys) or {f["key"] for f in results} != set(keys)
            or any(f["status"] not in DISPOSITIONS or not f["explanation"].strip() for f in results)):
        raise ValueError("Report must account for every finding exactly once with evidence")
    if not isinstance(report["summary"], str) or len(json.dumps(report)) > 40_000:
        raise ValueError("Invalid or oversized report")
    return any(f["status"] == "fixed" for f in results)


def requires_regression(report, state):
    # CI formatting/build fixes can be verified by rerunning the failing check.
    # Reviewer-reported behavior bugs still require a changed regression test.
    return any(f["status"] == "fixed" and f["key"] not in state.get("ci_keys", [])
               for f in report["findings"])


def allowed_path(path):
    parts = PurePosixPath(path).parts
    return (bool(parts) and not PurePosixPath(path).is_absolute()
            and any(fnmatch.fnmatchcase(path, pattern) for pattern in PROFILE["allowed_paths"])
            and not any(p in {"..", ".git", ".github", ".codex"} for p in parts)
            and parts[-1] not in {"AGENTS.md", "conftest.py", "quarantine.txt", "package.json", "package-lock.json",
                                 "requirements.txt", "pyproject.toml", "setup.cfg", "Dockerfile", ".terraform.lock.hcl"}
            and not any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in
                        {"Dockerfile*", "*.Dockerfile", "docker-compose*", "compose.yaml", "compose.yml"})
            and not any(ord(c) < 32 for c in path))


def is_test_file(path):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in PROFILE["test_paths"])


def git(root, *args, env=None):
    return subprocess.check_output(["git", "-C", str(root), *args], env=env)


def validate_patch(root):
    paths = git(root, "diff", "--cached", "--no-renames", "--name-only", "-z").decode().split("\0")[:-1]
    if any(not allowed_path(path) for path in paths):
        raise ValueError("Patch touches a protected or unsupported path")
    for entry in git(root, "diff", "--cached", "--raw", "--no-renames", "-z").split(b"\0"):
        if entry.startswith(b":"):
            fields = entry.split()
            if fields[0][1:] not in {b"000000", b"100644", b"100755"} or fields[1] not in {b"000000", b"100644", b"100755"}:
                raise ValueError("Symlinks and submodules are not accepted")
    git(root, "diff", "--cached", "--check")
    return paths


def test_arguments(root, tests, *, allow_empty=False):
    if not isinstance(tests, list) or (not tests and not allow_empty):
        raise ValueError("Every fix requires affected regression tests")
    result = []
    for node in tests:
        if not isinstance(node, str):
            raise ValueError("Test selectors must be strings")
        path = node.split("::")[0]
        resolved = (root / path).resolve()
        if (not is_test_file(path)
                or not allowed_path(path) or not resolved.is_relative_to(root.resolve())
                or not is_test_file(str(resolved.relative_to(root.resolve())))
                or not resolved.is_file() or any(ord(c) < 32 for c in node)):
            raise ValueError("Only existing test file paths and node selectors are accepted")
        result.append(node)
    quarantine = root / "tests/quarantine.txt"
    if PROFILE["kind"] == "backend" and quarantine.exists():
        result += ["--deselect=" + line.strip() for line in quarantine.read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
    return result


def package(args):
    directory, root = Path(args.directory), Path(args.source)
    context = json.loads((directory / "context.json").read_text())
    report = json.loads((directory / "agent-report.json").read_text())
    fixed = validate_report(report, context["state"]["keys"])
    if git(root, "rev-parse", "HEAD").decode().strip() != context["state"]["sha"]:
        raise ValueError("Agent changed HEAD")
    git(root, "add", "--all")
    paths = validate_patch(root)
    if bool(paths) != fixed:
        raise ValueError("Patch must correspond to at least one confirmed fix")
    if fixed:
        changed_tests = [p for p in paths if is_test_file(p) and (root / p).is_file()]
        regression = requires_regression(report, context["state"])
        if regression and not changed_tests:
            raise ValueError("Reviewer fixes must include a regression test")
        # Always run each changed regression module, even if the agent omitted it.
        report["tests"] = list(dict.fromkeys([*report["tests"], *changed_tests]))
        tests = test_arguments(root, report["tests"], allow_empty=not regression)
        before = git(root, "diff", "--cached", "--binary")
        validation_log = directory / "validation.log"
        try:
            with project.capture_validation(validation_log):
                project.verify(root, tests, paths, PROFILE, findings=context.get("findings", []))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # A failed patch is evidence for another bounded attempt, never a
            # publishable artifact. Keep the candidate as it was before testing.
            artifact = directory / "artifact"
            artifact.mkdir(exist_ok=True)
            failure = ci.bounded_retry({"kind": "validation", "error": str(exc),
                                        "log": project.log_tail(validation_log),
                                        "patch": before.decode("utf-8", errors="replace")})
            (artifact / "validation-failure.json").write_text(ci.safe_json(failure))
            raise
        git(root, "add", "--all")
        validate_patch(root)
        if (git(root, "diff", "--cached", "--binary") != before
                or git(root, "rev-parse", "HEAD").decode().strip() != context["state"]["sha"]):
            raise ValueError("Tests changed the proposed patch or HEAD; refusing to publish untested changes")
    artifact = directory / "artifact"
    artifact.mkdir(exist_ok=True)
    patch = git(root, "diff", "--cached", "--binary")
    if len(patch) > 1_000_000:
        raise ValueError("Patch too large for automatic publication")
    (artifact / "change.patch").write_bytes(patch)
    (artifact / "report.json").write_text(json.dumps(report, indent=2))


def publish(gh, args):
    history = claims(gh.pages(f"issues/{args.number}/comments"))
    run_id = os.environ["GITHUB_RUN_ID"]
    state = next((c for c in history if c["run_id"] == run_id and c["status"] == "running"), None)
    if not state:
        return
    link = f"https://github.com/{gh.repo}/actions/runs/{run_id}"
    try:
        directory = Path(args.directory)
        failure_file = directory / "validation-failure.json"
        if failure_file.exists():
            failure = ci.retry_details(failure_file)
            pr = gh.get(f"pulls/{args.number}")
            retryable = (state["attempt"] < MAX_ATTEMPTS
                         and eligible(pr, gh.repo, gh.get("")["default_branch"])
                         and pr["head"]["sha"] == state["sha"]
                         and pr["head"]["ref"] == state["branch"])
            state.update(status="failed", retryable_validation=retryable,
                         validation_failure=failure)
            # The fingerprint retains identity; the next attempt reconstructs
            # findings. Drop redundant lists so the diagnostic comment is bounded.
            for key in ("keys", "ci_keys", "urls"):
                state.pop(key, None)
            next_step = ("The next poll will investigate this patch and validation output again."
                         if retryable else "Automatic fixing has stopped; manual follow-up is required.")
            gh.comment(args.number, claim_body(state,
                f"Review autofix attempt {state['attempt']}/3 failed validation; no patch was pushed. "
                f"[Inspect run]({link}). {next_step}\n\n" + failure["error"].replace("@", "＠")), state["comment_id"])
            return
        report = json.loads((directory / "report.json").read_text())
        fixed = validate_report(report, state["keys"])
        patch = (directory / "change.patch").read_bytes()
        if bool(patch) != fixed or len(patch) > 1_000_000:
            raise ValueError("Missing or invalid patch")
        pr = gh.get(f"pulls/{args.number}")
        if (not eligible(pr, gh.repo, gh.get("")["default_branch"])
                or pr["head"]["sha"] != state["sha"] or pr["head"]["ref"] != state["branch"]):
            raise ValueError("PR changed or was opted out while the fixer ran; patch was not pushed")
        if fixed:
            root = Path(args.source)
            root.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ, GH_TOKEN=os.environ["REVIEW_FIXER_TOKEN"],
                       GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="credential.helper",
                       GIT_CONFIG_VALUE_0="!gh auth git-credential", GIT_TERMINAL_PROMPT="0")
            git(root, "init")
            git(root, "remote", "add", "origin", f"https://github.com/{gh.repo}.git")
            git(root, "fetch", "--depth=1", "origin", state["sha"], env=env)
            git(root, "checkout", "--detach", "FETCH_HEAD")
            git(root, "apply", "--index", str((directory / "change.patch").resolve()))
            paths = validate_patch(root)
            if requires_regression(report, state) and not any(is_test_file(p) and (root / p).is_file() for p in paths):
                raise ValueError("Missing regression test")
            git(root, "-c", "user.name=github-actions[bot]", "-c",
                "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m",
                f"fix: address review and CI findings (round {state['attempt']})")
            # Recheck immediately before the network write after fetching/applying.
            latest = gh.get(f"pulls/{args.number}")
            if (not eligible(latest, gh.repo, gh.get("")["default_branch"])
                    or latest["head"]["sha"] != state["sha"]):
                raise ValueError("PR changed or was opted out before push")
            # Normal fast-forward push: a concurrent human push is rejected, never overwritten.
            git(root, "push", "origin", f"HEAD:refs/heads/{state['branch']}", env=env)
            state["pushed_sha"] = git(root, "rev-parse", "HEAD").decode().strip()
        state["status"] = "needs_human" if any(f["status"] == "needs_human" for f in report["findings"]) else "completed"
        state["processed"] = [f["key"] for f in report["findings"] if f["status"] != "needs_human"]
        message = f"Review autofix attempt {state['attempt']}/3 completed. [Run]({link}).\n\n"
        # Prevent report text from accidentally mentioning or commanding other bots.
        message += report["summary"].replace("@", "＠") + "\n\n"
        for item in report["findings"]:
            url = state["urls"][item["key"]]
            message += f"- [Finding]({url}) **{item['status']}**: {item['explanation'].replace('@', '＠')}\n"
        if fixed:
            message += f"\nPushed `{state['pushed_sha']}` after the selected tests passed. CI and fresh reviews are still required.\n"
            message += "Validation: " + (", ".join(f"`{t}`" for t in report["tests"]) or "trusted CI checks for the failing jobs") + ".\n"
        if state["attempt"] == MAX_ATTEMPTS or state["status"] == "needs_human":
            message += "\nAutomatic fixing has stopped. Manual follow-up is required for any remaining findings.\n"
        if fixed:
            try:
                GitHub(gh.repo, os.environ["REVIEW_FIXER_TOKEN"]).comment(args.number, "@codex review")
            except Exception as exc:
                message += (f"\nThe fix was pushed, but requesting Codex review failed ({type(exc).__name__}). "
                            "Request review manually before merging.\n")
                summary("Fix published; fresh Codex review must be requested manually.")
        gh.comment(args.number, claim_body(state, message), state["comment_id"])
    except Exception as exc:
        # Report from a fresh publisher runner, including agent/test failures with no artifact.
        if state.get("pushed_sha"):
            state["status"] = "needs_human"
            message = (f"Fix was pushed as `{state['pushed_sha']}`, but reporting failed. "
                       f"[Inspect run]({link}) and confirm review/CI before continuing manually.\n\n"
                       f"{type(exc).__name__}: {exc}")
            summary(message)
            gh.comment(args.number, claim_body(state, message), state["comment_id"])
            return
        state["status"] = "failed"
        gh.comment(args.number, claim_body(state,
            f"Review autofix stopped. [Inspect run]({link}).\n\n{type(exc).__name__}: {exc}\n\n"
            "This attempt counts toward the three-run limit. No automatic retry of the same snapshot."), state["comment_id"])
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["scan", "prepare", "setup", "package", "publish"])
    parser.add_argument("--number", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--directory", default=".")
    parser.add_argument("--source", default=".")
    args = parser.parse_args()
    if args.command == "setup":
        context = json.loads((Path(args.directory) / "context.json").read_text())
        project.setup(Path(args.source).resolve(), context, PROFILE)
        return
    if args.command == "package":
        package(args)
        return
    gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    {"scan": scan, "prepare": prepare, "publish": publish}[args.command](gh, args)


if __name__ == "__main__":
    main()
