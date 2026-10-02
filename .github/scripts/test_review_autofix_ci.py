import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from test_review_autofix import autofix, FakeGitHub, BACKEND_PROFILE, report

ci = autofix.ci


class CIGitHub(FakeGitHub):
    token = "unused"

    def __init__(self):
        super().__init__()
        self.found = []
        self.checks = [{"id": 123, "head_sha": self.pr["head"]["sha"], "status": "completed",
                        "conclusion": "failure", "app": {"slug": "github-actions"},
                        "output": {"summary": "One test failed"}}]
        self.run = {"id": 99, "path": ".github/workflows/tests.yml", "head_sha": self.pr["head"]["sha"],
                    "head_branch": self.pr["head"]["ref"], "status": "completed", "event": "pull_request", "run_attempt": 1}
        self.job = {"id": 456, "head_sha": self.pr["head"]["sha"], "status": "completed", "conclusion": "failure",
                    "check_run_url": f"https://api.github.com/repos/{self.repo}/check-runs/123",
                    "html_url": "https://github.com/job", "name": "pytest (core-auth)",
                    "steps": [{"name": "Run suite", "conclusion": "failure"}]}

    def get(self, path):
        if path.startswith("actions/runs?head_sha="):
            return {"total_count": 1, "workflow_runs": [self.run]}
        if path.startswith("actions/runs/99/jobs?"):
            return {"total_count": 1, "jobs": [self.job]}
        if path.startswith("check-runs/123/annotations"):
            return [{"path": "tests/test_example.py", "start_line": 5, "title": "Assertion failed", "message": "Expected 2, got 1"}]
        return super().get(path)


class CIFixture:
    def setUp(self):
        self.policy = dict(BACKEND_PROFILE, ci_workflows={
            ".github/workflows/tests.yml": {"pytest (core-auth)": "backend:core-auth"}})
        swap = patch.dict(autofix.PROFILE, self.policy, clear=True)
        swap.start()
        self.addCleanup(swap.stop)
        logs = patch.object(ci, "log_excerpt", return_value="FAILED tests/test_example.py: expected 2, got 1")
        self.logs = logs.start()
        self.addCleanup(logs.stop)


