#!/usr/bin/env python3
"""Customer impact validation and deterministic release rendering (stdlib only)."""

import argparse
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

NOTES = ".release-notes/"
KINDS = ["internal", "added", "improved", "fixed", "security", "deprecated"]
IMPACTS = ["none", "compatible", "behavior", "breaking"]
AVAILABILITY = ["on_deploy", "feature_flag", "coordinated"]
SOURCES = {
    "KanopyAI/kanopy-backend": ("backend", "API and platform"),
    "KanopyAI/kanopy-frontend": ("frontend", "Web app"),
    "KanopyAI/Powerline_3D": ("powerline", "Processing and analysis"),
    "KanopyAI/Kanopy-ios-app": ("ios", "iOS app"),
}
IDENTITY = r"(?:backend|frontend|powerline)-pr-\d+|ios-(?:pr|run)-\d+"
API_PATHS = (
    "app/api/", "app/schemas/", "app/core/", "app/models/",
    "app/services/auth", "app/services/accounts/", "app/services/storage/",
    "app/services/jobs/", "app/services/outputs/", "worker/storage_tasks.py",
    "openapi", "docs/api", "scripts/export_openapi",
)


def string(enum=None):
    return {"type": "string", **({"enum": enum} if enum else {})}


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


LEGACY_ENTRY_SCHEMA = object_schema({
    "id": string(), "source_digest": string(), "type": string(KINDS),
    "source_files": {"type": "array", "items": object_schema({"path": string(), "blob": string()})},
    "summary": string(), "audience": string(), "customer_action": string(),
    "api": object_schema({"impact": string(IMPACTS), "assessment": string(),
                          "endpoints": {"type": "array", "items": string()}}),
    "availability": string(AVAILABILITY), "rollout": string(),
    "data_effect": string(),
    "dependencies": {"type": "array", "items": string()},
    "notice_url": string(), "notice_date": string(), "effective_date": string(),
})

# Keep old source assessments valid and published history intact. New drafts
# separate the private impact assessment from the customer-facing notice.
ENTRY_SCHEMA = object_schema({**LEGACY_ENTRY_SCHEMA["properties"],
    "developer_notice": object_schema({
        "publish": {"type": "boolean"}, "reason": string(), "summary": string(),
        "action": string(), "availability": string(), "data_effect": string(),
    }),
})


def validate_shape(value, schema, path="entry"):
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            raise ValueError(f"{path}: expected fields {', '.join(schema['properties'])}")
        for key, sub in schema["properties"].items():
            validate_shape(value[key], sub, f"{path}.{key}")
    elif kind == "array":
        if not isinstance(value, list) or len(value) > 2000:
            raise ValueError(f"{path}: expected a bounded list")
        for item in value:
            validate_shape(item, schema["items"], path)
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path}: expected a boolean")
    elif not isinstance(value, str) or len(value) > 4000:
        raise ValueError(f"{path}: expected text of at most 4,000 characters")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unexpected value")


