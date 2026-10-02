"""Repository-specific setup and verification, executed from the trusted checkout."""

from contextlib import contextmanager
import fnmatch
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


SERVICES = {
    "powerline_analysis", "powerline_clustering", "powerline_reconstruction",
    "powerline_data_prep", "powerline_orchestrator", "powerline_segmentation", "shared",
}


VALIDATION_LOG = None
BACKEND_SHARDS = {
    "core-auth": ["tests/accounts", "tests/auth", "tests/embeds", "tests/security", "tests/test_*.py"],
    "jobs-storage": ["tests/jobs", "tests/storage", "tests/uploads"],
    "tracking-powerline": ["tests/tracking", "tests/powerline", "tests/gps", "tests/clearance"],
    "services-data": ["tests/ai", "tests/clients", "tests/datasets", "tests/db", "tests/projects", "tests/services", "tests/worker"],
}


def log_tail(path, limit=12_000):
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit).decode("utf-8", errors="replace")


@contextmanager
def capture_validation(path):
    global VALIDATION_LOG
    previous = VALIDATION_LOG
    VALIDATION_LOG = path
    path.write_text("")
    try:
        yield
    finally:
        VALIDATION_LOG = previous


def run(command, root, **kwargs):
    command = [str(arg) for arg in command]
    if command[0] in {"npm", "npx"} and os.environ.get("REVIEW_NODE_BIN"):
        env = dict(kwargs.pop("env", os.environ))
        env["PATH"] = os.environ["REVIEW_NODE_BIN"] + os.pathsep + env["PATH"]
        kwargs["env"] = env
    if VALIDATION_LOG is None:
        subprocess.run(command, cwd=root, check=True, timeout=2400, **kwargs)
        return
    print("Validating: " + shlex.join(command), flush=True)
    with VALIDATION_LOG.open("a") as stream:
        stream.write("\n$ " + shlex.join(command) + "\n")
        stream.flush()
        try:
            subprocess.run(command, cwd=root, check=True, timeout=2400,
                           stdout=stream, stderr=subprocess.STDOUT, **kwargs)
        finally:
            # The full output stays in the local diagnostic; show a bounded tail.
            print(log_tail(VALIDATION_LOG), flush=True)


def ci_targets(findings):
    targets = {f["target"] for f in findings if f.get("kind") == "ci"}
    if any(not isinstance(t, str) or len(t.split(":")) != 2 or not all(t.split(":")) for t in targets):
        raise ValueError("CI policy targets must use a nonempty project:check format; fix the trusted policy manually")
    return targets


def sdk_python(version):
    return Path(os.environ["RUNNER_TEMP"]) / "review-autofix-sdk" / version / "bin/python"


def setup_sdk_version(root, version):
    if version not in {"3.10", "3.11", "3.12", "3.13", "3.14"}:
        raise ValueError("Unsupported SDK Python version")
    python = sdk_python(version)
    if python.exists():
        return
    interpreter = shutil.which("python" + version)
    if not interpreter:
        raise RuntimeError(f"Python {version} is unavailable for reproducing CI")
    run([interpreter, "-m", "venv", python.parent.parent], root)
    run([python, "-m", "pip", "install", "-e", ".[dev]", "twine"], root)


def service_for(path):
    parts = Path(path.split("::")[0]).parts
    if len(parts) >= 3 and parts[0] == "containers" and parts[1] in SERVICES:
        return parts[1]
    if len(parts) >= 3 and parts[:2] == ("containers", "tests"):
        return "powerline_data_prep"
    raise ValueError(f"No CPU test environment is configured for {path}")


def service_python(service):
    return Path(os.environ["RUNNER_TEMP"]) / "review-autofix-venvs" / service / "bin/python"


