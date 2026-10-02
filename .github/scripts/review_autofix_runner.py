"""Bound Codex execution without giving descendants the runner's log pipes."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import review_autofix as control


ACTION_SHA256 = "448852e9e59565440bf25174b54bbf661bd66102be295af2e526fa09eaaf3bd7"
ACTION_COMMAND = 'exec env -u NODE_OPTIONS NODE_OPTIONS=--disable-sigusr1 node --disable-sigusr1 "$ACTION_PATH/dist/main.js" run-codex-exec \\'


def patch_action(root):
    path = root / "action.yml"
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != ACTION_SHA256:
        raise ValueError("Unexpected Codex action content; review the pinned action before updating its adapter")
    command = ('exec python3 "$REVIEW_CONTROL_DIRECTORY/review_autofix_runner.py" run '
               '--directory "$REVIEW_DIRECTORY" --source "$CODEX_WORKING_DIRECTORY" -- '
               + ACTION_COMMAND.removeprefix("exec "))
    text = original.decode()
    if text.count(ACTION_COMMAND) != 1:
        raise ValueError("Cannot locate the pinned Codex execution boundary")
    path.write_text(text.replace(ACTION_COMMAND, command))


def valid_report(path, context):
    try:
        if path.is_symlink() or path.stat().st_size > 128_000:
            return None
        data = path.read_bytes()
        report = json.loads(data)
        if not isinstance(report.get("summary"), str):
            return None
        control.validate_report(report, context["state"]["keys"], context["state"].get("manual_keys", []))
        return hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def process_table():
    with subprocess.Popen(["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as listing:
        try:
            output, error = listing.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            listing.kill()
            listing.communicate()
            raise
        if listing.returncode:
            raise RuntimeError(f"Cannot inspect Codex processes: {error}")
    table = {}
    for line in output.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5:
            pid, parent, group = map(int, fields[:3])
            if pid != listing.pid:  # The snapshot command has already exited.
                table[pid] = (parent, group, fields[3], fields[4])
    return table


def descendants(table, root, known):
    # Keep identities across reparenting, but never signal a reused PID.
    owned = {pid for pid, identity in known.items()
             if pid in table and (table[pid][1], table[pid][3]) == identity}
    parents = {root, *owned}
    while True:
        added = {pid for pid, row in table.items() if row[0] in parents} - parents
        if not added:
            break
        owned.update(added)
        parents.update(added)
    for pid in owned:
        known[pid] = (table[pid][1], table[pid][3])
    return {pid for pid in owned if "Z" not in table[pid][2]}


def enable_subreaper():
    # Linux adopts even orphaned descendants that created their own session.
    # macOS uses process-group cleanup plus identities observed while running.
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "Cannot enable child-process recovery")


def stop_children(process, known, grace=2):
    for sig, duration in [(signal.SIGTERM, grace), (signal.SIGKILL, grace)]:
        # This is the private session created below, not the runner's group.
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        until = time.monotonic() + duration
        while True:
            table = process_table()
            remaining = descendants(table, os.getpid(), known)
            for pid in remaining:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    # The official action's root-owned sudo monitor exits when
                    # its unprivileged workload stops. Verify that below too.
                    pass
            process.poll()
            if not remaining:
                process.wait(timeout=1)
                if sys.platform == "linux":
                    try:
                        while os.waitpid(-1, os.WNOHANG)[0]:
                            pass
                    except ChildProcessError:
                        pass
                return
            if time.monotonic() >= until:
                break
            time.sleep(.05)
    raise RuntimeError("Codex descendants could not be stopped; refusing to validate a changing checkout")


def diagnostics(directory, source, result):
    artifact = directory / "artifact"
    artifact.mkdir(exist_ok=True)
    (artifact / "agent-run.json").write_text(json.dumps(result, indent=2) + "\n")
    tails = []
    for name in ["agent-stdout.log", "agent-stderr.log"]:
        path = directory / name
        tails.append(name + "\n" + control.ci.redact(control.project.log_tail(path, 12_000)))
    (artifact / "agent-log.txt").write_text("\n".join(tails))
    report = directory / "agent-report.json"
    if not report.is_symlink() and report.is_file() and report.stat().st_size <= 128_000:
        (artifact / "agent-report.json").write_bytes(report.read_bytes())
    try:
        # Recovery evidence has a different filename from the validated patch.
        # The publisher never treats these files as authorization to push.
        subprocess.run(["git", "-C", str(source), "add", "--all"], check=True,
                       capture_output=True, timeout=15)
        patch = subprocess.check_output(["git", "-C", str(source), "diff", "--cached", "--binary", "HEAD"], timeout=15)
        if len(patch) <= 2_000_000:
            (artifact / "candidate.patch").write_bytes(patch)
    except (OSError, subprocess.SubprocessError):
        print("Could not snapshot the candidate; the agent report and diagnostic log were preserved.", flush=True)


def supervise(directory, source, command, *, timeout=1800, report_grace=60, interval=.2):
    context = json.loads((directory / "context.json").read_text())
    report = directory / "agent-report.json"
    if report.exists() or report.is_symlink():
        raise ValueError("Refusing to reuse an existing agent report")
    enable_subreaper()
    cancelled = []
    old_handlers = {}
    for sig in [signal.SIGTERM, signal.SIGINT]:
        old_handlers[sig] = signal.signal(sig, lambda received, _frame: cancelled.append(received))
    started = time.monotonic()
    known, previous_report, ready_since = {}, None, None
    reason, code, process = "error", 1, None
    heartbeat = started + 30
    outputs, readers = [], []
    try:
        for name in ["agent-stdout.log", "agent-stderr.log"]:
            path = directory / name
            outputs.append(path.open("wb", buffering=0))
            readers.append(path.open("rb", buffering=0))
        # Regular files are essential: surviving descendants cannot retain
        # GitHub's stdout/stderr transport, even if the action wrapper exits.
        process = subprocess.Popen(command, cwd=source, stdin=subprocess.DEVNULL,
                                   stdout=outputs[0], stderr=outputs[1], start_new_session=True)

        def forward():
            for stream, destination in zip(readers, [sys.stdout, sys.stderr]):
                chunk = stream.read(65_536)
                if chunk:
                    destination.write(chunk.decode("utf-8", errors="replace"))
                    destination.flush()
            return any(stream.tell() < os.fstat(stream.fileno()).st_size for stream in readers)

        while True:
            forward()
            now = time.monotonic()
            descendants(process_table(), os.getpid(), known)
            current = valid_report(report, context)
            if current != previous_report:
                previous_report, ready_since = current, now if current else None
                if current:
                    print("Codex final report received; waiting for clean process exit.", flush=True)
            if cancelled:
                reason, code = "cancelled", 128 + cancelled[0]
                break
            exit_code = process.poll()
            if exit_code is not None:
                current = valid_report(report, context)
                previous_report = current
                reason = "completed" if exit_code == 0 and current else "process_failed"
                code = 0 if reason == "completed" else 1
                break
            if ready_since is not None and now - ready_since >= report_grace:
                reason, code = "recovered_after_report", 0
                print("Codex did not exit after its final report; stopping its process tree before independent validation.", flush=True)
                break
            if now - started >= timeout:
                reason, code = "execution_timeout", 124
                break
            if now >= heartbeat:
                print(f"Codex supervisor: {int(now - started)}s elapsed; final report {'ready' if current else 'not yet received'}.", flush=True)
                heartbeat = now + 30
            time.sleep(interval)
        stop_children(process, known)
        while forward():
            pass
        # A changing, removed, or malformed report never authorizes recovery.
        if code == 0 and valid_report(report, context) != previous_report:
            reason, code = "report_changed_during_cleanup", 1
        if cancelled:
            reason, code = "cancelled", 128 + cancelled[0]
    except Exception as exc:
        print(f"Codex supervisor failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        reason, code = "supervisor_failed", 1
        if process is not None:
            try:
                stop_children(process, known)
            except Exception as cleanup:
                print(f"Process cleanup failed: {cleanup}", file=sys.stderr, flush=True)
    finally:
        for stream in [*outputs, *readers]:
            stream.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        diagnostics(directory, source, {"reason": reason, "exit_code": code,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "requires_independent_validation": True})
    return code


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    patch = sub.add_parser("patch-action")
    patch.add_argument("path", type=Path)
    run = sub.add_parser("run")
    run.add_argument("--directory", type=Path, required=True)
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.operation == "patch-action":
        patch_action(args.path)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    return supervise(args.directory.resolve(), args.source.resolve(), command)


if __name__ == "__main__":
    sys.exit(main())
