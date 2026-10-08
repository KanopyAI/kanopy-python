#!/usr/bin/env python3
"""Check the vendored report core without importing repository or PR code."""
import argparse
import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ".github/review-autofix-core.json"
CONTROLLER = ".github/scripts/review_autofix.py"
SYMBOLS = (
    "MAX_ATTEMPTS", "TEST_SELECTOR_PATTERN", "InvalidTestSelector",
    "test_arguments", "package", "package_verified", "check_packaging_failure", "validate_patch",
)
FILES = (
    ".github/prompts/review-autofix.md",
    ".github/prompts/review-autofix.schema.json",
    ".github/scripts/review_autofix_impact.py",
    ".github/scripts/test_review_autofix_impact.py",
    ".github/scripts/review_autofix_parity.py",
    ".github/scripts/test_review_autofix_parity.py",
)


def fingerprints(root):
    source = (root / CONTROLLER).read_text()
    selected = {}
    for node in ast.parse(source).body:
        names = ([node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                 else [target.id for target in node.targets if isinstance(target, ast.Name)]
                 if isinstance(node, ast.Assign) else [])
        for name in names:
            if name in SYMBOLS:
                if name in selected:
                    raise ValueError(f"Duplicate shared symbol: {name}")
                selected[name] = ast.get_source_segment(source, node).encode()
    missing = set(SYMBOLS) - selected.keys()
    if missing:
        raise ValueError("Missing shared symbols: " + ", ".join(sorted(missing)))
    # Hash exact source segments, not AST dumps whose fields vary by Python version.
    content = {f"{CONTROLLER}:{name}": value for name, value in selected.items()}
    content.update({path: (root / path).read_bytes() for path in FILES})
    return {key: hashlib.sha256(value).hexdigest() for key, value in sorted(content.items())}


def manifest(root):
    return {"version": 1, "source": "KanopyAI/kanopy-backend", "fingerprints": fingerprints(root)}


def check(root, reference):
    try:
        recorded = json.loads((root / MANIFEST).read_text())
        actual = fingerprints(root)
    except (OSError, ValueError, SyntaxError) as exc:
        return [str(exc)]
    errors = []
    if recorded != reference:
        errors.append(f"{MANIFEST} differs from the canonical baseline")
    expected = reference["fingerprints"]
    errors.extend(f"Shared core differs: {key}" for key in sorted(actual.keys() | expected.keys())
                  if actual.get(key) != expected.get(key))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="*", type=Path, help="Compare checkouts with this checkout's baseline")
    parser.add_argument("--write-manifest", action="store_true", help="Record an intentionally updated canonical core")
    args = parser.parse_args()
    if args.write_manifest:
        if args.repos:
            parser.error("--write-manifest only updates this checkout")
        (ROOT / MANIFEST).write_text(json.dumps(manifest(ROOT), indent=2) + "\n")
        return
    reference = json.loads((ROOT / MANIFEST).read_text())
    failed = False
    for root in args.repos or [ROOT]:
        errors = check(root, reference)
        failed |= bool(errors)
        print(f"{root}: " + ("\n  ".join(errors) if errors else "shared report core matches"))
    raise SystemExit(int(failed))


if __name__ == "__main__":
    main()
