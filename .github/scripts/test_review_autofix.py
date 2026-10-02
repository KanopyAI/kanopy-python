import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("review_autofix", Path(__file__).with_name("review_autofix.py"))
autofix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autofix)

BACKEND_PROFILE = {
    "kind": "backend", "allowed_paths": ["app/*", "worker/*", "tests/*", "docs/*"],
    "test_paths": ["tests/test*.py", "tests/*/test*.py"],
}


class BackendPolicyFixture:
    def setUp(self):
        self.policy = patch.dict(autofix.PROFILE, BACKEND_PROFILE, clear=True)
        self.policy.start()
        self.addCleanup(self.policy.stop)


def pr():
    return {"number": 268, "title": "Pole matching", "state": "open", "draft": False,
            "head": {"repo": {"full_name": "KanopyAI/kanopy-backend"},
                     "ref": "hotfix/pole-neighborhood-matching", "sha": "a" * 40},
            "labels": []}


def thread():
    return {"id": "thread1", "isResolved": False, "isOutdated": False,
            "path": "app/example.py", "line": 5,
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{
                "id": "comment1", "body": "A genuine finding", "url": "https://github.com/finding",
                "author": {"login": "greptile-apps"}, "originalCommit": {"oid": "a" * 40}}]}}


def report(status="fixed"):
    return {"summary": "Investigated", "findings": [
        {"key": "key1", "status": status, "explanation": "Evidence"}],
        "tests": ["tests/test_example.py"]}


class FakeGitHub:
    repo = "KanopyAI/kanopy-backend"

    def __init__(self):
        self.pr = pr()
        self.comments = []
        self.checks = []
        self.found = [autofix.select_finding(thread())]
        self.writes = []

    def get(self, path):
        if not path:
            return {"default_branch": "dev"}
        if path.startswith("pulls/"):
            return self.pr
        if "check-runs" in path:
            return {"total_count": len(self.checks), "check_runs": self.checks}
        if path.endswith("/status"):
            return {"total_count": 0, "state": "pending"}
        raise AssertionError(path)

    def pages(self, path):
        if path == "pulls?state=open":
            return [self.pr]
        return self.comments

    def findings(self, number):
        return self.found

    def comment(self, number, body, comment_id=None):
        self.writes.append(body)
        return {"id": 123}