class CollectionTests(CIFixture, unittest.TestCase):
    def test_failed_check_triggers_without_a_reviewer_comment(self):
        gh = CIGitHub()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HAS_OPENAI_KEY": "true", "HAS_PUSH_TOKEN": "true", "GITHUB_RUN_ID": "run1"}):
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
            context = json.loads((Path(directory) / "context.json").read_text())
        self.assertEqual(len(context["findings"]), 1)
        finding = context["findings"][0]
        self.assertEqual(finding["kind"], "ci")
        self.assertEqual(finding["target"], "backend:core-auth")
        self.assertIn("Expected 2", finding["annotations"][0]["message"])
        self.assertEqual(context["state"]["ci_keys"], [finding["key"]])
        self.assertEqual(len(gh.writes), 1)

    def test_old_heads_cancelled_jobs_untrusted_providers_and_unlisted_workflows_are_excluded(self):
        cases = [
            ("check", "head_sha", "old"), ("job", "head_sha", "old"), ("run", "head_sha", "old"),
            ("run", "head_branch", "another-branch"), ("run", "status", "in_progress"),
            ("check", "app", {"slug": "other-app"}), ("check", "conclusion", "cancelled"),
            ("job", "conclusion", "cancelled"), ("job", "check_run_url", "https://attacker/check-runs/123"),
            ("run", "path", ".github/workflows/deploy.yml"),
            ("run", "path", ".github/workflows/review-autofix.yml"),
            ("job", "name", "release"), ("run", "event", "schedule"),
        ]
        for kind, key, value in cases:
            with self.subTest(kind=kind, key=key):
                gh = CIGitHub()
                target = gh.checks[0] if kind == "check" else getattr(gh, kind)
                target[key] = value
                self.assertEqual(ci.collect(gh, gh.pr, gh.checks, self.policy), [])
        self.logs.assert_not_called()

    def test_cloud_plan_only_failure_requests_manual_validation_without_model_call(self):
        gh = CIGitHub()
        # Reuse the real collection path with a policy requiring manual validation.
        with patch.dict(autofix.PROFILE, {"ci_manual_targets": ["backend:core-auth"]}), tempfile.TemporaryDirectory() as directory, patch.object(autofix, "output") as output:
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
        output.assert_called_once_with("ready", "false")
        self.assertEqual(len(gh.writes), 1)
        self.assertIn('"status":"needs_human"', gh.writes[0])
        self.assertIn("No model call", gh.writes[0])

    def test_cloud_plan_cannot_be_reported_as_locally_fixed(self):
        with self.assertRaisesRegex(ValueError, "manual validation"):
            autofix.validate_report(report(), ["key1"], ["key1"])
        self.assertFalse(autofix.validate_report(report("needs_human"), ["key1"], ["key1"]))

    def test_pending_checks_do_not_spend_an_attempt(self):
        gh = CIGitHub()
        gh.checks.append({"status": "in_progress"})
        with tempfile.TemporaryDirectory() as directory:
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=False, directory=directory))
        self.assertEqual(gh.writes, [])
        self.logs.assert_not_called()

    def test_rerun_has_a_new_key_and_old_jobs_cannot_supply_current_failure(self):
        gh = CIGitHub()
        first = ci.collect(gh, gh.pr, gh.checks, self.policy)[0]
        gh.run["run_attempt"] = 2
        second = ci.collect(gh, gh.pr, gh.checks, self.policy)[0]
        self.assertNotEqual(first["key"], second["key"])
        gh.job["check_run_url"] = f"https://api.github.com/repos/{gh.repo}/check-runs/122"
        self.assertEqual(ci.collect(gh, gh.pr, gh.checks, self.policy), [])

    def test_preview_collects_evidence_without_reserving(self):
        gh = CIGitHub()
        with tempfile.TemporaryDirectory() as directory:
            autofix.prepare(gh, SimpleNamespace(number=268, dry_run=True, directory=directory))
            self.assertTrue((Path(directory) / "context.json").exists())
            self.assertFalse((Path(directory) / "prompt.md").exists())
        self.assertEqual(gh.writes, [])