def setup_service(root, service):
    python = service_python(service)
    if python.exists():
        return
    interpreter = (os.environ.get("REVIEW_SEGMENTATION_PYTHON") or sys.executable
                   if service == "powerline_segmentation" else sys.executable)
    run([interpreter, "-m", "venv", python.parent.parent], root)
    try:
        run([python, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], root)
        if service in {"powerline_analysis", "powerline_clustering", "powerline_reconstruction", "powerline_segmentation"}:
            packages = ["torch", "torchvision"] if service == "powerline_reconstruction" else ["torch"]
            run([python, "-m", "pip", "install", *packages, "--index-url", "https://download.pytorch.org/whl/cpu"], root)
        req = "containers/shared/dependencies/requirements.txt" if service == "shared" else f"containers/{service}/requirements.txt"
        run([python, "-m", "pip", "install", "-r", root / req, "pytest"], root)
        if service == "shared":
            run([python, "-m", "pip", "install", "numpy", "scipy", "pyproj", "plyfile", "pyyaml", "opencv-python-headless"], root)
        if service == "powerline_data_prep":
            run([python, "-m", "pip", "install", "scipy", "plyfile<1.1.4"], root)
    except Exception:
        # The job fails, rather than reusing a partially installed environment.
        raise


def setup(root, context, profile):
    kind = profile["kind"]
    targets = ci_targets(context["findings"])
    if kind == "backend":
        run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools>=83.0.0", "wheel>=0.46.2"], root)
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "pytest", "plyfile"], root)
    elif kind == "python":
        run([sys.executable, "-m", "pip", "install", "-e", ".[dev]", "twine"], root)
        for target in targets:
            if target.startswith("python:"):
                version = target.split(":")[1]
                setup_sdk_version(root, version if version.startswith("3.") else "3.13")
    elif kind == "frontend":
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js must be installed by the workflow before frontend setup")
        if os.environ.get("GITHUB_ENV"):
            with open(os.environ["GITHUB_ENV"], "a") as stream:
                stream.write(f"REVIEW_NODE_BIN={Path(node).parent}\n")
        run(["npm", "ci"], root)
        run(["npm", "ci", "--prefix", "packages/kanopy-embeds"], root)
        # Any repair may touch embeds source; install before sudo is removed.
        run(["npx", "playwright", "install", "--with-deps", "chromium"], root)
    elif kind == "infra":
        run([sys.executable, "-m", "pip", "install", "boto3", "redis==5.0.1", "pytest"], root)
        # A syntax error in the PR can prevent init. Let the agent repair the
        # Terraform source first; the verifier still requires init and validate.
        if "infra:terraform" not in targets:
            run(["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"], root)
    elif kind == "ios":
        run(["xcodebuild", "-resolvePackageDependencies", "-project", "kanopy-ios-app.xcodeproj", "-scheme", "KanopyAI"], root)
    elif kind == "powerline":
        services = {target.split(":", 1)[1] for target in targets if target.startswith("powerline:")}
        for finding in context["findings"]:
            try:
                services.add(service_for(finding["path"]))
            except ValueError:
                # Non-service findings still need investigation; they must not
                # prevent valid service findings from reaching the agent.
                continue
        for service in sorted(services):
            setup_service(root, service)
    else:
        raise ValueError(f"Unsupported project kind: {kind}")


