"""Process-level regressions for completed Codex turns that leave children alive."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import review_autofix_runner as runner


REPORT = {"summary": "fixture", "findings": [
    {"key": "finding", "status": "already_addressed", "explanation": "fixture"}], "tests": []}
SCRIPT_DIRECTORY = Path(__file__).resolve().parent


def fixture(root):
    directory, source = root / "context", root / "source"
    directory.mkdir(); source.mkdir()
    (directory / "context.json").write_text(json.dumps({"state": {"keys": ["finding"]}}))
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    return directory, source


def launcher(directory, source, command, timeout=.8, grace=.15):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "import review_autofix_runner as r, json; from pathlib import Path; "
            "sys.exit(r.supervise(Path(sys.argv[2]),Path(sys.argv[3]),json.loads(sys.argv[4]),"
            "timeout=float(sys.argv[5]),report_grace=float(sys.argv[6]),interval=.02))")
    return [sys.executable, "-c", code, str(SCRIPT_DIRECTORY), str(directory), str(source),
            json.dumps(command), str(timeout), str(grace)]


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory, self.source = fixture(self.root)

    def command(self, mode):
        code = '''import json, os, subprocess, sys, time
from pathlib import Path
directory=Path(sys.argv[1]); mode=sys.argv[2]; report=json.loads(sys.argv[3])
output=directory/'agent-report.json'
if mode=='partial':
    output.write_text('{'); time.sleep(.12)
if mode not in {'timeout','cancel'}:
    if mode=='invalid': report['findings'][0]['key']='other-finding'
    output.write_text(json.dumps(report))
print('model stdout',flush=True); print('model stderr',file=sys.stderr,flush=True)
if mode in {'orphan','detached','timeout','cancel'}:
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
                           start_new_session=mode=='detached')
    (directory/'descendant.pid').write_text(str(child.pid))
    if mode=='detached': time.sleep(.08)
if mode in {'hang','timeout','invalid','cancel'}: time.sleep(60)
if mode=='nonzero': sys.exit(7)
'''
        return [sys.executable, "-c", code, str(self.directory), mode, json.dumps(REPORT)]

    def run_case(self, mode):
        result = subprocess.run(launcher(self.directory, self.source, self.command(mode)),
                                capture_output=True, text=True, timeout=8)
        info = json.loads((self.directory / "artifact/agent-run.json").read_text())
        return result, info

    def assert_descendant_stopped(self):
        pid = int((self.directory / "descendant.pid").read_text())
        table = runner.process_table()
        self.assertTrue(pid not in table or "Z" in table[pid][2], f"Descendant {pid} survived")

    def test_clean_completion_preserves_report_and_both_log_streams(self):
        result, info = self.run_case("normal")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(info["reason"], "completed")
        self.assertIn("model stdout", result.stdout)
        self.assertIn("model stderr", result.stderr)
        self.assertEqual(json.loads((self.directory / "artifact/agent-report.json").read_text()), REPORT)
        self.assertFalse((self.directory / "artifact/change.patch").exists())

    def test_orphan_cannot_keep_runner_stdout_open(self):
        result, info = self.run_case("orphan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(info["reason"], "completed")
        self.assert_descendant_stopped()

    def test_descendant_in_a_separate_session_is_stopped(self):
        result, _ = self.run_case("detached")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_descendant_stopped()

    def test_valid_report_is_recovered_when_direct_process_never_exits(self):
        result, info = self.run_case("hang")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(info["reason"], "recovered_after_report")
        self.assertTrue(info["requires_independent_validation"])

    def test_missing_report_times_out_without_a_publishable_patch(self):
        result, info = self.run_case("timeout")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual(info["reason"], "execution_timeout")
        self.assertFalse((self.directory / "artifact/change.patch").exists())
        self.assert_descendant_stopped()

    def test_report_for_different_findings_cannot_trigger_recovery(self):
        result, info = self.run_case("invalid")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertEqual(info["reason"], "execution_timeout")

    def test_partial_json_is_not_treated_as_a_completed_report(self):
        result, info = self.run_case("partial")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(info["reason"], "completed")

    def test_failed_process_does_not_become_success_because_report_exists(self):
        result, info = self.run_case("nonzero")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(info["reason"], "process_failed")

    def test_cancellation_stops_children_and_never_recovers_as_success(self):
        process = subprocess.Popen(launcher(self.directory, self.source, self.command("cancel"), timeout=10),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not (self.directory / "descendant.pid").exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.directory / "descendant.pid").exists())
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, error)
            self.assertEqual(json.loads((self.directory / "artifact/agent-run.json").read_text())["reason"], "cancelled")
            self.assert_descendant_stopped()
        finally:
            if process.poll() is None:
                process.kill(); process.wait()

    def test_existing_report_is_rejected_before_a_process_is_started(self):
        (self.directory / "agent-report.json").write_text(json.dumps(REPORT))
        with patch.object(runner.subprocess, "Popen") as popen, self.assertRaisesRegex(ValueError, "existing"):
            runner.supervise(self.directory, self.source, ["unused"])
        popen.assert_not_called()

    def test_pid_reuse_is_not_a_cleanup_target(self):
        known = {10: (10, "old start")}
        table = {10: (1, 10, "S", "new start"), 11: (10, 10, "S", "new child")}
        self.assertEqual(runner.descendants(table, 99, known), set())

    def test_adapter_refuses_unreviewed_upstream_content(self):
        (self.root / "action.yml").write_text("unexpected action")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            runner.patch_action(self.root)


def integration(action_path, strategy="unsafe"):
    """Exercise the actual pinned Node wrapper, with no model/API call."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory, source = fixture(root)
        fake = root / "codex"
        fake.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args=sys.argv[1:]