class ControllerTests(BackendPolicyFixture, unittest.TestCase):
    def test_repository_url_has_no_trailing_slash(self):
        gh = autofix.GitHub(FakeGitHub.repo, "unused")
        with patch.object(gh, "api", return_value={}) as api:
            gh.get("")
            api.assert_called_once_with("repos/KanopyAI/kanopy-backend")

    def test_same_repo_feature_prs_are_included_without_a_label(self):
        source = pr()
        self.assertTrue(autofix.eligible(source, FakeGitHub.repo, "dev"))
        for field, value in [("state", "closed"), ("draft", True),
                             ("labels", [{"name": autofix.SKIP_LABEL}])]:
            changed = dict(source, **{field: value})
            self.assertFalse(autofix.eligible(changed, FakeGitHub.repo, "dev"))
        for branch in ["main", "staging", "dev", "master"]:
            changed = copy.deepcopy(source)
            changed["head"]["ref"] = branch
            self.assertFalse(autofix.eligible(changed, FakeGitHub.repo, "dev"))
        source["head"]["repo"]["full_name"] = "attacker/fork"
        self.assertFalse(autofix.eligible(source, FakeGitHub.repo, "dev"))
        source["head"]["repo"] = None
        self.assertFalse(autofix.eligible(source, FakeGitHub.repo, "dev"))

    def test_skip_label_overrides_other_labels(self):
        source = pr()
        source["labels"] = [{"name": "auto-fix-review"}, {"name": "bug"}]
        self.assertTrue(autofix.eligible(source, FakeGitHub.repo, "dev"))
        source["labels"].append({"name": autofix.SKIP_LABEL})
        self.assertFalse(autofix.eligible(source, FakeGitHub.repo, "dev"))

    def test_discovery_includes_unlabelled_prs_and_filters_exceptions(self):
        gh = FakeGitHub()
        with patch.object(autofix, "output") as output:
            autofix.scan(gh, SimpleNamespace(number=None, dry_run=False))
            output.assert_called_once_with("matrix", "[268]")
        gh.pr["labels"] = [{"name": autofix.SKIP_LABEL}]
        with patch.object(autofix, "output") as output:
            autofix.scan(gh, SimpleNamespace(number=None, dry_run=False))
            output.assert_called_once_with("matrix", "[]")
    def test_findings_are_trusted_unresolved_and_include_old_commits(self):
        source = thread()
        before = autofix.select_finding(source)
        source["isOutdated"] = True
        self.assertEqual(autofix.select_finding(source)["key"], before["key"])
        source["comments"]["nodes"][0]["body"] += " revised"
        self.assertNotEqual(autofix.select_finding(source)["key"], before["key"])
        source["comments"]["nodes"][0]["author"]["login"] = "untrusted-reviewer"
        self.assertIsNone(autofix.select_finding(source))
        source = thread()
        source["isResolved"] = True
        self.assertIsNone(autofix.select_finding(source))

    def test_new_thread_reply_reopens_a_processed_finding(self):
        source = thread()
        original = autofix.select_finding(source)
        source["comments"]["nodes"].append({"id": "reply1", "body": "This still fails", "author": {"login": "greptile-apps"}})
        revised = autofix.select_finding(source)
        self.assertNotEqual(original["key"], revised["key"])
        self.assertEqual(autofix.pending_findings([revised], [{"processed": [original["key"]]}]), [revised])

    def test_cancelled_run_claim_is_reconciled_without_retry(self):
        gh = FakeGitHub()
        old = {"status": "running", "run_id": "old-run", "fingerprint": autofix.fingerprint(gh.pr["head"]["sha"], gh.found)}
        gh.comments = [{"id": 123, "user": {"login": "github-actions[bot]"}, "body": autofix.claim_body(old, "Running")}]
        original_get = gh.get
        def get(path):
            return {"status": "completed", "conclusion": "cancelled"} if path == "actions/runs/old-run" else original_get(path)
        with tempfile.TemporaryDirectory() as directory, patch.object(gh, "get", side_effect=get):
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
        self.assertEqual(len(gh.writes), 1)
        self.assertIn('"status":"failed"', gh.writes[0])
        self.assertIn("cancelled", gh.writes[0])

    def test_bot_suffix_and_truncated_discussion(self):
        source = thread()
        source["comments"]["nodes"][0]["author"]["login"] = "sentry[bot]"
        self.assertIsNotNone(autofix.select_finding(source))
        source["comments"]["pageInfo"]["hasNextPage"] = True
        with self.assertRaises(RuntimeError):
            autofix.select_finding(source)

    def test_human_cannot_spoof_attempt_state(self):
        state = {"fingerprint": "x", "status": "completed", "processed": ["key"]}
        comment = {"id": 5, "user": {"login": "human"},
                   "body": autofix.claim_body(state, "Done")}
        self.assertEqual(autofix.claims([comment]), [])
        comment["user"]["login"] = "github-actions[bot]"
        self.assertEqual(autofix.claims([comment])[0]["processed"], ["key"])

    def test_processed_findings_and_snapshot_dedup(self):
        self.assertEqual(autofix.pending_findings([{"key": "a"}, {"key": "b"}],
                         [{"processed": ["a"]}]), [{"key": "b"}])
        self.assertEqual(autofix.fingerprint("sha", [{"key": "a"}, {"key": "b"}]),
                         autofix.fingerprint("sha", [{"key": "b"}, {"key": "a"}]))
        self.assertNotEqual(autofix.fingerprint("old", [{"key": "a"}]),
                            autofix.fingerprint("new", [{"key": "a"}]))

    def test_prepare_preview_does_not_reserve_or_require_keys(self):
        gh = FakeGitHub()
        gh.pr["labels"] = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=True, directory=directory))
            data = json.loads((Path(directory) / "context.json").read_text())
            self.assertEqual(len(data["findings"]), 1)
            self.assertEqual(gh.writes, [])
            self.assertFalse((Path(directory) / "prompt.md").exists())

    def test_oversized_context_pauses_once_with_a_pr_comment(self):
        gh = FakeGitHub()
        gh.found[0]["discussion"][0]["body"] = "x" * 200_001
        with tempfile.TemporaryDirectory() as directory, patch.object(autofix, "output") as output:
            args = SimpleNamespace(number=268, dry_run=False, directory=directory)
            autofix.prepare(gh, args)
            self.assertEqual(len(gh.writes), 1)
            self.assertIn("manually triage", gh.writes[0])
            self.assertEqual(output.call_args.args, ("ready", "false"))
            gh.comments = [{"id": 123, "user": {"login": "github-actions[bot]"}, "body": gh.writes[0]}]
            self.assertEqual(autofix.claims(gh.comments)[0]["status"], "needs_human")
            autofix.prepare(gh, args)
            self.assertEqual(len(gh.writes), 1)

    def test_oversized_context_preview_does_not_write(self):
        gh = FakeGitHub()
        gh.found[0]["discussion"][0]["body"] = "x" * 200_001
        with tempfile.TemporaryDirectory() as directory:
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=True, directory=directory))
        self.assertEqual(gh.writes, [])

    def test_skip_label_prevents_live_and_preview_attempts(self):
        gh = FakeGitHub()
        gh.pr["labels"] = [{"name": autofix.SKIP_LABEL}]
        for dry_run in [True, False]:
            with tempfile.TemporaryDirectory() as directory, patch.object(gh, "findings") as findings:
                autofix.prepare(gh, SimpleNamespace(number=268, dry_run=dry_run, directory=directory))
                findings.assert_not_called()
                self.assertFalse((Path(directory) / "context.json").exists())
        self.assertEqual(gh.writes, [])

    def test_prepare_waits_for_reviewers(self):
        gh = FakeGitHub()
        gh.checks = [{"status": "in_progress"}]
        with tempfile.TemporaryDirectory() as directory:
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
            self.assertFalse((Path(directory) / "context.json").exists())
        self.assertEqual(gh.writes, [])

    def test_attempt_is_reserved_before_model_is_enabled(self):
        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "HAS_OPENAI_KEY": "true", "HAS_PUSH_TOKEN": "true", "GITHUB_RUN_ID": "run1",
        }), patch.object(autofix, "output") as output:
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
            context = json.loads((Path(directory) / "context.json").read_text())
            self.assertEqual(context["state"]["comment_id"], 123)
            self.assertEqual(len(gh.writes), 1)
            self.assertEqual(output.call_args.args, ("ready", "true"))
            gh.comments = [{"id": 123, "user": {"login": "github-actions[bot]"}, "body": gh.writes[0]}]
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
            self.assertEqual(len(gh.writes), 1)
            self.assertEqual(output.call_args.args, ("ready", "false"))

    def test_missing_credentials_do_not_consume_an_attempt(self):
        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Configure OPENAI_API_KEY"):
                autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
        self.assertEqual(gh.writes, [])

    def test_no_fourth_attempt_or_retry_after_human_decision(self):
        for statuses in [["failed"] * 3, ["needs_human"]]:
            gh = FakeGitHub()
            gh.comments = [{"id": i, "user": {"login": "github-actions[bot]"}, "body":
                autofix.claim_body({"status": status, "fingerprint": str(i)}, "Stopped")}
                for i, status in enumerate(statuses)]
            with tempfile.TemporaryDirectory() as directory:
                autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
                self.assertFalse((Path(directory) / "context.json").exists())
            self.assertEqual(gh.writes, [])

    def test_report_cannot_omit_duplicate_or_invent_findings(self):
        self.assertTrue(autofix.validate_report(report(), ["key1"]))
        for keys in [["key1", "key2"], ["other"]]:
            with self.assertRaises(ValueError):
                autofix.validate_report(report(), keys)
        duplicated = report()
        duplicated["findings"] *= 2
        with self.assertRaises(ValueError):
            autofix.validate_report(duplicated, ["key1", "key2"])

    def test_protected_paths(self):
        for path in [".github/workflows/deploy.yml", "app/../../.env", "/app/example.py",
                     "app/AGENTS.md", "tests/quarantine.txt", "tests/conftest.py",
                     "app/.codex/config.toml", "requirements.txt", "app/x\nfile.py"]:
            self.assertFalse(autofix.allowed_path(path), path)
        for path in ["app/main.py", "worker/task.py", "tests/test_fix.py", "docs/feature.md"]:
            self.assertTrue(autofix.allowed_path(path), path)

    def test_test_selectors_reject_flags_escape_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_example.py").touch()
            (root / "outside.py").touch()
            (root / "tests/test_link.py").symlink_to(root / "outside.py")
            for tests in [[], ["--override-ini=x"], ["tests/../outside.py"],
                          ["tests/test_link.py"], ["tests/missing.py"]]:
                with self.assertRaises(ValueError):
                    autofix.test_arguments(root, tests)
            self.assertEqual(autofix.test_arguments(root, ["tests/test_example.py::test_x"]),
                             ["tests/test_example.py::test_x"])

    def test_publisher_rechecks_head_and_skip_label_before_git(self):
        for change in ["head", "skip_label"]:
            gh = FakeGitHub()
            state = {"run_id": "run1", "status": "running", "keys": ["key1"],
                     "sha": "a" * 40, "branch": gh.pr["head"]["ref"], "attempt": 1}
            gh.comments = [{"id": 3, "user": {"login": "github-actions[bot]"},
                            "body": autofix.claim_body(state, "Running")}]
            if change == "head":
                gh.pr["head"]["sha"] = "b" * 40
            else:
                gh.pr["labels"] = [{"name": autofix.SKIP_LABEL}]
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_RUN_ID": "run1"}):
                root = Path(directory)
                (root / "report.json").write_text(json.dumps(report()))
                (root / "change.patch").write_text("patch")
                with patch.object(autofix, "git") as git:
                    with self.assertRaisesRegex(ValueError, "PR changed"):
                        autofix.publish(gh, SimpleNamespace(number=268, directory=directory, source=directory))
                    git.assert_not_called()
            self.assertIn('"status":"failed"', gh.writes[-1])


    def exercise_publication(self, *, opt_out=False, review_error=False, reporting_error=False):
        gh = FakeGitHub()
        state = {"run_id": "run1", "status": "running", "keys": ["key1"],
                 "urls": {"key1": "https://github.com/finding"}, "sha": "a" * 40,
                 "branch": gh.pr["head"]["ref"], "attempt": 1}
        gh.comments = [{"id": 3, "user": {"login": "github-actions[bot]"},
                        "body": autofix.claim_body(state, "Running")}]
        pushed = []
        def git(root, *args, **kwargs):
            if args[0] == "fetch" and opt_out:
                gh.pr["labels"] = [{"name": autofix.SKIP_LABEL}]
            if args[0] == "push":
                pushed.append(args)
            return b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n" if args[0] == "rev-parse" else b""
        original_comment = gh.comment
        comment_calls = []
        def comment(*args, **kwargs):
            comment_calls.append(args)
            if reporting_error and len(comment_calls) == 1:
                raise RuntimeError("temporary comment failure")
            return original_comment(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "GITHUB_RUN_ID": "run1", "REVIEW_FIXER_TOKEN": "fake-token"}):
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests/test_example.py").touch()
            (root / "report.json").write_text(json.dumps(report()))
            (root / "change.patch").write_text("patch")
            with patch.object(autofix, "git", side_effect=git), \
                    patch.object(autofix, "validate_patch", return_value=["tests/test_example.py"]), \
                    patch.object(autofix.GitHub, "comment", side_effect=RuntimeError("review unavailable") if review_error else None) as review, \
                    patch.object(gh, "comment", side_effect=comment):
                args = SimpleNamespace(number=268, directory=directory, source=directory)
                if opt_out:
                    with self.assertRaisesRegex(ValueError, "opted out before push"):
                        autofix.publish(gh, args)
                    review.assert_not_called()
                else:
                    autofix.publish(gh, args)
                    review.assert_called_once()
        return gh.writes, pushed

    def test_late_opt_out_stops_push(self):
        writes, pushed = self.exercise_publication(opt_out=True)
        self.assertEqual(pushed, [])
        self.assertIn('"status":"failed"', writes[-1])

    def test_review_request_failure_preserves_successful_publication(self):
        writes, pushed = self.exercise_publication(review_error=True)
        self.assertEqual(len(pushed), 1)
        self.assertIn('"status":"completed"', writes[-1])
        self.assertIn("Request review manually", writes[-1])

    def test_reporting_failure_records_successful_push_for_followup(self):
        writes, pushed = self.exercise_publication(reporting_error=True)
        self.assertEqual(len(pushed), 1)
        self.assertIn('"status":"needs_human"', writes[-1])
        self.assertIn("Fix was pushed", writes[-1])