def verify(root, tests, changed, profile, findings=()):
    kind = profile["kind"]
    targets = ci_targets(findings)
    if kind == "backend":
        selected = list(tests)
        for target in sorted(targets):
            shard = target.removeprefix("backend:")
            if shard not in BACKEND_SHARDS:
                raise ValueError("Unsupported backend CI target")
            for pattern in BACKEND_SHARDS[shard]:
                matches = [str(p.relative_to(root)) for p in sorted(root.glob(pattern))]
                selected.extend(matches or [pattern])
        if not any(not t.startswith("--") for t in selected):
            raise ValueError("No backend tests selected")
        run([sys.executable, "-m", "pytest", *dict.fromkeys(selected), "-q"], root)
    elif kind == "python":
        if targets:
            versions = {target.split(":")[1] for target in targets if target.split(":")[1].startswith("3.")}
            for version in sorted(versions):
                run([sdk_python(version), "-m", "pytest", "tests", "-q"], root)
            # Quality/package failures still exercise the application tests.
            if not versions or tests:
                run([sys.executable, "-m", "pytest", *(tests or ["tests"]), "-q"], root)
        else:
            run([sys.executable, "-m", "pytest", *tests, "-q"], root)
        quality_python = sdk_python("3.13") if {"python:quality", "python:package"} & targets else sys.executable
        run([quality_python, "-m", "ruff", "check", "src", "tests", "scripts"], root)
        run([quality_python, "-m", "ruff", "format", "--check", "src", "tests", "scripts"], root)
        if "python:quality" in targets:
            run([quality_python, "scripts/check_version.py"], root)
        if "python:package" in targets:
            run([quality_python, "-m", "build"], root)
            run([quality_python, "-m", "twine", "check", *sorted((root / "dist").glob("*"))], root)
            run([quality_python, "scripts/check_dist.py", "dist"], root)
            run([quality_python, "-m", "pip", "install", "--force-reinstall", *sorted((root / "dist").glob("*.whl"))], root)
            run([quality_python, "-c", "import importlib.metadata, kanopy; assert kanopy.__version__ == importlib.metadata.version('kanopy-ai')"], root)
    elif kind == "frontend":
        run(["npm", "test"], root)
        run(["npm", "run", "typecheck"], root)
        embeds = ("frontend:embeds" in targets or any(
            p.startswith(("packages/kanopy-embeds/", "src/embed/"))
            or fnmatch.fnmatchcase(p, "src/components/viewers/ReconstructionViewer.*") for p in changed))
        if embeds:
            run(["npm", "--prefix", "packages/kanopy-embeds", "run", "typecheck"], root)
        if embeds:
            run(["npm", "run", "test:embeds-e2e"], root)
            run(["npm", "--prefix", "packages/kanopy-embeds", "run", "pack:check"], root)
    elif kind == "infra":
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], root)
        # unittest discovery skips nested directories without __init__.py.
        # Run the accepted regressions explicitly as well, including pytest tests.
        run([sys.executable, "-m", "pytest", *(tests or ["tests"]), "-q"], root)
        run(["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"], root)
        run(["terraform", "validate", "-no-color"], root)
        for name in changed:
            if name.endswith(".tf") and (root / name).exists():
                run(["terraform", "fmt", "-check", "-diff", name], root)
    elif kind == "ios":
        devices = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "devices", "available", "--json"]))
        simulator = next((d["udid"] for runtime in devices["devices"].values()
                          for d in runtime if d.get("isAvailable") and "iPhone" in d["name"]), None)
        if not simulator:
            raise RuntimeError("No available iPhone simulator")
        common = ["-project", "kanopy-ios-app.xcodeproj", "-scheme", "KanopyAI",
                  "-configuration", "Debug", "-destination", f"platform=iOS Simulator,id={simulator}",
                  "-derivedDataPath", str(Path(os.environ["RUNNER_TEMP"]) / "ReviewDerivedData"),
                  "CODE_SIGNING_ALLOWED=NO", "COMPILER_INDEX_STORE_ENABLE=NO"]
        run(["xcodebuild", "test", *common], root)
        run(["xcodebuild", "analyze", *common], root)
    elif kind == "powerline":
        groups = {}
        for node in tests:
            groups.setdefault(service_for(node), []).append(node)
        for target in targets:
            service = target.split(":", 1)[1]
            if service not in SERVICES:
                raise ValueError("Unsupported Powerline CI target")
            nodes = groups.setdefault(service, [])
            nodes.append(f"containers/{service}/tests")
            if service == "powerline_analysis":
                nodes.append("containers/powerline_analysis/wire_fitting/tests")
            if service == "powerline_data_prep":
                nodes.append("containers/tests")
        for service, nodes in sorted(groups.items()):
            setup_service(root, service)
            service_root = root / "containers" / service
            selected = []
            for node in dict.fromkeys(nodes):
                path, separator, selector = node.partition("::")
                selected.append(os.path.relpath(root / path, service_root) + (separator + selector if separator else ""))
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([
                str(service_root), str(root / "containers"), str(root)]), ATEN_CPU_CAPABILITY="default")
            run([service_python(service), "-m", "pytest", *selected, "-q"], service_root, env=env)
    else:
        raise ValueError(f"Unsupported project kind: {kind}")