class RetryTests(CIFixture, unittest.TestCase):
    def state(self, gh, attempt=1):
        finding = ci.collect(gh, gh.pr, gh.checks, self.policy)[0]
        return {"run_id": "run1", "status": "running", "keys": [finding["key"]], "ci_keys": [finding["key"]],
                "urls": {finding["key"]: finding["url"]}, "sha": gh.pr["head"]["sha"],
                "branch": gh.pr["head"]["ref"], "attempt": attempt,
                "fingerprint": autofix.fingerprint(gh.pr["head"]["sha"], [finding]), "processed": []}

    def publish_failure(self, gh, state):
        gh.comments = [{"id": 3, "user": {"login": "github-actions[bot]"}, "body": autofix.claim_body(state, "Running")}]
        failure = {"kind": "validation", "error": "pytest exited 1", "log": "AssertionError: expected 2", "patch": "+value = 2"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_RUN_ID": "run1"}), patch.object(autofix, "git") as git:
            (Path(directory) / "validation-failure.json").write_text(json.dumps(failure))
            autofix.publish(gh, SimpleNamespace(number=268, directory=directory, source=directory))
            git.assert_not_called()
        gh.comments = [{"id": 3, "user": {"login": "github-actions[bot]"}, "body": gh.writes[-1]}]
        return autofix.claims(gh.comments)[0]

    def test_validation_failure_is_not_pushed_and_next_attempt_gets_diagnostics(self):
        gh = CIGitHub()
        state = self.publish_failure(gh, self.state(gh))
        self.assertTrue(state["retryable_validation"])
        self.assertEqual(state["status"], "failed")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HAS_OPENAI_KEY": "true", "HAS_PUSH_TOKEN": "true", "GITHUB_RUN_ID": "run2"}):
            autofix.prepare(gh, SimpleNamespace(number=268, directory=directory, dry_run=False))
            context = json.loads((Path(directory) / "context.json").read_text())
        self.assertEqual(context["state"]["attempt"], 2)
        self.assertIn("expected 2", context["previous_validation_failure"]["log"])
        self.assertEqual(context["previous_validation_failure"]["patch"], "+value = 2")

    def test_retry_stops_at_three_and_opt_out_disables_retry(self):
        for attempt, opt_out in [(3, False), (1, True)]:
            gh = CIGitHub()
            state = self.state(gh, attempt=attempt)
            if opt_out:
                gh.pr["labels"] = [{"name": autofix.SKIP_LABEL}]
            result = self.publish_failure(gh, state)
            self.assertFalse(result["retryable_validation"])
        gh = CIGitHub()
        state = self.state(gh)
        state.update(status="failed", retryable_validation=True)
        gh.comments = [{"id": i, "user": {"login": "github-actions[bot]"}, "body": autofix.claim_body(dict(state, attempt=i+1), "Failed")}
                       for i in range(3)]
        with tempfile.TemporaryDirectory() as directory, patch.object(autofix, "output") as output:
            autofix.prepare(gh, SimpleNamespace(number=268, directory=directory, dry_run=False))
        output.assert_called_once_with("ready", "false")
        self.assertEqual(gh.writes, [])

    def test_changed_findings_do_not_receive_an_unrelated_failed_patch(self):
        gh = CIGitHub()
        self.publish_failure(gh, self.state(gh))
        gh.run["run_attempt"] = 2
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HAS_OPENAI_KEY": "true", "HAS_PUSH_TOKEN": "true", "GITHUB_RUN_ID": "run2"}):
            autofix.prepare(gh, SimpleNamespace(number=268, directory=directory, dry_run=False))
            context = json.loads((Path(directory) / "context.json").read_text())
        self.assertNotIn("previous_validation_failure", context)

    def test_setup_or_model_failure_does_not_repeat_the_same_snapshot(self):
        gh = CIGitHub()
        state = self.state(gh)
        state["status"] = "failed"
        gh.comments = [{"id": 1, "user": {"login": "github-actions[bot]"}, "body": autofix.claim_body(state, "Failed") }]
        with tempfile.TemporaryDirectory() as directory, patch.object(autofix, "output") as output:
            autofix.prepare(gh, SimpleNamespace(number=268, directory=directory, dry_run=False))
        output.assert_called_once_with("ready", "false")
        self.assertEqual(gh.writes, [])

    def test_only_ci_fixed_findings_can_use_existing_check_as_regression(self):
        result = report()
        self.assertFalse(autofix.requires_regression(result, {"ci_keys": ["key1"]}))
        self.assertTrue(autofix.requires_regression(result, {}))
        result["findings"].append({"key": "review", "status": "fixed", "explanation": "behavior regression"})
        self.assertTrue(autofix.requires_regression(result, {"ci_keys": ["key1"]}))


class CIPackageTests(CIFixture, unittest.TestCase):
    def test_ci_only_source_fix_runs_trusted_check_without_a_new_test(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root, context_dir = base / "source", base / "context"
            root.mkdir()
            context_dir.mkdir()
            (root / "app").mkdir()
            (root / "app/example.py").write_text("value = 1\n")
            for args in [("init", "-q"), ("config", "user.email", "test@example.com"),
                         ("config", "user.name", "Test"), ("add", "."), ("commit", "-qm", "initial")]:
                autofix.git(root, *args)
            sha = autofix.git(root, "rev-parse", "HEAD").decode().strip()
            finding = {"key": "key1", "kind": "ci", "target": "backend:core-auth"}
            context = {"state": {"sha": sha, "keys": ["key1"], "ci_keys": ["key1"]}, "findings": [finding]}
            (context_dir / "context.json").write_text(json.dumps(context))
            result = report()
            result["tests"] = []
            (context_dir / "agent-report.json").write_text(json.dumps(result))
            (root / "app/example.py").write_text("value = 2\n")
            with patch.object(autofix.project, "verify") as verify:
                autofix.package(SimpleNamespace(directory=str(context_dir), source=str(root)))
            self.assertEqual(verify.call_args.kwargs["findings"], [finding])
            self.assertIn(b"+value = 2", (context_dir / "artifact/change.patch").read_bytes())
            self.assertFalse((context_dir / "artifact/validation-failure.json").exists())


class DiagnosticTests(unittest.TestCase):
    def test_download_redirect_never_forwards_authentication(self):
        request = urllib.request.Request("https://api.github.com/job/logs", headers={"Authorization": "Bearer secret"})
        redirect = ci.LogRedirect().redirect_request(request, None, 302, "Found", {}, "https://logs.blob.core.windows.net/signed")
        self.assertFalse(redirect.has_header("Authorization"))
        with self.assertRaises(ValueError):
            ci.LogRedirect().redirect_request(request, None, 302, "Found", {}, "http://insecure/logs")

    def test_log_redaction_and_retry_bounds(self):
        token = "ghp_" + "x" * 30
        data = ci.redact(f"{token} Authorization: Bearer abc.def.ghi api_key=secret-value password=hunter2")
        for secret in [token, "abc.def.ghi", "secret-value", "hunter2"]:
            self.assertNotIn(secret, data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failure.json"
            path.write_text(json.dumps({"kind": "validation", "error": "failed", "log": token, "patch": "x" * 25_000}))
            result = ci.retry_details(path)
            self.assertNotIn(token, result["log"])
            self.assertLessEqual(len(ci.safe_json(result["patch"])), 16_000)
            path.write_text("x" * 40_001)
            with self.assertRaises(ValueError):
                ci.retry_details(path)

    def test_large_annotations_fit_the_context_budget(self):
        result = ci.bounded_annotations([{"path": "app/example.py", "message": "error " * 1000}] * 20)
        self.assertTrue(result)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 4000)

    def test_expired_logs_fall_back_to_check_metadata(self):
        error = urllib.error.HTTPError("https://api.github.com/logs", 410, "Gone", {}, None)
        with patch.object(ci.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            self.assertIn("HTTP 410", ci.log_excerpt(CIGitHub(), 456))

    def test_transient_log_error_preserves_other_evidence(self):
        error = urllib.error.HTTPError("https://api.github.com/logs", 503, "Unavailable", {}, None)
        with patch.object(ci.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            self.assertIn("HTTP 503", ci.log_excerpt(CIGitHub(), 456))

    def test_long_log_retains_the_actual_failure_at_its_end(self):
        log = io.BytesIO(b"x" * 5_000_000 + b"\nERROR: unique final failure\n")
        excerpt = ci.read_log_excerpt(log)
        self.assertIn("unique final failure", excerpt)
        self.assertLess(len(excerpt), 12_000)

    def test_unicode_and_html_diagnostics_remain_bounded_and_round_trip(self):
        failure = {"kind": "validation", "error": "failed", "log": "\u2603\n" * 10_000,
                   "patch": "<!-- --> @codex \\" * 10_000}
        bounded = ci.bounded_retry(failure)
        encoded = ci.safe_json(bounded)
        self.assertLess(len(encoded.encode()), 24_000)
        self.assertNotIn("-->", encoded)
        self.assertNotIn("@codex", encoded)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failure.json"
            path.write_text(encoded)
            self.assertEqual(ci.retry_details(path), bounded)
        state = {"status": "failed", "validation_failure": bounded}
        body = autofix.claim_body(state, "Validation failed")
        self.assertLess(len(body), 25_000)
        restored = autofix.claims([{"id": 1, "user": {"login": "github-actions[bot]"}, "body": body}])[0]
        self.assertEqual(restored["validation_failure"], bounded)

    def test_real_failed_process_output_is_available_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "validation.log"
            with self.assertRaises(subprocess.CalledProcessError), autofix.project.capture_validation(log):
                autofix.project.run([autofix.sys.executable, "-c", "print('AssertionError: expected 2'); raise SystemExit(1)"], root)
            self.assertIn("AssertionError: expected 2", autofix.project.log_tail(log))
            self.assertIsNone(autofix.project.VALIDATION_LOG)


class VerificationTests(unittest.TestCase):
    def test_backend_ci_reruns_affected_shard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests/auth").mkdir(parents=True)
            (root / "tests/test_smoke.py").touch()
            findings = [{"kind": "ci", "target": "backend:core-auth"}]
            with patch.object(autofix.project, "run") as run:
                autofix.project.verify(root, [], ["app/auth.py"], {"kind": "backend"}, findings)
        self.assertIn("tests/auth", run.call_args.args[0])
        self.assertIn("tests/test_smoke.py", run.call_args.args[0])

    def test_frontend_sdk_ci_reproduces_browser_and_package_checks(self):
        with patch.object(autofix.project, "run") as run:
            autofix.project.verify(Path("/repo"), [], [], {"kind": "frontend"}, [{"kind": "ci", "target": "frontend:embeds"}])
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(["npm", "run", "test:embeds-e2e"], commands)
        self.assertIn(["npm", "--prefix", "packages/kanopy-embeds", "run", "pack:check"], commands)

    def test_embeds_source_repair_runs_browser_checks_even_if_only_unit_ci_failed(self):
        with patch.object(autofix.project, "run") as run:
            autofix.project.verify(Path("/repo"), [], ["src/embed/handler.ts"], {"kind": "frontend"}, [{"kind": "ci", "target": "frontend:unit"}])
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(["npm", "run", "test:embeds-e2e"], commands)
        self.assertIn(["npm", "--prefix", "packages/kanopy-embeds", "run", "pack:check"], commands)

    def test_terraform_syntax_failure_can_reach_the_agent_before_init(self):
        def run(command, root):
            if command[:2] == ["terraform", "init"]:
                raise subprocess.CalledProcessError(1, command)
        with patch.object(autofix.project, "run", side_effect=run):
            autofix.project.setup(Path("/repo"), {"findings": [{"kind": "ci", "target": "infra:terraform"}]}, {"kind": "infra"})

    def test_malformed_ci_target_is_a_nonretryable_policy_error(self):
        with self.assertRaisesRegex(ValueError, "project:check"):
            autofix.project.ci_targets([{"kind": "ci", "target": "python"}])

    def test_python_ci_uses_failing_matrix_interpreter(self):
        with patch.object(autofix.project, "run") as run, patch.dict(os.environ, {"RUNNER_TEMP": "/tmp/runner"}):
            autofix.project.verify(Path("/repo"), [], [], {"kind": "python"}, [{"kind": "ci", "target": "python:3.14"}])
        self.assertEqual(str(run.call_args_list[0].args[0][0]), "/tmp/runner/review-autofix-sdk/3.14/bin/python")
        self.assertIn("tests", run.call_args_list[0].args[0])

    def test_powerline_ci_without_source_annotation_installs_affected_service(self):
        with patch.object(autofix.project, "setup_service") as setup:
            autofix.project.setup(Path("/repo"), {"findings": [{"kind": "ci", "target": "powerline:powerline_analysis", "path": ""}]}, {"kind": "powerline"})
        setup.assert_called_once_with(Path("/repo"), "powerline_analysis")


if __name__ == "__main__":
    unittest.main()
