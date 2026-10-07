import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("parity", Path(__file__).with_name("review_autofix_parity.py"))
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)


class ReportCoreParityTests(unittest.TestCase):
    def setUp(self):
        self.reference = json.loads((parity.ROOT / parity.MANIFEST).read_text())

    def checkout(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in (*parity.FILES, parity.CONTROLLER, parity.MANIFEST):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((parity.ROOT / name).read_bytes())
        return root

    def test_checked_in_core_matches_baseline(self):
        self.assertEqual(parity.check(parity.ROOT, self.reference), [])

    def test_shared_logic_and_schema_drift_fail(self):
        for name, old, new in [
            (parity.CONTROLLER, "MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 30"),
            (parity.CONTROLLER, "def package(args):", "def removed_package(args):"),
            (".github/prompts/review-autofix.schema.json", '"type": "array"', '"type": "string"'),
        ]:
            with self.subTest(name=name, old=old):
                root = self.checkout()
                path = root / name
                self.assertIn(old, path.read_text())
                path.write_text(path.read_text().replace(old, new, 1))
                self.assertTrue(parity.check(root, self.reference))

    def test_repository_specific_code_can_differ(self):
        root = self.checkout()
        path = root / parity.CONTROLLER
        path.write_text(path.read_text() + '\n\ndef repository_setup_diagnostic():\n    return "preserved"\n')
        (root / ".github/review-autofix.json").write_text('{"kind": "custom"}\n')
        self.assertEqual(parity.check(root, self.reference), [])

    def test_locally_refreshed_manifest_still_fails_cross_repo_check(self):
        root = self.checkout()
        path = root / parity.CONTROLLER
        path.write_text(path.read_text().replace("MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 30", 1))
        refreshed = parity.manifest(root)
        (root / parity.MANIFEST).write_text(json.dumps(refreshed))
        self.assertEqual(parity.check(root, refreshed), [])
        errors = parity.check(root, self.reference)
        self.assertTrue(any("canonical baseline" in error for error in errors))
        self.assertTrue(any("MAX_ATTEMPTS" in error for error in errors))

    def test_missing_file_reports_failure(self):
        root = self.checkout()
        (root / parity.FILES[0]).unlink()
        self.assertTrue(parity.check(root, self.reference))


if __name__ == "__main__":
    unittest.main()