class PatchTests(BackendPolicyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        autofix.git(self.root, "init", "-q")
        autofix.git(self.root, "config", "user.email", "test@example.com")
        autofix.git(self.root, "config", "user.name", "Test")
        (self.root / "app").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "app/example.py").write_text("value = 1\n")
        (self.root / "tests/test_example.py").write_text("def test_example():\n    assert True\n")
        autofix.git(self.root, "add", ".")
        autofix.git(self.root, "commit", "-qm", "initial")

    def test_symlink_patch_is_rejected(self):
        (self.root / "app/link.py").symlink_to("/etc/passwd")
        autofix.git(self.root, "add", ".")
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            autofix.validate_patch(self.root)

    def test_rename_outside_allowed_paths_is_rejected(self):
        (self.root / "app/example.py").rename(self.root / "danger.py")
        autofix.git(self.root, "add", "--all")
        with self.assertRaisesRegex(ValueError, "protected"):
            autofix.validate_patch(self.root)

    def test_rename_from_protected_path_is_rejected(self):
        protected = self.root / ".github/workflows/deploy.yml"
        protected.parent.mkdir(parents=True)
        protected.write_text("name: deployment\n")
        autofix.git(self.root, "add", ".")
        autofix.git(self.root, "commit", "-qm", "protected workflow")
        protected.rename(self.root / "app/deploy.yml")
        autofix.git(self.root, "add", "--all")
        with self.assertRaisesRegex(ValueError, "protected"):
            autofix.validate_patch(self.root)

    def setup_package(self):
        directory = Path(self.temp.name) / "result"
        directory.mkdir()
        sha = autofix.git(self.root, "rev-parse", "HEAD").decode().strip()
        (directory / "context.json").write_text(json.dumps({"state": {"sha": sha, "keys": ["key1"]}}))
        (directory / "agent-report.json").write_text(json.dumps(report()))
        (self.root / "app/example.py").write_text("value = 2\n")
        (self.root / "tests/test_example.py").write_text("def test_example():\n    assert 2 == 2\n")
        return SimpleNamespace(directory=str(directory), source=str(self.root)), directory

    def test_failed_tests_never_produce_a_publishable_artifact(self):
        args, directory = self.setup_package()
        real_run = subprocess.run

        def run(command, **kwargs):
            if "pytest" in command:
                raise subprocess.CalledProcessError(1, command)
            return real_run(command, **kwargs)

        with patch.object(autofix.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                autofix.package(args)
        self.assertFalse((directory / "artifact").exists())

    def test_success_packages_verified_diff_and_includes_changed_test_modules(self):
        args, directory = self.setup_package()
        selected = report()
        selected["tests"] = ["tests/test_example.py::test_example"]
        (directory / "agent-report.json").write_text(json.dumps(selected))
        real_run = subprocess.run
        commands = []

        def run(command, **kwargs):
            if "pytest" in command:
                commands.append(command)
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, **kwargs)

        with patch.object(autofix.subprocess, "run", side_effect=run):
            autofix.package(args)
        self.assertIn("tests/test_example.py", commands[0])
        self.assertIn(b"+value = 2", (directory / "artifact/change.patch").read_bytes())
        published = json.loads((directory / "artifact/report.json").read_text())
        self.assertIn("tests/test_example.py", published["tests"])

    def test_test_mutations_cannot_be_published_as_tested(self):
        args, directory = self.setup_package()
        real_run = subprocess.run

        def run(command, **kwargs):
            if "pytest" in command:
                (self.root / "app/example.py").write_text("untested = True\n")
                return subprocess.CompletedProcess(command, 0)
            return real_run(command, **kwargs)

        with patch.object(autofix.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "Tests changed"):
                autofix.package(args)
        self.assertFalse((directory / "artifact").exists())


