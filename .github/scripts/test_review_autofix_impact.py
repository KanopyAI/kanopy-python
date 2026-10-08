import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_review_autofix import autofix, BACKEND_PROFILE, FakeGitHub, report

impact = autofix.impact
PROFILE = dict(BACKEND_PROFILE, customer_impact={"required": True})
ROOT = Path(__file__).resolve().parents[2]


def entry(slug="author-entry"):
    return {
        "id": slug, "source_digest": "0" * 64, "source_files": [], "type": "fixed",
        "summary": "Keep frame access within the requested job.", "audience": "Cleanup specialists",
        "customer_action": "None.",
        "api": {"impact": "behavior", "assessment": "Frame reads retain job and token permission boundaries.",
                "endpoints": []},
        "availability": "on_deploy", "rollout": "Available after deployment; timing is unverified.",
        "data_effect": "No processing rerun or stored-data migration.", "dependencies": [],
        "notice_url": "", "notice_date": "", "effective_date": "",
    }


class ImpactPolicyTests(unittest.TestCase):
    def test_repository_opt_in_matches_actual_release_gate(self):
        profile = json.loads((ROOT / ".github/review-autofix.json").read_text())
        required = (ROOT / ".github/scripts/release_notes.py").exists()
        self.assertEqual(impact.enabled(profile), required)
        if required:
            self.assertIn("Customer impact recorded", profile["required_completion_checks"])
            workflow = ".github/workflows/" + ("ci.yml" if profile["kind"] == "ios" else "tests.yml")
            self.assertEqual(autofix.ci.target_for(profile, workflow, "Customer impact recorded"), impact.TARGET)
            self.assertIn("fetch-depth: 0 # pinned impact base", (ROOT / ".github/workflows/review-autofix.yml").read_text())

    def test_disabled_repositories_never_load_or_require_release_engine(self):
        with patch.object(impact, "engine", side_effect=AssertionError("must not load")):
            self.assertEqual(impact.allowed_entries(Path("/missing"), {}, BACKEND_PROFILE), [])
            impact.review_and_stamp(Path("/missing"), {}, {}, BACKEND_PROFILE)
            impact.check(Path("/missing"), {}, {}, BACKEND_PROFILE)

    def test_missing_pinned_context_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "trusted customer-impact context"):
            impact.allowed_entries(Path("/missing"), {}, PROFILE)

    def test_feature_base_is_pinned_and_retarget_is_rejected(self):
        gh = Mock()
        gh.get.return_value = {"merge_base_commit": {"sha": "a" * 40}}
        pr = {"number": 325, "head": {"ref": "fix/frames"}, "base": {"ref": "dev", "sha": "b" * 40}}
        state = {"sha": "c" * 40}
        impact.pin(gh, pr, state, PROFILE)
        self.assertEqual(state["impact"]["base"], "a" * 40)
        impact.recheck_base(gh, pr, state, PROFILE)
        pr["base"]["ref"] = "staging"
        with self.assertRaisesRegex(ValueError, "base changed"):
            impact.recheck_base(gh, pr, state, PROFILE)
        pr["base"]["ref"] = "dev"
        gh.get.return_value = {"merge_base_commit": {"sha": "d" * 40}}
        with self.assertRaisesRegex(ValueError, "base changed"):
            impact.recheck_base(gh, pr, state, PROFILE)

    def test_promotion_record_covers_only_separate_fix_pr(self):
        gh = Mock()
        pr = {"number": 325, "head": {"ref": "dev"}, "base": {"ref": "staging"}}
        state = {"sha": "a" * 40, "promotion_base": "staging"}
        impact.pin(gh, pr, state, PROFILE)
        self.assertEqual(state["impact"]["base"], state["sha"])
        self.assertEqual(state["impact"]["base_ref"], "dev")
        gh.get.assert_not_called()
        findings = [{"kind": "ci", "target": impact.TARGET}]
        impact.classify(findings, pr)
        self.assertTrue(findings[0]["manual_only"])

    def test_release_ci_is_collected_without_a_reviewer_finding(self):
        from test_review_autofix_ci import CIGitHub
        gh = CIGitHub()
        gh.job["name"] = "Customer impact recorded"
        profile = dict(PROFILE, ci_workflows={".github/workflows/tests.yml": {
            "Customer impact recorded": impact.TARGET}})
        with patch.object(autofix.ci, "log_excerpt", return_value="customer impact is stale"):
            findings = autofix.ci.collect(gh, gh.pr, gh.checks, profile)
        self.assertEqual([f["target"] for f in findings], [impact.TARGET])
        self.assertEqual(impact.application_findings(findings), [])

    def test_missing_current_head_impact_check_blocks_readiness(self):
        gh = FakeGitHub()
        profile = json.loads((ROOT / ".github/review-autofix.json").read_text())
        if not impact.enabled(profile):
            self.skipTest("Repository has no required impact check")
        _, waiting = autofix.readiness.inspect(gh, gh.pr, {
            "required_completion_checks": ["Customer impact recorded"]}, require_reviews=False)
        self.assertIn("Customer impact recorded", waiting)


