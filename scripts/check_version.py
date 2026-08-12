"""Verify that package versions agree, optionally including a release tag."""

from __future__ import annotations

import ast
import configparser
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
config = configparser.ConfigParser()
config.read(root / "setup.cfg")
distribution_spec = config["metadata"]["version"]


def literal_version(path: Path) -> str | None:
    """Read a literal ``__version__`` without importing package dependencies."""
    module = ast.parse(path.read_text(encoding="utf-8"))
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            return value if isinstance(value, str) else None
    return None


if distribution_spec.startswith("attr:"):
    attribute_ref = distribution_spec.removeprefix("attr:").strip()
    *module_parts, attribute_name = attribute_ref.split(".")
    if attribute_name != "__version__" or not module_parts:
        raise SystemExit(f"unsupported setup.cfg version attribute: {attribute_ref!r}")
    distribution_version = literal_version(
        root / "src" / Path(*module_parts).with_suffix(".py")
    )
else:
    distribution_version = distribution_spec

init_path = root / "src/kanopy/__init__.py"
module_version = literal_version(init_path)
if module_version is None:
    init_module = ast.parse(init_path.read_text(encoding="utf-8"))
    for statement in init_module.body:
        if not isinstance(statement, ast.ImportFrom) or statement.level != 1:
            continue
        if not any(alias.name == "__version__" for alias in statement.names):
            continue
        if statement.module:
            module_version = literal_version(
                init_path.parent / Path(*statement.module.split(".")).with_suffix(".py")
            )
        break

if module_version != distribution_version:
    raise SystemExit(
        f"version mismatch: setup.cfg={distribution_spec!r} "
        f"(resolved={distribution_version!r}), "
        f"kanopy.__version__={module_version!r}"
    )

if len(sys.argv) > 1:
    release_tag = sys.argv[1]
    expected_tag = f"v{distribution_version}"
    if release_tag != expected_tag:
        raise SystemExit(
            f"release tag {release_tag!r} does not match package version "
            f"({expected_tag!r} required)"
        )

print(f"Version {distribution_version} is consistent")
