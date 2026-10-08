"""Keep an autofix and its reviewed impact record in one verified transaction.

Only trusted controller/release-engine code runs here, including in the publisher.
The PR's scripts, configuration and impact prose are never executed.
"""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess


TARGET = "release:impact"
NOTES = ".release-notes/"
SHA = r"[a-f0-9]{40}"
NOTE = r"\.release-notes/[a-z0-9][a-z0-9-]{2,79}\.json"


def enabled(profile):
    return profile.get("customer_impact", {}).get("required", False) is True


def pin(gh, pr, state, profile):
    if not enabled(profile):
        return
    # Promotion fixes get their own PR against the current dev/staging head.
    is_promotion = bool(state.get("promotion_base"))
    base = (state["sha"] if is_promotion else gh.get(
        f"compare/{pr['base']['sha']}...{state['sha']}")["merge_base_commit"]["sha"])
    if not re.fullmatch(SHA, base):
        raise ValueError("Invalid customer-impact base")
    state["impact"] = {
        "base": base,
        "base_ref": pr["head"]["ref"] if is_promotion else pr["base"]["ref"],
        "new_entry": f"{NOTES}autofix-pr-{pr['number']}-{state['sha'][:12]}.json",
    }


def recheck_base(gh, pr, state, profile):
    if not enabled(profile):
        return
    candidate = dict(state)
    pin(gh, pr, candidate, profile)
    if candidate["impact"] != state.get("impact"):
        raise ValueError("PR customer-impact base changed; prepare a new assessment")


def classify(findings, pr):
    # Promotion checks cover deployed history/notice policy, not a feature diff.
    # A separate source-fix PR cannot repair that history by restamping it.
    if pr["head"]["ref"] in {"dev", "staging"}:
        for finding in findings:
            if finding.get("kind") == "ci" and finding.get("target") == TARGET:
                finding["manual_only"] = True


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        stderr=subprocess.PIPE,
    ).decode().strip()


def policy(state, profile):
    if not enabled(profile):
        return None
    value = state.get("impact", {})
    if (not re.fullmatch(SHA, value.get("base", ""))
            or not re.fullmatch(SHA, state.get("sha", ""))
            or not re.fullmatch(NOTE, value.get("new_entry", ""))):
        raise ValueError("Missing trusted customer-impact context")
    return value


def engine():
    # Resolve relative to this trusted file, never to the PR checkout or sys.path.
    path = Path(__file__).with_name("release_notes.py")
    spec = importlib.util.spec_from_file_location("trusted_release_notes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def in_checkout(root):
    previous = Path.cwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous)


def exists(root, tree, path):
    return bool(git(root, "ls-tree", tree, "--", path))


def allowed_entries(root, state, profile):
    value = policy(state, profile)
    if value is None:
        return []
    paths = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                "--name-only", "-z", value["base"], state["sha"], "--", NOTES).split("\0")
    paths = [p for p in paths if p]
    if len(paths) > 20:
        raise ValueError("Too many impact entries for automatic review")
    for path in paths or [value["new_entry"]]:
        if not re.fullmatch(NOTE, path) or exists(root, value["base"], path):
            raise ValueError("Historical or unsupported impact entry; manual correction required")
    return paths or [value["new_entry"]]


def describe(root, context, profile):
    if not enabled(profile):
        return
    paths = allowed_entries(root, context["state"], profile)
    context["customer_impact"] = {
        "base_sha": context["state"]["impact"]["base"],
        "head_sha": context["state"]["sha"],
        "allowed_entries": paths,
        "entry_schema": engine().ENTRY_SCHEMA,
    }


def assessment(report, state, paths):
    review = report.get("customer_impact")
    if (not isinstance(review, dict) or set(review) != {"head_sha", "entries"}
            or review["head_sha"] != state["sha"] or not isinstance(review["entries"], list)):
        raise ValueError("Fix requires an explicit customer-impact review for this head")
    entries = review["entries"]
    if (len(entries) != len(paths)
            or any(not isinstance(e, dict) or set(e) != {"path", "assessment"}
                   or not isinstance(e["assessment"], str)
                   or not 20 <= len(e["assessment"].strip()) <= 4000 for e in entries)
            or {e["path"] for e in entries} != set(paths)):
        raise ValueError("Review every current-PR impact entry with concrete behavioral evidence")


def check(root, report, state, profile):
    """Verify the staged candidate without modifying it or executing PR code."""
    value = policy(state, profile)
    if value is None:
        return
    paths = allowed_entries(root, state, profile)
    assessment(report, state, paths)
    release = engine()
    tree = git(root, "write-tree")
    with in_checkout(root):
        entries = release.read_entries(value["base"], tree, feature=True)
        if {f"{NOTES}{e['id']}.json" for e in entries} != set(paths):
            raise ValueError("Candidate must preserve every current-PR impact entry")
        fingerprints = release.source_files(value["base"], tree)
        if any(e["source_files"] != fingerprints for e in entries):
            raise ValueError("Customer-impact file fingerprints do not match the candidate")


def review_and_stamp(root, report, state, profile):
    """Stamp only after the agent has reviewed prose against the entire PR delta."""
    value = policy(state, profile)
    if value is None:
        return
    paths = allowed_entries(root, state, profile)
    assessment(report, state, paths)
    release = engine()
    tree = git(root, "write-tree")
    with in_checkout(root):
        digest = release.source_digest(value["base"], tree)
        fingerprints = release.source_files(value["base"], tree)
    for path in paths:
        target = root / path
        mode = git(root, "ls-files", "--stage", "--", path).split()[:1]
        if target.is_symlink() or target.parent.is_symlink() or mode != ["100644"]:
            raise ValueError("Impact entry must remain an ordinary JSON file")
        if target.stat().st_size > 100_000:
            raise ValueError("Impact entry too large")
        entry = json.loads(target.read_text())
        entry.update(source_digest=digest, source_files=fingerprints)
        release.validate_entry(entry)
        if path != f"{NOTES}{entry['id']}.json":
            raise ValueError("Impact entry id must match its filename")
        target.write_text(json.dumps(entry, indent=2) + "\n")
        git(root, "add", "--", path)
    check(root, report, state, profile)


def application_findings(findings):
    return [f for f in findings if not (f.get("kind") == "ci" and f.get("target") == TARGET)]


def needs_application_checks(paths, tests, findings):
    return bool(tests or any(not p.startswith(NOTES) for p in paths)
                or any(f.get("kind") == "ci" for f in application_findings(findings)))
