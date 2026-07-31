"""Verify that package versions agree, optionally including a release tag."""

from __future__ import annotations

import ast
import configparser
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
config = configparser.ConfigParser()
config.read(root / "setup.cfg")
distribution_version = config["metadata"]["version"]

module = ast.parse((root / "src/kanopy/__init__.py").read_text(encoding="utf-8"))
module_version: str | None = None
for statement in module.body:
    if not isinstance(statement, ast.Assign):
        continue
    if any(
        isinstance(target, ast.Name) and target.id == "__version__"
        for target in statement.targets
    ):
        module_version = ast.literal_eval(statement.value)
        break

if module_version != distribution_version:
    raise SystemExit(
        f"version mismatch: setup.cfg={distribution_version!r}, "
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