class ProjectPolicyTests(unittest.TestCase):
    def test_repository_profile_is_configured(self):
        self.assertIn(autofix.PROFILE["kind"], {"backend", "frontend", "powerline", "infra", "ios", "python"})
        self.assertTrue(autofix.PROFILE["allowed_paths"])
        self.assertTrue(autofix.PROFILE["test_paths"])
        self.assertTrue(autofix.PROFILE["validation_description"])

    def test_project_paths_and_regression_patterns(self):
        cases = [
            ("frontend", ["src/*"], ["src/*.test.tsx"], "src/components/Button.test.tsx", "src/components/Button.tsx"),
            ("powerline", ["containers/*"], ["containers/*/test_*.py"], "containers/powerline_analysis/tests/test_pole.py", "containers/powerline_analysis/service.py"),
            ("infra", ["*.tf", "tests/*"], ["tests/test*.py"], "tests/test_policy.py", "main.tf"),
            ("ios", ["kanopy-ios-app/*", "KanopyAITests/*"], ["KanopyAITests/*Tests.swift"], "KanopyAITests/LoginTests.swift", "kanopy-ios-app/Views/Login.swift"),
            ("python", ["src/*", "tests/*"], ["tests/test*.py"], "tests/test_client.py", "src/kanopy/client.py"),
        ]
        for kind, allowed, tests, regression, source in cases:
            with self.subTest(kind=kind), patch.dict(autofix.PROFILE, {
                    "kind": kind, "allowed_paths": allowed, "test_paths": tests}, clear=True):
                self.assertTrue(autofix.allowed_path(source))
                self.assertTrue(autofix.allowed_path(regression))
                self.assertTrue(autofix.is_test_file(regression))
                self.assertFalse(autofix.is_test_file(source))
                self.assertFalse(autofix.allowed_path(".github/workflows/review-autofix.yml"))

    def test_frontend_verifier_runs_unit_tests_and_typecheck(self):
        with patch.object(autofix.project, "run") as run:
            autofix.project.verify(Path("/repo"), ["src/a.test.tsx"], ["src/a.tsx"], {"kind": "frontend"})
        self.assertEqual([call.args[0] for call in run.call_args_list], [["npm", "test"], ["npm", "run", "typecheck"]])

    def test_infra_validation_never_plans_or_applies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").touch()
            with patch.object(autofix.project, "run") as run:
                autofix.project.verify(root, ["tests/nested/test_policy.py"], ["main.tf"], {"kind": "infra"})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["terraform", "init", "-backend=false", "-input=false", "-lockfile=readonly"], commands)
        self.assertIn(["terraform", "validate", "-no-color"], commands)
        self.assertIn([autofix.sys.executable, "-m", "pytest", "tests/nested/test_policy.py", "-q"], commands)
        self.assertFalse(any("plan" in command or "apply" in command for command in commands))

    def test_powerline_nonservice_findings_do_not_block_setup(self):
        context = {"findings": [{"path": "docs/usage.md"}, {"path": "containers/powerline_analysis/main.py"}]}
        with patch.object(autofix.project, "setup_service") as setup_service:
            autofix.project.setup(Path("/repo"), context, {"kind": "powerline"})
        setup_service.assert_called_once_with(Path("/repo"), "powerline_analysis")

    def test_powerline_service_mapping_and_isolation(self):
        project = autofix.project
        self.assertEqual(project.service_for("containers/powerline_analysis/wire_fitting/tests/test_wire.py"), "powerline_analysis")
        self.assertEqual(project.service_for("containers/tests/test_boundary.py"), "powerline_data_prep")
        with self.assertRaises(ValueError):
            project.service_for("unknown/test_example.py")
        with patch.dict(os.environ, {"RUNNER_TEMP": "/tmp/runner"}):
            self.assertNotEqual(project.service_python("powerline_analysis"), project.service_python("powerline_segmentation"))

    def test_ios_uses_simulator_without_signing(self):
        devices = {"devices": {"iOS": [{"isAvailable": True, "name": "iPhone", "udid": "device-id"}]}}
        with patch.object(autofix.project.subprocess, "check_output", return_value=json.dumps(devices).encode()), \
                patch.object(autofix.project, "run") as run, patch.dict(os.environ, {"RUNNER_TEMP": "/tmp/runner"}):
            autofix.project.verify(Path("/repo"), ["KanopyAITests/ExampleTests.swift"], [], {"kind": "ios"})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual([command[1] for command in commands], ["test", "analyze"])
        for command in commands:
            self.assertIn("CODE_SIGNING_ALLOWED=NO", command)
            self.assertIn("platform=iOS Simulator,id=device-id", command)


if __name__ == "__main__":
    unittest.main()
