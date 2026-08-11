"""Fail closed if a built SDK distribution contains unexpected files."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

BLOCKED_NAMES = {".env", ".git", "__pycache__"}
BLOCKED_SUFFIXES = {".key", ".pem", ".pyc", ".pyo"}
REQUIRED_WHEEL_FILES = {
    "kanopy/__init__.py",
    "kanopy/client.py",
    "kanopy/errors.py",
    "kanopy/models.py",
    "kanopy/py.typed",
}
REQUIRED_LICENSE_FILES = {"LICENSE", "NOTICE"}


def unsafe(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(BLOCKED_NAMES.intersection(path.parts)) or path.suffix in BLOCKED_SUFFIXES
    )


def wheel_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def sdist_names(path: Path) -> set[str]:
    with tarfile.open(path, mode="r:gz") as archive:
        return set(archive.getnames())


dist_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
artifacts = sorted(dist_dir.iterdir()) if dist_dir.is_dir() else []
wheels = [path for path in artifacts if path.suffix == ".whl"]
sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
if len(wheels) != 1 or len(sdists) != 1 or len(artifacts) != 2:
    raise SystemExit(
        f"expected exactly one wheel and one sdist in {dist_dir}, found: "
        f"{[path.name for path in artifacts]}"
    )

for artifact, names in (
    (wheels[0], wheel_names(wheels[0])),
    (sdists[0], sdist_names(sdists[0])),
):
    blocked = sorted(name for name in names if unsafe(name))
    if blocked:
        raise SystemExit(f"{artifact.name} contains blocked files: {blocked}")

wheel_contents = wheel_names(wheels[0])
missing = sorted(REQUIRED_WHEEL_FILES - wheel_contents)
unexpected_top_level = sorted(
    name
    for name in wheel_contents
    if not name.startswith("kanopy/") and ".dist-info/" not in name
)
if missing or unexpected_top_level:
    raise SystemExit(
        f"unsafe wheel contents; missing={missing}, "
        f"unexpected_top_level={unexpected_top_level}"
    )

for required in REQUIRED_LICENSE_FILES:
    if not any(
        name.endswith((f".dist-info/{required}", f".dist-info/licenses/{required}"))
        for name in wheel_contents
    ):
        raise SystemExit(f"wheel is missing {required}")

sdist_contents = sdist_names(sdists[0])
for required in REQUIRED_LICENSE_FILES:
    if not any(name.endswith(f"/{required}") for name in sdist_contents):
        raise SystemExit(f"source distribution is missing {required}")
if not any(
    name.endswith("/tests/fixtures/openapi.public.json") for name in sdist_contents
):
    raise SystemExit("source distribution is missing the reviewed OpenAPI fixture")

print(f"Distribution contents approved: {wheels[0].name}, {sdists[0].name}")