@unittest.skipUnless((ROOT / ".github/scripts/release_notes.py").exists(),
                     "Repository explicitly has no release-impact engine or policy")
class ImpactTransactionTests(unittest.TestCase):
    def setUp(self):
        swap = patch.dict(autofix.PROFILE, PROFILE, clear=True)
        swap.start()
        self.addCleanup(swap.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.root = self.temp / "source"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.write(".github/scripts/release_notes.py", "raise AssertionError('Never execute PR release code')\n")
        self.write("app/example.py", "value = 1\n")
        self.write("tests/test_example.py", "def test_example():\n    assert True\n")
        self.write(".release-notes/historical.json", json.dumps(entry("historical")))
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.write("app/example.py", "value = 2\n")
        self.note = ".release-notes/author-entry.json"
        self.write(self.note, json.dumps(entry()))
        self.git("add", ".")
        self.git("commit", "-qm", "author feature with stale note")
        self.head = self.git("rev-parse", "HEAD")
        self.state = {"sha": self.head, "keys": ["key1"], "ci_keys": ["key1"],
                      "branch": "fix/frames", "attempt": 1, "status": "running", "run_id": "run1",
                      "urls": {"key1": "https://github.com/finding"},
                      "impact": {"base": self.base, "base_ref": "dev",
                                 "new_entry": f".release-notes/autofix-pr-268-{self.head[:12]}.json"}}
        self.report = report()
        self.report["tests"] = []
        self.review([self.note])
        self.directory = self.temp / "context"
        self.directory.mkdir()

    def git(self, *args):
        return impact.git(self.root, *args)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def review(self, paths):
        self.report["customer_impact"] = {"head_sha": self.state["sha"], "entries": [
            {"path": p, "assessment": "Traced job-scoped frame reads and token filtering; the entry covers both changes."}
            for p in paths]}

    def package(self, verify=None):
        context = {"state": self.state, "findings": [{"kind": "ci", "target": impact.TARGET}]}
        (self.directory / "context.json").write_text(json.dumps(context))
        (self.directory / "agent-report.json").write_text(json.dumps(self.report))
        with patch.object(autofix.project, "verify", side_effect=verify) as called:
            autofix.package(SimpleNamespace(source=self.root, directory=self.directory))
        return called

    def test_source_fix_refreshes_author_record_and_runs_application_checks(self):
        self.write("app/example.py", "value = 3\n")
        self.report["tests"] = ["tests/test_example.py"]
        called = self.package()
        called.assert_called_once()
        self.assertEqual(called.call_args.kwargs["findings"], [])
        note = json.loads((self.root / self.note).read_text())
        self.assertEqual(note["summary"], entry()["summary"])
        self.assertEqual(note["source_files"], [{"path": "app/example.py", "blob": self.git("rev-parse", ":app/example.py")}])
        candidate = (self.directory / "artifact/change.patch").read_text()
        self.assertIn(self.note, candidate)
        self.assertIn("app/example.py", candidate)
        impact.check(self.root, self.report, self.state, PROFILE)

    def test_stale_record_only_repair_runs_release_check_without_application_tests(self):
        called = self.package()
        called.assert_not_called()
        impact.check(self.root, self.report, self.state, PROFILE)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), self.note)

    def test_missing_or_previous_head_review_never_produces_publishable_artifacts(self):
        for review in (None, {"head_sha": self.base, "entries": []}, {"head_sha": self.head, "entries": []}):
            with self.subTest(review=review):
                self.report["customer_impact"] = review
                with self.assertRaises(ValueError):
                    self.package()
                self.assertFalse((self.directory / "artifact/change.patch").exists())
                self.assertTrue((self.directory / "artifact/packaging-failure.json").exists())

    def test_historical_arbitrary_deleted_and_symlink_entries_are_rejected(self):
        for action in ("historical", "arbitrary", "delete", "symlink"):
            with self.subTest(action=action):
                self.git("reset", "--hard", self.head)
                self.git("clean", "-fd")
                if action == "historical":
                    self.write(".release-notes/historical.json", json.dumps(entry("historical"), indent=2))
                elif action == "arbitrary":
                    self.write(".release-notes/arbitrary.json", json.dumps(entry("arbitrary")))
                else:
                    (self.root / self.note).unlink()
                    if action == "symlink":
                        (self.root / self.note).symlink_to(self.root / ".release-notes/historical.json")
                with self.assertRaises(ValueError):
                    self.package()
                self.assertFalse((self.directory / "artifact/change.patch").exists())

    def test_no_existing_entry_creates_only_deterministic_current_pr_record(self):
        self.git("rm", self.note)
        self.git("commit", "-qm", "remove unreviewed author note")
        self.state["sha"] = self.git("rev-parse", "HEAD")
        paths = impact.allowed_entries(self.root, self.state, PROFILE)
        self.assertEqual(paths, [self.state["impact"]["new_entry"]])
        self.write(paths[0], json.dumps(entry(Path(paths[0]).stem)))
        self.review(paths)
        self.package()
        impact.check(self.root, self.report, self.state, PROFILE)

    def test_all_current_pr_entries_require_review_and_are_preserved(self):
        second = ".release-notes/second-entry.json"
        self.write(second, json.dumps(entry("second-entry")))
        self.git("add", ".")
        self.git("commit", "-qm", "second author record")
        self.state["sha"] = self.git("rev-parse", "HEAD")
        self.review([self.note])
        with self.assertRaisesRegex(ValueError, "every current-PR"):
            self.package()
        self.review([self.note, second])
        self.package()
        impact.check(self.root, self.report, self.state, PROFILE)

    def test_invalid_release_semantics_are_not_hidden_by_stamping(self):
        note = entry()
        note["api"]["impact"] = "breaking"
        self.write(self.note, json.dumps(note))
        with self.assertRaisesRegex(ValueError, "notice URL"):
            self.package()
        self.assertFalse((self.directory / "artifact/change.patch").exists())

    def test_test_mutation_fails_without_restamping_the_changed_source(self):
        self.write("app/example.py", "value = 3\n")
        self.report["tests"] = ["tests/test_example.py"]
        def mutate(*args, **kwargs):
            self.write("app/example.py", "value = 4\n")
        with self.assertRaisesRegex(ValueError, "Tests changed"):
            self.package(mutate)
        self.assertFalse((self.directory / "artifact/change.patch").exists())

    def publish_candidate(self, *, tamper=False):
        self.write("app/example.py", "value = 3\n")
        self.report["tests"] = ["tests/test_example.py"]
        self.package()
        # Simulate a changed artifact after verification, with its old fingerprints.
        artifact = self.directory / "artifact"
        if tamper:
            self.write("app/example.py", "value = 999\n")
            self.git("add", ".")
            (artifact / "change.patch").write_bytes(autofix.git(self.root, "diff", "--cached", "--binary"))
        gh = FakeGitHub()
        gh.pr.update(number=268, base={"ref": "dev", "sha": self.base})
        gh.pr["head"].update(sha=self.head, ref=self.state["branch"])
        gh.comments = [{"id": 3, "user": {"login": "github-actions[bot]"},
                        "body": autofix.claim_body(self.state, "Running")}]
        original_get = gh.get
        gh.get = lambda path: ({"merge_base_commit": {"sha": self.base}} if path.startswith("compare/") else original_get(path))
        original_git = autofix.git
        pushed = []
        def local_git(root, *args, **kwargs):
            if args[:3] == ("remote", "add", "origin"):
                args = (*args[:3], str(self.root))
            if args[0] == "push":
                pushed.append(args)
                return b""
            return original_git(root, *args)  # local fixture, no publisher token
        with patch.object(autofix, "git", side_effect=local_git), patch.dict(os.environ, {
                "GITHUB_RUN_ID": "run1", "REVIEW_FIXER_TOKEN": "unused"}), \
                patch.object(impact, "review_and_stamp", side_effect=AssertionError("publisher must never stamp")), \
                patch.object(autofix.GitHub, "comment"):
            if tamper:
                with self.assertRaisesRegex(ValueError, "customer impact is stale"):
                    autofix.publish(gh, SimpleNamespace(number=268, directory=artifact, source=self.temp / "publish"))
            else:
                autofix.publish(gh, SimpleNamespace(number=268, directory=artifact, source=self.temp / "publish"))
        return pushed

    def test_publisher_accepts_source_and_reviewed_record_in_one_commit(self):
        self.assertEqual(len(self.publish_candidate()), 1)
        files = impact.git(self.temp / "publish", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        self.assertEqual(set(files.splitlines()), {"app/example.py", self.note})

    def test_publisher_rejects_tampered_candidate_without_running_pr_release_code(self):
        self.assertEqual(self.publish_candidate(tamper=True), [])


if __name__ == "__main__":
    unittest.main()
