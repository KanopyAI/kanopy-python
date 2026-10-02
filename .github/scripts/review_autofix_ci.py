"""Collect bounded failure evidence from trusted, current-head Actions checks."""

from collections import deque
import fnmatch
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

LOG_TAIL_BYTES = 65_536
MAX_FINDINGS = 8


def redact(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,})\b", "[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    return text


class LogRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise ValueError("Log downloads require HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        # GitHub redirects to a signed blob URL; never forward the GitHub token.
        redirected.remove_header("Authorization")
        return redirected


def log_excerpt(gh, job_id):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{gh.repo}/actions/jobs/{int(job_id)}/logs",
        headers={"Authorization": "Bearer " + gh.token,
                 "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.build_opener(LogRedirect()).open(req, timeout=15) as response:
            return read_log_excerpt(response)
    except urllib.error.HTTPError as exc:
        return f"Job logs unavailable (HTTP {exc.code}); use the annotations and check summary."
    except (urllib.error.URLError, TimeoutError):
        return "Job logs temporarily unavailable; use the annotations and check summary."


def read_log_excerpt(response):
    # Stream the whole log with bounded memory, retaining its actual ending.
    # A wall-clock bound prevents a slow log service from blocking preparation.
    deadline = time.monotonic() + 30
    tail, partial = b"", b""
    errors = deque(maxlen=32)
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("Log download exceeded 30 seconds")
        chunk = response.read(LOG_TAIL_BYTES)
        if not chunk:
            break
        tail = (tail + chunk)[-LOG_TAIL_BYTES:]
        lines = (partial + chunk).split(b"\n")
        partial = lines.pop()[-2000:]
        for line in lines:
            if re.search(rb"error:|##\[error\]|FAILED |AssertionError|Traceback", line, re.I):
                errors.append(redact(line.decode("utf-8", errors="replace"))[:500])
    text = redact(tail.decode("utf-8", errors="replace"))
    return "Error lines:\n" + "\n".join(errors)[-3500:] + "\nLog tail:\n" + text[-7500:]


def bounded_annotations(annotations):
    result = []
    for annotation in annotations[:20]:
        item = {k: redact(str(annotation.get(k, "")))[:800]
                for k in ["path", "start_line", "title", "message"]}
        if len(json.dumps([*result, item], ensure_ascii=False)) > 4000:
            break
        result.append(item)
    return result


def target_for(profile, path, name):
    for pattern, target in profile.get("ci_workflows", {}).get(path, {}).items():
        if fnmatch.fnmatchcase(name, pattern):
            return target
    return None


def collect(gh, pr, checks, profile):
    sha = pr["head"]["sha"]
    failed = {c["id"]: c for c in checks
              if c.get("head_sha") == sha and c.get("status") == "completed"
              and c.get("conclusion") in {"failure", "timed_out"}
              and (c.get("app") or {}).get("slug") == "github-actions"}
    if not failed or not profile.get("ci_workflows"):
        return []
    runs = gh.get(f"actions/runs?head_sha={sha}&per_page=100")
    if runs["total_count"] > 100:
        raise RuntimeError("Too many workflow runs on this head; manual CI triage required")
    found = []
    for run in runs["workflow_runs"]:
        path = run["path"].split("@", 1)[0]
        if (run["head_sha"] != sha or run.get("head_branch") != pr["head"]["ref"]
                or run["status"] != "completed" or run.get("event") not in {"pull_request", "push", "workflow_dispatch"}
                or path not in profile["ci_workflows"]):
            continue
        jobs = gh.get(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
        if jobs["total_count"] > 100:
            raise RuntimeError("Too many CI jobs; manual CI triage required")
        for job in jobs["jobs"]:
            # Match an actual check returned for this exact PR head, not a name or URL supplied in text.
            url = job.get("check_run_url", "")
            check = next((c for c in failed.values()
                          if url == f"https://api.github.com/repos/{gh.repo}/check-runs/{c['id']}"), None)
            target = target_for(profile, path, job["name"])
            if (not check or not target or job.get("head_sha") != sha
                    or job["status"] != "completed" or job["conclusion"] not in {"failure", "timed_out"}):
                continue
            attempt = run.get("run_attempt", 1)
            key = hashlib.sha256(f"ci:{sha}:{job['id']}:{attempt}".encode()).hexdigest()
            annotations = gh.get(f"check-runs/{check['id']}/annotations?per_page=20")
            annotations = bounded_annotations(annotations)
            output = check.get("output") or {}
            found.append({
                "kind": "ci", "key": key, "path": "", "line": None,
                "reviewer": "github-actions", "url": job["html_url"],
                "workflow": path, "check": job["name"], "target": target,
                "manual_only": target in profile.get("ci_manual_targets", []),
                "run_id": run["id"], "run_attempt": attempt, "job_id": job["id"],
                "conclusion": job["conclusion"], "annotations": annotations,
                "summary": redact(str(output.get("summary") or ""))[:3000],
                "failed_steps": [s["name"] for s in job.get("steps", [])
                                 if s.get("conclusion") in {"failure", "timed_out"}],
                "log": log_excerpt(gh, job["id"]),
            })
            if len(found) == MAX_FINDINGS:
                return found
    return found


def safe_json(data):
    # Hidden bot-state JSON must not close its HTML comment or mention a bot.
    return (json.dumps(data, separators=(",", ":"), ensure_ascii=True)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("@", "\\u0040"))


def fit_json_string(text, budget, *, tail=False):
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        sample = text[-middle:] if tail else text[:middle]
        if len(safe_json(sample).encode()) <= budget:
            low = middle
        else:
            high = middle - 1
    return (text[-low:] if tail else text[:low]) if low else ""


def bounded_retry(data):
    if not isinstance(data, dict) or data.get("kind") != "validation" or not all(isinstance(data.get(k), str) for k in ["error", "log", "patch"]):
        raise ValueError("Invalid validation failure report")
    return {"kind": "validation", "error": fit_json_string(redact(data["error"]), 1000),
            "log": fit_json_string(redact(data["log"]), 6000, tail=True),
            "patch": fit_json_string(redact(data["patch"]), 16_000)}


def retry_details(path):
    """Validate bounded retry data before storing it in trusted bot state."""
    if path.stat().st_size > 40_000:
        raise ValueError("Oversized validation failure report")
    return bounded_retry(json.loads(path.read_text()))