def validate_entry(entry, *, release=False, today=None, require_notice=False):
    if require_notice and "developer_notice" not in entry:
        raise ValueError("New impact entries require developer_notice; historical entries remain valid")
    schema = ENTRY_SCHEMA if "developer_notice" in entry else LEGACY_ENTRY_SCHEMA
    if "supersedes" in entry:
        schema = object_schema({**schema["properties"], "supersedes": string()})
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", entry["supersedes"]) or entry["supersedes"] == entry.get("id"):
            raise ValueError("A notice correction must name a different existing entry")
        if "developer_notice" not in entry:
            raise ValueError("A notice correction requires developer_notice")
    validate_shape(entry, schema)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", entry["id"]):
        raise ValueError("Use a stable lowercase slug for id")
    if not re.fullmatch(r"[a-f0-9]{64}", entry["source_digest"]):
        raise ValueError("Missing source digest: run the stamp command")
    if any(not re.fullmatch(r"[a-f0-9]{40}|deleted", f["blob"]) for f in entry["source_files"]):
        raise ValueError("Invalid source file fingerprint; run stamp")
    for field in ("summary", "audience", "customer_action", "rollout", "data_effect"):
        if not entry[field].strip():
            raise ValueError(f"{field} must explain the impact, including explicit 'None' where appropriate")
    if len(entry["api"]["assessment"].strip()) < 20:
        raise ValueError("Explain API behavior, authentication, permissions, links and storage impact")
    if entry["type"] == "internal" and entry["api"]["impact"] != "none":
        raise ValueError("An API behavior change cannot be classified as internal")
    if entry["availability"] == "coordinated" and not entry["dependencies"]:
        raise ValueError("Coordinated releases must name their dependencies")
    if "developer_notice" in entry:
        notice = entry["developer_notice"]
        requires_action = entry["customer_action"].strip().lower() not in {
            "none", "none.", "no action required", "no action required.",
        }
        if not notice["reason"].strip():
            raise ValueError("Explain the developer notice publication decision")
        if not notice["publish"] and (entry["api"]["impact"] in {"behavior", "breaking"}
                                      or (entry["api"]["impact"] != "none" and requires_action)
                                      or entry["type"] == "deprecated" or entry["notice_url"]):
            raise ValueError("API behavior changes, required customer action and advance notices require a developer notice")
        if notice["publish"]:
            if entry["type"] == "internal":
                raise ValueError("Internal changes cannot publish a developer notice")
            if not notice["summary"].strip() or len(notice["summary"]) > 280:
                raise ValueError("Developer notice summary must be 1–280 characters")
            for field in ("action", "availability", "data_effect"):
                if len(notice[field]) > 480:
                    raise ValueError(f"Developer notice {field} must be at most 480 characters")
            if requires_action and not notice["action"].strip():
                raise ValueError("A developer notice must retain required customer action")
            if (entry["availability"] != "on_deploy" or entry["dependencies"]) and not notice["availability"].strip():
                raise ValueError("A developer notice must explain restricted availability")
    notice = [entry[k] for k in ("notice_url", "notice_date", "effective_date")]
    if entry["api"]["impact"] == "breaking" or entry["type"] == "deprecated" or any(notice):
        if not all(notice):
            raise ValueError("Breaking changes/deprecations need a notice URL, notice date and effective date")
        url = urllib.parse.urlparse(entry["notice_url"])
        if (url.scheme != "https" or url.username or url.port not in {None, 443}
                or not ((url.hostname == "app.kanopy-ai.com" and url.path == "/updates")
                        or (url.hostname == "status.kanopy-ai.com" and url.path == "/changelog.html"))):
            raise ValueError("Notice must link to Kanopy Product updates or its legacy changelog redirect")
        start, end = date.fromisoformat(entry["notice_date"]), date.fromisoformat(entry["effective_date"])
        if start >= end:
            raise ValueError("The notice must precede the effective date")
        if entry["customer_action"].strip().lower() in {"none", "none.", "no action required"}:
            raise ValueError("Explain the migration or action for this planned change")
        if release and (today or date.today()) < end:
            raise ValueError(f"Cannot deploy before announced effective date {end}")
    return entry


def git(*args, cwd=None):
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd,
        stderr=subprocess.PIPE,
    ).decode()


def commit(ref):
    return git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def changed_paths(base, head):
    return [p for p in git("diff", "--name-only", "-z", base, head, "--").split("\0") if p]


def source_diff(base, head):
    return git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index", base, head,
               "--", ".", f":(exclude){NOTES}*")


def source_digest(base, head):
    return hashlib.sha256(source_diff(base, head).encode()).hexdigest()


