"""Repository-specific setup and verification, executed from the trusted checkout."""

import json
import os
from pathlib import Path
import subprocess
import sys


SERVICES = {
    "powerline_analysis", "powerline_clustering", "powerline_reconstruction",
    "powerline_data_prep", "powerline_orchestrator", "powerline_segmentation", "shared",
}


def run(command, root, **kwargs):
    subprocess.run([str(arg) for arg in command], cwd=root, check=True, timeout=2400, **kwargs)


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
    if kind == "backend":
        run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools>=83.0.0", "wheel>=0.46.2"], root)
        run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "pytest", "plyfile"], root)
    elif kind == "python":
        run([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], root)
    elif kind == "frontend":
        run(["npm", "ci"], root)
        run(["npm", "ci", "--prefix", "packages/kanopy-embeds"], root)
    elif kind == "infra":
        run([sys.executable, "-m", "pip", "install", "boto3", "redis==5.0.1", "pytest"], root)
        run(["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"], root)
    elif kind == "ios":
        run(["xcodebuild", "-resolvePackageDependencies", "-project", "kanopy-ios-app.xcodeproj", "-scheme", "KanopyAI"], root)
    elif kind == "powerline":
        services = {service_for(f["path"]) for f in context["findings"]}
        for service in sorted(services):
            setup_service(root, service)
    else:
        raise ValueError(f"Unsupported project kind: {kind}")


def verify(root, tests, changed, profile):
    kind = profile["kind"]
    if kind in {"backend", "python"}:
        run([sys.executable, "-m", "pytest", *tests, "-q"], root)
        if kind == "python":
            run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"], root)
            run([sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "scripts"], root)
    elif kind == "frontend":
        run(["npm", "test"], root)
        run(["npm", "run", "typecheck"], root)
        if any(p.startswith("packages/kanopy-embeds/") for p in changed):
            run(["npm", "--prefix", "packages/kanopy-embeds", "run", "typecheck"], root)
    elif kind == "infra":
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], root)
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
        for service, nodes in sorted(groups.items()):
            setup_service(root, service)
            # Selectors remain relative to the repository; PYTHONPATH mirrors service CI.
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([
                str(root / "containers" / service), str(root / "containers"), str(root)]),
                ATEN_CPU_CAPABILITY="default")
            run([service_python(service), "-m", "pytest", *nodes, "-q"], root, env=env)
    else:
        raise ValueError(f"Unsupported project kind: {kind}")