output=Path(args[args.index('--output-last-message')+1])
output.write_text(os.environ['FIXTURE_REPORT'])
print('native fixture stdout',flush=True)
print('native fixture stderr',file=sys.stderr,flush=True)
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
(output.parent/'descendant.pid').write_text(str(child.pid))
''')
        fake.chmod(0o755)
        env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
               "FIXTURE_REPORT": json.dumps(REPORT)}
        command = ["node", str(action_path.resolve() / "dist/main.js"), "run-codex-exec",
                   "--prompt", "fixture", "--prompt-file", "", "--codex-home", "", "--cd", str(source),
                   "--extra-args", "", "--output-file", str(directory / "agent-report.json"),
                   "--output-schema", "", "--output-schema-file", "", "--sandbox", "workspace-write",
                   "--model", "", "--effort", "", "--safety-strategy", strategy, "--codex-user", ""]
        adapter = root / "adapter"; adapter.mkdir()
        shutil.copyfile(action_path / "action.yml", adapter / "action.yml")
        runner.patch_action(adapter)
        assert "review_autofix_runner.py" in (adapter / "action.yml").read_text()
        # Red: the unmodified wrapper gives the orphan our capture pipes. The
        # final report exists, but the caller cannot observe EOF and finish.
        baseline_command = command.copy()
        baseline_command[baseline_command.index("--safety-strategy") + 1] = "unsafe"
        baseline = subprocess.Popen(baseline_command, cwd=source, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        try:
            try:
                baseline.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                assert (directory / "agent-report.json").exists(), "Baseline did not finish the model turn"
            else:
                raise AssertionError("Baseline did not reproduce the inherited-stream hang")
        finally:
            try:
                os.killpg(baseline.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            baseline.communicate(timeout=5)
        (directory / "agent-report.json").unlink()
        (directory / "descendant.pid").unlink()
        # Green: the same pinned wrapper finishes with the supervisor, and the
        # production Linux privilege-drop path is exercised on a hosted runner.
        result = subprocess.run(launcher(directory, source, command, timeout=30, grace=2),
                                capture_output=True, text=True, env=env, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "native fixture stdout" in result.stdout
        assert "native fixture stderr" in result.stderr
        assert json.loads((directory / "agent-report.json").read_text()) == REPORT
        pid = int((directory / "descendant.pid").read_text())
        table = runner.process_table()
        assert pid not in table or "Z" in table[pid][2], "Fixture descendant survived cleanup"
        print("Pinned Codex action regression passed: final report preserved, logs forwarded, orphan stopped; no API call.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--action-path":
        integration(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "unsafe")
    else:
        unittest.main()