def source_files(base, head):
    files = []
    for path in changed_paths(base, head):
        if path.startswith(NOTES):
            continue
        result = subprocess.run(["git", "rev-parse", "--verify", f"{head}:{path}"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        files.append({"path": path, "blob": result.stdout.strip() if result.returncode == 0 else "deleted"})
    return files


def read_entries(base, head, *, feature=False, release=False, reconcile=None):
    """Validate exact source coverage; only a trusted merged branch can import notes.

    Reconciliation never exempts new source from review. An authored entry must
    still describe the entire feature delta, including the combined file blobs.
    """
    if reconcile:
        reconcile = commit(reconcile)
        if subprocess.run(["git", "merge-base", "--is-ancestor", reconcile, head],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise ValueError("Reconciliation source must be an ancestor of the candidate")
    paths = changed_paths(base, head)
    note_paths = [p for p in paths if p.startswith(NOTES) and p.endswith(".json")]
    entries = []
    seen = set()
    digest = source_digest(base, head) if feature else None
    authored = []
    for path in note_paths:
        # Entries are append-only. Corrections use a new entry, preserving the
        # historical customer record even when the original change is reverted.
        if subprocess.run(["git", "cat-file", "-e", f"{base}:{path}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise ValueError(f"Do not change an existing release entry: {path}")
        mode = git("ls-tree", head, "--", path).split()[0:1]
        if mode != ["100644"]:
            raise ValueError(f"Release entry must be an ordinary JSON file: {path}")
        raw = git("show", f"{head}:{path}")
        inherited = False
        if reconcile:
            previous = subprocess.run(["git", "show", f"{reconcile}:{path}"],
                                      capture_output=True, text=True)
            inherited = previous.returncode == 0 and previous.stdout == raw
        entry = validate_entry(json.loads(raw), release=release,
                               require_notice=feature and not inherited)
        if path != f"{NOTES}{entry['id']}.json" or entry["id"] in seen:
            raise ValueError("Release entry ids must be unique and match their filenames")
        if feature and not inherited and entry["source_digest"] != digest:
            raise ValueError(f"{path}: customer impact is stale; review it and run stamp")
        if not inherited:
            authored.append(entry)
        seen.add(entry["id"])
        entries.append(entry)
    source_paths = [p for p in paths if not p.startswith(NOTES)]
    if source_paths and not entries:
        raise ValueError("This change needs a customer-impact entry, including an explicit internal assessment")
    if feature and reconcile and source_paths and not authored:
        raise ValueError("Reconciliation needs a new impact assessment of the combined source changes")
    for entry in entries:
        if "supersedes" not in entry:
            continue
        original = validate_entry(json.loads(git("show", f"{head}:{NOTES}{entry['supersedes']}.json")))
        # This is an editorial correction, never permission to hide a behavioral
        # change, customer action, notice deadline, or deployment restriction.
        for field in ("type", "summary", "audience", "customer_action", "api", "availability",
                      "rollout", "data_effect", "dependencies", "notice_url", "notice_date", "effective_date"):
            if entry[field] != original[field]:
                raise ValueError(f"Notice correction must preserve the original assessment: {field}")
        if "supersedes" in original:
            raise ValueError("Notice corrections cannot form chains")
        if not feature and original["id"] not in seen:
            raise ValueError("Notice correction targets an entry already in the release baseline; "
                             "published notices need a new assessment, not an unpublished correction")
    targets = {e["supersedes"] for e in entries if "supersedes" in e}
    if targets:
        # Include records already on the feature base. Checking only this diff
        # would let a second correction pass feature CI and fail on promotion.
        recorded_targets = set()
        for path in git("ls-tree", "-r", "--name-only", head, "--", NOTES).splitlines():
            if not path.endswith(".json"):
                continue
            record = json.loads(git("show", f"{head}:{path}"))
            target = record.get("supersedes")
            if target not in targets:
                continue
            if target in recorded_targets:
                raise ValueError("Multiple notice corrections target the same entry")
            recorded_targets.add(target)
    coverage_entries = authored if feature and reconcile else entries
    covered = {(f["path"], f["blob"]) for e in coverage_entries for f in e["source_files"]}
    uncovered = [f["path"] for f in source_files(base, head) if (f["path"], f["blob"]) not in covered]
    if uncovered:
        details = []
        for f in source_files(base, head):
            if f["path"] in uncovered:
                recorded = [f"{e['id']}={item['blob']}" for e in entries for item in e["source_files"]
                            if item["path"] == f["path"]]
                details.append(f"  {f['path']}: candidate={f['blob']}; recorded={', '.join(recorded) or 'none'}")
        raise ValueError("Changes missing a current impact assessment: " + ", ".join(uncovered)
                         + f"\nBaseline: {base}\nCandidate: {head}\n" + "\n".join(details)
                         + "\nReconcile staging hotfixes into dev, review the combined source, and stamp a new entry.")
    return entries


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Refusing an authenticated API redirect")


class GitHub:
    def __init__(self, repo, token):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError("Invalid repository")
        self.repo, self.token = repo, token

    def api(self, path, data=None, method=None):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}/{path}",
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "User-Agent": "kanopy-release-notes"})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"GitHub {method or 'GET'} {self.repo}/{path}: HTTP {error.code}") from None

    def pages(self, path, key=None):
        for page in range(1, 101):
            result = self.api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            batch = result[key] if key else result
            yield from batch
            if len(batch) < 100:
                return
        raise RuntimeError("GitHub pagination exceeded limit; refusing a partial release")

    def file(self, path, ref):
        import base64
        data = self.api(f"contents/{path}?ref={urllib.parse.quote(ref, safe='')}")
        if data.get("encoding") != "base64" or data.get("type") != "file":
            raise ValueError("Expected a small regular GitHub file")
        return json.loads(base64.b64decode(data["content"]))


def successful_deployments(gh):
    runs = list(gh.pages("actions/workflows/deploy.yml/runs?branch=main&status=success", "workflow_runs"))
    return sorted((r for r in runs if r["head_branch"] == "main"
                   and r["event"] in {"push", "workflow_dispatch"}),
                  key=lambda r: r["run_started_at"], reverse=True)


def source_config():
    config = json.loads((Path(__file__).resolve().parents[1] / "release-notes-source.json").read_text())
    if config["repository"] not in SOURCES or not re.fullmatch(r"[a-f0-9]{40}", config["tracking_start_sha"]):
        raise ValueError("Invalid release tracking configuration")
    return config


def coverage_base(gh, head, current=None, *, verified=None):
    """A declared tracking cutover is coverage, never evidence of deployment.

    `verified` is (sha, url) of a release proven outside CI (an App Store
    release); coverage then starts after changes customers already have."""
    config = source_config()
    if config["repository"] != gh.repo:
        raise ValueError("Release configuration belongs to another repository")
    start = config["tracking_start_sha"]
    fetch_sha(gh.repo, start)
    fetch_sha(gh.repo, head)
    common = git("merge-base", start, head).strip()
    if common != start and git("rev-parse", f"{common}^{{tree}}") != git("rev-parse", f"{start}^{{tree}}"):
        raise ValueError("Candidate predates tracking cutover or has nonlinear history")
    runs = [] if SOURCES[gh.repo][0] == "ios" else successful_deployments(gh)
    base, baseline_url = start, ""
    if verified:
        fetch_sha(gh.repo, verified[0])
        common = git("merge-base", start, verified[0]).strip()
        if common != start and git("rev-parse", f"{common}^{{tree}}") != git("rev-parse", f"{start}^{{tree}}"):
            raise ValueError("Verified release predates tracking cutover or has nonlinear history")
        base, baseline_url = verified
    since = int(git("show", "-s", "--format=%ct", start).strip())
    # Replay successful promotions in order. A partial Powerline run advances
    # coverage only if it promotes every service changed since the proven base.
    for run in reversed(runs):
        if datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00")).timestamp() < since:
            continue
        if current and (run["id"] == current["id"] or run["run_started_at"] >= current["run_started_at"]):
            continue
        fetch_sha(gh.repo, run["head_sha"])
        common = git("merge-base", base, run["head_sha"]).strip()
        if common != base and git("rev-parse", f"{common}^{{tree}}") != git("rev-parse", f"{base}^{{tree}}"):
            if baseline_url:
                raise ValueError("Nonlinear production history/rollback requires explicit release review")
            continue
        required = processing_services(changed_paths(base, run["head_sha"]))
        if verify_deployment(gh, run, required_services=required):
            base, baseline_url = run["head_sha"], run["html_url"]
    common = git("merge-base", base, head).strip()
    if common != base and git("rev-parse", f"{common}^{{tree}}") != git("rev-parse", f"{base}^{{tree}}"):
        raise ValueError("Nonlinear production history/rollback requires explicit release review")
    return base, baseline_url


PROCESSING_SERVICES = {
    "powerline_orchestrator": "orchestrator", "powerline_data_prep": "data-prep",
    "powerline_reconstruction": "reconstruct", "powerline_segmentation": "segmentation",
    "powerline_clustering": "clustering", "powerline_analysis": "analysis",
}


def processing_services(paths):
    required = set()
    for path in paths:
        if path.startswith("containers/shared/"):
            return set(PROCESSING_SERVICES.values())
        if path.startswith("containers/Dockerfile.base.py311"):
            required.update({"reconstruct", "clustering"})
        elif path.startswith("containers/Dockerfile.base.py312"):
            required.add("segmentation")
        for directory, service in PROCESSING_SERVICES.items():
            if path.startswith(f"containers/{directory}/"):
                required.add(service)
    return required


def verify_deployment(gh, run, *, required_services=None):
    if (run.get("conclusion") != "success" or run.get("head_branch") != "main"
            or run.get("status") != "completed" or run.get("path") != ".github/workflows/deploy.yml"):
        return False
    jobs = list(gh.pages(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"))
    component = SOURCES[gh.repo][0]
    names = {j["name"] for j in jobs if j["conclusion"] == "success"}
    if component == "backend":
        return "Promote verified image → prod" in names
    if component == "frontend":
        return "Build verified source & deploy → prod" in names
    if component == "powerline":
        services = set(PROCESSING_SERVICES.values()) if required_services is None else required_services
        # A source-only promotion still runs candidate/impact validation. This
        # evidence cannot cover services left undeployed by an earlier run:
        # callers compute required_services from the last verified baseline.
        if not services and {"Verify promotion candidate", "Verify promotion without service changes"} <= names:
            return True
        return ("Refresh pipeline pod → prod" in names and
                any(name.startswith("Promote & deploy → prod (") for name in names) and all(
            any(name.startswith("Promote & deploy → prod (") and
                re.search(r"(?:[ (,])" + re.escape(service) + r"(?:[ ,)])", name)
                for name in names) for service in services))
    return False  # iOS uses App Store Connect evidence, never a CI success.


def deployment_baseline(runs, current=None):
    candidates = [r for r in runs if current is None or
                  (r["id"] != current["id"] and r["run_started_at"] < current["run_started_at"])]
    if not candidates:
        raise ValueError("No successful production deployment baseline; do not guess from main")
    return candidates[0]


def customer_visible(entry):
    if entry["type"] == "internal":
        return False
    notice = entry.get("developer_notice")
    if notice is not None:
        return notice["publish"]
    # Older assessments still in flight have no editorial decision. Keep API
    # impact and advance notices, but omit routine UI-only releases.
    return entry["api"]["impact"] != "none" or entry["type"] == "deprecated" or bool(entry["notice_url"])


def customer_entries(entries):
    superseded = {e["supersedes"] for e in entries if "supersedes" in e}
    return [e for e in entries if e["id"] not in superseded and customer_visible(e)]


def public_change(entry, deployed=False):
    ready = deployed and entry["availability"] == "on_deploy" and not entry["dependencies"]
    notice = entry.get("developer_notice")
    if notice is not None:
        return {"type": entry["type"], "body": notice["summary"],
                "audience": "", "api_impact": entry["api"]["impact"],
                "customer_action": notice["action"], "data_effect": notice["data_effect"],
                "availability": "" if ready else notice["availability"],
                "dependencies": [], "notice_url": entry["notice_url"],
                "effective_date": entry["effective_date"]}
    return {"type": entry["type"], "body": entry["summary"],
            "audience": entry["audience"], "api_impact": entry["api"]["impact"],
            "customer_action": entry["customer_action"],
            "data_effect": entry["data_effect"],
            "availability": "Available" if ready else entry["rollout"],
            "dependencies": entry["dependencies"], "notice_url": entry["notice_url"],
            "effective_date": entry["effective_date"]}


def public_bundle(manifest):
    title = SOURCES[manifest["repository"]][1]
    return {"id": manifest["id"], "title": f"Upcoming {title} changes",
            "status": "planned", "timing": ("Uploaded to TestFlight; App Store release not confirmed"
                if manifest["state"] == "testflight" else "Release date not scheduled"),
            "changes": [public_change(e) for e in customer_entries(manifest["entries"])]}


def update_public(manifest, upcoming, releases, *, today=None):
    """Pure, idempotent transformation. Only a verified deployment creates history."""
    upcoming, releases = json.loads(json.dumps(upcoming)), json.loads(json.dumps(releases))
    identity = manifest["id"]
    upcoming["releases"] = [r for r in upcoming.get("releases", []) if r["id"] != identity]
    visible = customer_entries(manifest["entries"])
    if any(r.get("source_id") == identity for r in releases["releases"]):
        return upcoming, releases  # A delayed event must never regress a published release.
    if manifest["state"] == "deployed":
        if not manifest.get("deployment", {}).get("verified"):
            raise ValueError("Released status requires verified deployment evidence")
        if visible and not any(r.get("source_id") == identity for r in releases["releases"]):
            day = today or date.today()
            prefix = day.strftime("%Y.%m.")
            versions = [int(r["version"].removeprefix(prefix)) for r in releases["releases"]
                        if re.fullmatch(re.escape(prefix) + r"\d+", r["version"])]
            releases["releases"].insert(0, {
                "version": prefix + str(max(versions, default=0) + 1), "date": day.isoformat(),
                "source_id": identity, "deployed_at": manifest["deployment"]["completed_at"],
                "title": SOURCES[manifest["repository"]][1] + " update", "summary": "",
                "changes": [public_change(e, deployed=True) for e in visible],
            })
    elif visible:
        upcoming["releases"].append(public_bundle(manifest))
    return upcoming, releases


def render_brief(manifest):
    lines = ["# Release review", "", f"Status: **{manifest['state']}**. Publication requires review.",
             f"Component: {SOURCES[manifest['repository']][1]}",
             f"Coverage baseline: `{manifest['base_sha']}`", f"Candidate: `{manifest['head_sha']}`", "",
             ("Baseline deployment: " + manifest["baseline_run"] if manifest.get("baseline_run") else
              "Coverage begins at the declared adoption commit. Earlier changes and production state are not reconstructed."), "",
             "## Customer changes", ""]
    for entry in manifest["entries"]:
        if entry["type"] == "internal":
            continue
        lines.extend([f"### {entry['summary']}", "", f"Affected: {entry['audience']}",
                      f"API impact: {entry['api']['impact']} — {entry['api']['assessment']}",
                      f"Customer action: {entry['customer_action']}",
                      f"Existing data: {entry['data_effect']}",
                      f"Availability: {entry['rollout']}", ""])
        if entry["dependencies"]:
            lines.extend(["Dependencies: " + "; ".join(entry["dependencies"]), ""])
        if "developer_notice" in entry:
            notice = entry["developer_notice"]
            lines.extend([f"Developer notice: {'include' if notice['publish'] else 'omit'} — {notice['reason']}", ""])
            if notice["publish"]:
                lines.extend([notice["summary"], notice["action"], notice["availability"], notice["data_effect"], ""])
    lines.extend(["## Internal changes", ""] +
                 [f"- {e['summary']}" for e in manifest["entries"] if e["type"] == "internal"])
    lines.extend(["", "## Publication review", "",
                  "Confirm customer wording, API impact, actual rollout/feature flags, dependencies, and any advance notice.",
                  "The date in releases.json is the intended publication date; update it if review finishes on a later day.",
                  "Do not merge an advance notice after its promised effective date.", ""])
    return "\n".join(lines)


def fetch_sha(repo, sha):
    import base64
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Expected an immutable commit SHA")
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return
    env = dict(os.environ)
    if env.get("GH_TOKEN"):
        credential = base64.b64encode(("x-access-token:" + env["GH_TOKEN"]).encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + credential)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "fetch", "--no-tags",
                    f"https://github.com/{repo}.git", sha], env=env, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def check_candidate(head, *, base=None, feature=False, release=False, reconcile=None):
    head = commit(head)
    baseline_url = ""
    if not base:
        gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        base, baseline_url = coverage_base(gh, head)
    base = commit(base)
    if feature:
        base = git("merge-base", base, head).strip()
    # Print provenance before validation, including on a failing check.
    print(f"Impact baseline: {base}\nImpact candidate: {head}", flush=True)
    if baseline_url:
        print(f"Baseline deployment: {baseline_url}", flush=True)
    entries = read_entries(base, head, feature=feature, release=release, reconcile=reconcile)
    print(f"Customer impact checked: {len(entries)} entries against {base}")
    return entries


def preflight(head, target, *, base=None, run=()):
    """Check the actual combined tree without changing the caller's checkout."""
    head, target = commit(head), commit(target)
    root = git("rev-parse", "--show-toplevel").strip()
    with tempfile.TemporaryDirectory(prefix="kanopy-promotion-") as temporary:
        checkout = str(Path(temporary) / "candidate")
        git("worktree", "add", "--detach", checkout, target)
        try:
            env = dict(os.environ, GIT_MERGE_AUTOEDIT="no",
                       GIT_AUTHOR_NAME="Promotion preflight", GIT_AUTHOR_EMAIL="preflight@example.invalid",
                       GIT_COMMITTER_NAME="Promotion preflight", GIT_COMMITTER_EMAIL="preflight@example.invalid")
            merged = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "merge",
                                     "--no-commit", "--no-ff", head], cwd=checkout, env=env,
                                    capture_output=True, text=True)
            if merged.returncode:
                raise ValueError("Promotion merge needs reconciliation:\n" + merged.stdout + merged.stderr)
            tree = git("write-tree", cwd=checkout).strip()
            candidate = subprocess.check_output(["git", "commit-tree", tree, "-p", target,
                                                  *([] if head == target else ["-p", head])],
                                                 input="Promotion preflight\n", text=True,
                                                 cwd=checkout, env=env).strip()
            # Git-aware tests/builds must see the candidate through HEAD too,
            # rather than the destination commit with an uncommitted merge.
            git("reset", "--hard", candidate, cwd=checkout)
            print(f"Promotion source: {head}\nPromotion target: {target}", flush=True)
            check_candidate(candidate, base=base)
            if run:
                # The optional command runs against precisely the tree checked above.
                test_env = {k: v for k, v in os.environ.items() if k not in {
                    "GH_TOKEN", "GITHUB_TOKEN", "RELEASE_NOTES_TOKEN", "REVIEW_FIXER_TOKEN", "OPENAI_API_KEY"}}
                subprocess.run(run, cwd=checkout, env=test_env, check=True, timeout=900)
        finally:
            git("worktree", "remove", "--force", checkout, cwd=root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "stamp", "preflight"])
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--feature", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--file")
    parser.add_argument("--reconcile", help="Trusted staging commit already merged into the feature head")
    parser.add_argument("--target", help="Promotion destination for preflight, e.g. origin/staging")
    parser.add_argument("--run", nargs=argparse.REMAINDER, help="Optional test command run in the merged candidate")
    args = parser.parse_args()
    if args.command == "preflight":
        if not args.target:
            parser.error("preflight requires --target")
        preflight(args.head, args.target, base=args.base, run=args.run)
        return
    if args.command == "check":
        check_candidate(args.head, base=args.base, feature=args.feature,
                        release=args.release, reconcile=args.reconcile)
        return
    base = args.base
    if not base:
        gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
        base, _ = coverage_base(gh, commit(args.head))
        # checkout fetch-depth: 0 normally includes the baseline. SHA lookup
        # deliberately fails if production history is unavailable.
    base, head = commit(base), commit(args.head)
    if args.feature:
        base = git("merge-base", base, head).strip()
    if args.command == "stamp":
        if not args.file:
            parser.error("stamp requires --file")
        path = Path(args.file)
        entry = json.loads(path.read_text())
        # Stamp the working tree, so agents can do this before committing.
        patch = git("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--full-index", base,
                    "--", ".", f":(exclude){NOTES}*")
        entry["source_digest"] = hashlib.sha256(patch.encode()).hexdigest()
        # The index records new files too. Refuse unstaged source changes: their
        # fingerprints would not describe the commit about to be reviewed.
        unstaged = git("diff", "--name-only", "--", ".", f":(exclude){NOTES}*").strip()
        if unstaged:
            raise ValueError("Stage all source changes before stamping the impact entry")
        index_tree = git("write-tree").strip()
        entry["source_files"] = source_files(base, index_tree)
        validate_entry(entry, require_notice=True)
        path.write_text(json.dumps(entry, indent=2) + "\n")


if __name__ == "__main__":
    main()
