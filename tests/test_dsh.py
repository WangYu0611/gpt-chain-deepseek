from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from relay.dsh import DshError, DshRunner


FAKE_DSH = r'''
import json, os, subprocess, sys, time
from pathlib import Path
mode = os.environ.get("FAKE_MODE", "ok")
prompt = sys.stdin.buffer.read().decode("utf-8")
session = "session-test"
if "--session-id" in sys.argv:
    session = sys.argv[sys.argv.index("--session-id") + 1]
if mode == "wrong_session":
    session = "wrong"
print(json.dumps({"type":"session", "sessionId":session, "cwd":os.getcwd()}), flush=True)
if mode == "timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid))
    print("still running", file=sys.stderr, flush=True)
    time.sleep(60)
if mode == "malformed":
    print("NOT JSON", flush=True)
if mode == "missing_final":
    sys.exit(0)
if mode == "secret":
    print(os.environ["FAKE_SECRET_TOKEN"], file=sys.stderr, flush=True)
reason = {"kind": "error" if mode == "blocked" else "completed"}
text = json.dumps({"prompt":prompt, "cwd":os.getcwd(), "args":sys.argv[1:],
                   "home":os.environ.get("DSH_HOME", "")}, ensure_ascii=False)
print(json.dumps({"type":"final", "sessionId":session, "reason":reason, "text":text}, ensure_ascii=False), flush=True)
if mode == "after_final":
    print(json.dumps({"type":"status", "phase":"unexpected"}), flush=True)
sys.exit(7 if mode == "nonzero" else 0)
'''


class DshRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay dsh 中文 ")
        self.root = Path(self.temp.name)
        self.worker = self.root / "fake dsh 中文.py"
        self.worker.write_text(FAKE_DSH, encoding="utf-8")
        self.workspace = self.root / "project 中文 space"
        self.workspace.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, mode="ok", **overrides):
        config = {"executable":sys.executable, "args":[str(self.worker)],
                  "env":{"FAKE_MODE":mode}, "timeout_seconds":10}
        config.update(overrides)
        return DshRunner(config, self.root / "runtime")

    def test_utf8_prompt_and_directory(self):
        text = '拉取 main，完成任务。 Literal: & | % $(hello) "quote"\n第二行'
        result = self.runner().run(text, self.workspace, "unicode")
        payload = json.loads(result.answer)
        self.assertEqual(payload["prompt"], text)
        self.assertEqual(Path(payload["cwd"]), self.workspace)
        self.assertIn("--json", payload["args"])
        self.assertEqual(payload["args"][-1], "-")
        self.assertEqual(result.session_id, "session-test")
        self.assertEqual(result.exit_code, 0)

    def test_resume_and_home_patch(self):
        patch = self.root / "账号 patch.yml"
        patch.write_text("[]", encoding="utf-8")
        result = self.runner(home=str(self.root), patch=str(patch)).run(
            "continue", self.workspace, "resume", session_id="session-existing"
        )
        payload = json.loads(result.answer)
        self.assertEqual(payload["home"], str(self.root))
        self.assertIn(str(patch), payload["args"])
        self.assertEqual(result.session_id, "session-existing")

    def test_global_patch_precedes_profile_and_preserves_entry_script(self):
        patch = self.root / "账号 patch.yml"
        patch.write_text("[]", encoding="utf-8")
        result = self.runner(
            args=[str(self.worker), "--profile", "headless", "--json"], patch=str(patch)
        ).run("task", self.workspace, "patch-order")
        # Python could not execute the fixture if --patch had moved ahead of
        # the entry script. This is also the Node cli.js-prefix arrangement.
        args = json.loads(result.answer)["args"]
        self.assertEqual(args[:4], ["--patch", str(patch), "--profile", "headless"])
        self.assertLess(args.index("--patch"), args.index("--json"))
        self.assertEqual(args[-1], "-")

    def test_nonzero_completed_turn_is_failure(self):
        with self.assertRaises(DshError) as failure:
            self.runner("nonzero").run("task", self.workspace, "nonzero")
        self.assertEqual(failure.exception.exit_code, 7)
        self.assertTrue(failure.exception.events_path.is_file())

    def test_incomplete_or_invalid_event_stream_pauses(self):
        for mode in ("blocked", "missing_final", "malformed", "after_final", "wrong_session"):
            with self.subTest(mode=mode), self.assertRaises(DshError):
                self.runner(mode).run("task", self.workspace, mode, session_id="session-test")

    def test_run_id_is_not_replayed(self):
        self.runner().run("task", self.workspace, "once")
        with self.assertRaises(DshError):
            self.runner().run("task", self.workspace, "once")

    def test_explicit_secrets_not_in_logs(self):
        secret = "private-secret-value-12345"
        worker = self.runner("secret", env={"FAKE_MODE":"secret", "FAKE_SECRET_TOKEN":secret})
        result = worker.run("task", self.workspace, "secret")
        self.assertNotIn(secret, result.events_path.with_name("secret.stderr.log").read_text())
        self.assertIn("[REDACTED]", result.events_path.with_name("secret.stderr.log").read_text())

    def test_timeout_terminates_child_process_tree(self):
        pid_file = self.root / "child.pid"
        runner = self.runner("timeout", timeout_seconds=2,
                             env={"FAKE_MODE":"timeout", "FAKE_CHILD_PID":str(pid_file)})
        started = time.monotonic()
        with self.assertRaises(DshError) as failure:
            runner.run("task", self.workspace, "timeout")
        self.assertIn("超时", str(failure.exception))
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    code = ctypes.c_ulong()
                    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                    self.assertNotEqual(code.value, 259)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        else:
            # A dead child may be reaped asynchronously by the system init.
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists():
                self.assertEqual(stat.read_text().split()[2], "Z")

    @unittest.skipUnless(os.name == "nt", "Windows cmd launcher")
    def test_cmd_shim_spaces_and_chinese_paths(self):
        shim = self.root / "fake dsh 中文.cmd"
        shim.write_text('@echo off\r\nchcp 65001 >nul\r\n"' + sys.executable + '" "' + str(self.worker) + '" %*\r\n', encoding="utf-8")
        directory_arg = str(self.workspace) + "\\"
        patch = self.root / "账号 patch.yml"
        patch.write_text("[]", encoding="utf-8")
        result = self.runner(executable=str(shim), args=["--profile", "headless", "--json", "--add-dir", directory_arg], patch=str(patch)).run(
            "中文任务 & % |", self.workspace, "cmd"
        )
        payload = json.loads(result.answer)
        self.assertEqual(payload["prompt"], "中文任务 & % |")
        self.assertEqual(payload["args"][:4], ["--patch", str(patch), "--profile", "headless"])
        self.assertEqual(payload["args"][payload["args"].index("--add-dir") + 1], directory_arg)

    @unittest.skipUnless(os.name == "nt", "Windows cmd launcher")
    def test_cmd_metacharacters_are_rejected(self):
        shim = self.root / "fake.cmd"
        shim.write_text("@echo off", encoding="ascii")
        with self.assertRaises(DshError):
            self.runner(executable=str(shim), args=["--patch", "x&echo leaked"]).run(
                "task", self.workspace, "unsafe"
            )

    def parse_events(self, events, name, exit_code=0, requested_session=None):
        path = self.root / (name + ".jsonl")
        path.write_text("".join((event if isinstance(event, str) else json.dumps(event)) + "\n" for event in events), encoding="utf-8")
        return self.runner()._read_result(path, exit_code, requested_session)

    def legacy_events(self):
        return [
            {"type":"session", "sessionId":"session-legacy", "cwd":str(self.workspace)},
            {"type":"status", "phase":"turn_start", "turn":1},
            {"type":"status", "phase":"step_start", "turn":1, "step":1},
            {"type":"status", "phase":"step_end", "turn":1, "step":1},
            {"type":"status", "phase":"turn_end", "turn":1, "reason":{"kind":"completed"}},
            {"type":"final", "text":"Legacy task complete."},
        ]

    def test_legacy_real_schema_completed_turn_is_accepted(self):
        result = self.parse_events(self.legacy_events(), "legacy-success", requested_session="session-legacy")
        self.assertEqual(result.session_id, "session-legacy")
        self.assertEqual(result.answer, "Legacy task complete.")

    def test_legacy_requires_latest_started_turn_to_close(self):
        base = self.legacy_events()
        cases = {
            "no-start":[base[0], base[-2], base[-1]],
            "cross-turn":[base[0], base[1], dict(base[-2], turn=2), base[-1]],
            "last-unclosed":[*base[:-1], {"type":"status", "phase":"turn_start", "turn":2}, base[-1]],
            "prior-end-reused":[*base[:-1], {"type":"status", "phase":"turn_start", "turn":2}, base[-2], base[-1]],
            "error-turn":[base[0], base[1], dict(base[-2], reason={"kind":"error", "error":{"code":"MISSING_CREDENTIAL"}}), base[-1]],
            "no-session":base[1:],
            "empty-answer":[*base[:-1], {"type":"final", "text":""}],
        }
        for name, events in cases.items():
            with self.subTest(name=name), self.assertRaises(DshError):
                self.parse_events(events, "legacy-" + name)

    def test_partial_new_final_cannot_fall_back_to_legacy_completion(self):
        finals = [
            {"type":"final", "sessionId":"session-legacy", "text":"Done"},
            {"type":"final", "reason":{"kind":"completed"}, "text":"Done"},
            {"type":"final", "sessionId":None, "reason":{"kind":"completed"}, "text":"Done"},
            {"type":"final", "sessionId":"session-legacy", "reason":None, "text":"Done"},
            {"type":"final", "text":"Done", "unexpected":True},
        ]
        for index, final in enumerate(finals):
            with self.subTest(final=final), self.assertRaises(DshError):
                self.parse_events([*self.legacy_events()[:-1], final], f"legacy-partial-{index}")

    def test_legacy_error_duplicate_final_and_malformed_json_are_rejected(self):
        base = self.legacy_events()
        cases = [
            [*base[:-1], {"type":"error", "message":"launcher failed"}, base[-1]],
            [*base, base[-1]],
            [*base[:-1], "not JSON", base[-1]],
            [base[0], *base],
            [dict(base[0], sessionId=None), *base],
        ]
        for index, events in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(DshError):
                self.parse_events(events, f"legacy-invalid-{index}")

    def test_legacy_completion_still_requires_exit_zero_and_requested_session(self):
        with self.assertRaises(DshError):
            self.parse_events(self.legacy_events(), "legacy-nonzero", exit_code=1)
        with self.assertRaises(DshError):
            self.parse_events(self.legacy_events(), "legacy-wrong-session", requested_session="different-session")

    def test_legacy_new_turn_clears_previous_terminal_reason(self):
        events = self.legacy_events()
        second = [
            {"type":"status", "phase":"turn_start", "turn":2},
            {"type":"status", "phase":"turn_end", "turn":2, "reason":{"kind":"completed"}},
        ]
        # The final completed turn can follow an earlier error, but it must
        # establish its own matching terminal outcome.
        events[-2] = dict(events[-2], reason={"kind":"error"})
        result = self.parse_events([*events[:-1], *second, events[-1]], "legacy-last-completed")
        self.assertEqual(result.session_id, "session-legacy")


if __name__ == "__main__":
    unittest.main()
