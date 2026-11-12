"""Claude Code runs and retries must preserve the coding-task checkpoint."""
from pathlib import Path
import copy
import json
import os
import shlex
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult, safe_environment
from tests.test_unattended_providers import write_events
from unattended.providers import (
    AgentProviders, ProviderError, _claude_log_record, _claude_validation_read_directory,
    claude_subscription_ready, classify_transient_failure, read_session,
)


SESSION = "550e8400-e29b-41d4-a716-446655440000"
OTHER = "550e8400-e29b-41d4-a716-446655440001"


def events(*, session=SESSION, is_error=False, extra=()):
    return [
        {"type": "system", "subtype": "init", "session_id": session},
        *extra,
        {"type": "result", "subtype": "success", "session_id": session,
         "is_error": is_error, "result": "Committed and ready for independent checks."},
    ]


class ClaudeCodeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root / "code"
        self.settings = {"command": ["claude.exe"], "model": "sonnet", "reasoning_effort": "high"}
        self.provider = AgentProviders({"coding_provider": "claude", "claude": self.settings})

    def capture(self, rows, exit_code=0):
        self.out.mkdir(exist_ok=True)
        write_events(self.out / "stdout.log", rows)
        (self.out / "stderr.log").write_text("", encoding="utf-8")
        return ProcessResult(exit_code, False, False, self.out / "stdout.log", self.out / "stderr.log")

    def receipt(self, rows, exit_code=0, requested_session=None):
        self.capture(rows, exit_code)
        (self.out / "process.json").write_text(json.dumps({
            "kind": "code", "coding_provider": "claude", "exit_code": exit_code,
            "requested_session": requested_session, "timed_out": False, "output_limit_exceeded": False,
        }), encoding="utf-8")

    def managed_validation(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (self.root / "runs").mkdir()
        self.out = self.root / "runs" / "code-validation"
        task_id = "TASK-LOCAL-0067"
        validation = self.root / "tasks" / task_id / "validation"
        (validation / "check-1").mkdir(parents=True)
        (validation / "check-1" / "receipt.json").write_text('{"passed":false}', encoding="utf-8")
        (self.root / "tasks" / "TASK-LOCAL-0068" / "validation").mkdir(parents=True)
        state = {"phase": "CODE", "task": {"id": task_id},
                 "operation": {"kind": "code", "task_id": task_id, "output_dir": str(self.out)}}
        (self.root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.provider.config["runtime_dir"] = str(self.root)
        return workspace, validation, state

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_current_validation_logs_are_read_only_without_wider_directory_or_bash_permissions(self, launch, auth):
        workspace, validation, _ = self.managed_validation()
        state_path = self.root / "state.json"
        before = state_path.read_bytes(), state_path.stat().st_mtime_ns
        launch.side_effect = lambda *args, **kwargs: self.capture(events())
        self.provider.code("Read this task's failed validation evidence.", workspace, self.out, 120)
        argv = launch.call_args.args[0]
        permissions = json.loads(argv[argv.index("--settings") + 1])["permissions"]
        self.assertTrue(permissions["blockReadsOutsideWorkingDirectories"])
        self.assertEqual(permissions["additionalDirectories"], [str(validation.resolve())])
        pattern = validation.resolve().as_posix()
        if validation.drive:
            pattern = "/" + validation.drive[0].lower() + pattern[2:]
        self.assertIn(f"Edit(/{pattern})", permissions["deny"])
        self.assertIn(f"Edit(/{pattern}/**)", permissions["deny"])
        prompt = launch.call_args.kwargs["stdin"].decode()
        self.assertIn(validation.resolve().as_posix(), prompt)
        self.assertIn("Saved/Automation/TASK-LOCAL-0067/validation-results.json", prompt)
        self.assertEqual((state_path.read_bytes(), state_path.stat().st_mtime_ns), before)
        bash_permissions = [part for part in argv if part.startswith("Bash(")]
        self.provider.config.pop("runtime_dir")
        self.out = self.root / "runs" / "code-baseline"
        self.provider.code("Continue normally.", workspace, self.out, 120)
        baseline = launch.call_args.args[0]
        self.assertEqual([part for part in baseline if part.startswith("Bash(")], bash_permissions)
        baseline_permissions = json.loads(baseline[baseline.index("--settings") + 1])["permissions"]
        self.assertNotIn("additionalDirectories", baseline_permissions)

    def test_validation_reader_requires_exact_managed_task_and_operation_binding(self):
        workspace, validation, state = self.managed_validation()
        state_path = self.root / "state.json"
        cases = []
        for key, value in (("kind", "review"), ("task_id", "TASK-LOCAL-0068"),
                           ("output_dir", str(self.out.parent / "code-other"))):
            candidate = copy.deepcopy(state)
            candidate["operation"][key] = value
            cases.append(candidate)
        for task_id in ("../TASK-LOCAL-0068", "TASK-LOCAL-0068"):
            candidate = copy.deepcopy(state)
            candidate["task"]["id"] = task_id
            cases.append(candidate)
        cases.extend([state | {"phase": "REVIEW"}, state | {"operation": None}, []])
        for candidate in cases:
            with self.subTest(state=candidate):
                state_path.write_text(json.dumps(candidate), encoding="utf-8")
                self.assertIsNone(_claude_validation_read_directory(self.provider.config, workspace, self.out))
        state_path.write_text(json.dumps(state), encoding="utf-8")
        self.assertIsNone(_claude_validation_read_directory(self.provider.config, self.root / "other-workspace", self.out))
        self.assertIsNone(_claude_validation_read_directory(self.provider.config, workspace, self.root / "code-other"))
        no_logs = copy.deepcopy(state)
        no_logs["task"]["id"] = no_logs["operation"]["task_id"] = "TASK-LOCAL-0099"
        state_path.write_text(json.dumps(no_logs), encoding="utf-8")
        self.assertIsNone(_claude_validation_read_directory(self.provider.config, workspace, self.out))
        self.assertTrue(validation.is_dir())

    def test_validation_reader_rejects_reparse_paths_and_linked_descendants(self):
        workspace, validation, _ = self.managed_validation()
        for linked in (workspace, self.root / "runs", self.out, self.root / "state.json",
                       self.root / "tasks", validation.parent, validation):
            with self.subTest(linked=linked), \
                    patch("unattended.ue_checks._is_reparse", side_effect=lambda path: path == linked):
                self.assertIsNone(_claude_validation_read_directory(self.provider.config, workspace, self.out))
        linked = validation / "check-1" / "receipt.json"
        with patch("unattended.providers._reparse", side_effect=lambda path: path == linked):
            self.assertIsNone(_claude_validation_read_directory(self.provider.config, workspace, self.out))

    def test_validation_reader_rejects_an_actual_directory_link_to_another_task(self):
        workspace, validation, _ = self.managed_validation()
        target = self.root / "tasks" / "TASK-LOCAL-0068" / "validation"
        link = validation / "foreign"
        created = False
        if os.name == "nt":
            try:
                import _winapi
                _winapi.CreateJunction(str(target), str(link))
                created = True
            except (AttributeError, OSError):
                pass
        if not created:
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("Creating directory links is unavailable")
        self.assertIsNone(_claude_validation_read_directory(self.provider.config, workspace, self.out))

    def test_only_native_pro_login_passes_preflight_and_no_proxy_keys_are_forwarded(self):
        safe = dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty", subscriptionType="pro")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_AUTH_TOKEN": "test-token",
                                     "ANTHROPIC_BASE_URL": "https://proxy.invalid"}), \
                patch("unattended.providers.subprocess.run") as launch:
            launch.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(safe).encode())
            self.assertTrue(claude_subscription_ready(self.settings, self.root))
            argv = launch.call_args.args[0]
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
            self.assertEqual(argv[-3:], ["auth", "status", "--json"])
            self.assertNotIn("ANTHROPIC_API_KEY", launch.call_args.kwargs["env"])
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", launch.call_args.kwargs["env"])
            self.assertNotIn("ANTHROPIC_BASE_URL", launch.call_args.kwargs["env"])
            launch.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(
                dict(safe, authMethod="api_key")).encode())
            self.assertFalse(claude_subscription_ready(self.settings, self.root))

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_code_resumes_exact_session_and_recovers_its_receipt_offline(self, launch, auth):
        launch.side_effect = lambda *args, **kwargs: self.capture(events())
        result = self.provider.code("Implement the issued task.", self.root, self.out, 120, session_id=SESSION)
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--resume") + 1], SESSION)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertTrue(launch.call_args.kwargs["stdin"].startswith(b"Implement the issued task.\n"))
        self.assertTrue(result["completed"])
        self.assertEqual(result["session_id"], SESSION)
        self.assertEqual(AgentProviders({}).recover("code", self.out), result)
        self.assertEqual(read_session(self.out), SESSION)
        launch.assert_called_once()

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_optional_preflight_uses_exact_commands_and_scopes_tests_to_task(self, launch, auth):
        workspace = self.root / "workspace"
        (self.root / "runs").mkdir()
        self.out = self.root / "runs" / "code-test"
        ue = self.root / "Unreal Engine" / "Editor" / "UnrealEditor.exe"
        dotnet = self.root / "Unreal Engine" / "DotNet" / "dotnet.exe"
        ubt = self.root / "Unreal Engine" / "UnrealBuildTool" / "UnrealBuildTool.dll"
        self.settings.update(ue_preflight=True, visual_preflight={
            "task_id": "TASK-LOCAL-0018", "argv": [str(ue), "{workspace}/Project.uproject",
                                                  "-abslog={output_dir}/Visual.log"],
        }, test_preflight={
            "task_id": "TASK-LOCAL-0018",
            "commands": [[str(ue), "{workspace}/Project.uproject", "-nullrhi",
                          f"-ExecCmds=Automation RunTests ^ZhuGuoCity.M2.{name}$", f"-abslog={{output_dir}}/{name}.log"]
                         for name in ("Save", "Recipes")],
        })
        self.provider.config.update(runtime_dir=str(self.root), checks=[{
            "id": "ue-editor-build", "argv": [str(dotnet), str(ubt), "-project={workspace}/Project.uproject"],
        }])
        expected_build = shlex.join([dotnet.as_posix(), ubt.as_posix(),
                                     "-project=" + (workspace / "Project.uproject").as_posix()])
        launch.side_effect = lambda *args, **kwargs: self.capture(events())

        for task_id in ("TASK-LOCAL-0018", "TASK-LOCAL-0019"):
            with self.subTest(task_id=task_id):
                self.out = self.root / "runs" / ("code-" + task_id)
                (self.root / "state.json").write_text(json.dumps({
                    "phase": "CODE", "task": {"id": task_id},
                    "operation": {"kind": "code", "task_id": task_id, "output_dir": str(self.out)},
                }), encoding="utf-8")
                self.provider.code("Continue", workspace, self.out, 120)
                argv = launch.call_args.args[0]
                self.assertIn(f"Bash({expected_build})", argv)
                permissions = json.loads(argv[argv.index("--settings") + 1])["permissions"]
                self.assertTrue(permissions["blockReadsOutsideWorkingDirectories"])
                expected_dirs = [dotnet.parent, ubt.parent]
                expected_visual = shlex.join([ue.as_posix(), (workspace / "Project.uproject").as_posix(),
                                             "-abslog=" + (workspace / "Saved" / "Logs" / self.out.name / "Visual.log").as_posix()])
                expected_tests = [shlex.join([ue.as_posix(), (workspace / "Project.uproject").as_posix(), "-nullrhi",
                                             f"-ExecCmds=Automation RunTests ^ZhuGuoCity.M2.{name}$",
                                             "-abslog=" + (workspace / "Saved" / "Logs" / self.out.name / f"{name}.log").as_posix()])
                                  for name in ("Save", "Recipes")]
                prompt = launch.call_args.kwargs["stdin"].decode("utf-8")
                self.assertIn(expected_build, prompt)
                if task_id == "TASK-LOCAL-0018":
                    expected_dirs.append(ue.parent)
                    self.assertIn(f"Bash({expected_visual})", argv)
                    for command in expected_tests:
                        self.assertIn(f"Bash({command})", argv)
                    prompt_positions = [prompt.index(command) for command in (expected_build, *expected_tests, expected_visual)]
                    self.assertEqual(prompt_positions, sorted(prompt_positions))
                    self.assertIn("退出码为 0 不能替代测试通过", prompt)
                else:
                    for command in (*expected_tests, expected_visual):
                        self.assertNotIn(f"Bash({command})", argv)
                        self.assertNotIn(command, prompt)
                self.assertEqual(permissions["additionalDirectories"], [str(path.resolve()) for path in expected_dirs])
                for directory in expected_dirs:
                    pattern = directory.resolve().as_posix()
                    if directory.drive:
                        pattern = "/" + directory.drive[0].lower() + pattern[2:]
                    self.assertIn(f"Edit(/{pattern}/**)", permissions["deny"])

        with self.assertRaisesRegex(ProviderError, "active persisted coding operation"):
            self.provider.code("Continue", workspace, self.out.parent / "code-other", 120)
        self.provider.config["checks"][0]["argv"].append("*")
        with self.assertRaisesRegex(ProviderError, "wildcards"):
            self.provider.code("Continue", workspace, self.out, 120)
        self.assertEqual(launch.call_count, 2)

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_planned_core_tests_grant_exact_nullrhi_commands_without_duplicate_scoped_test(self, launch, auth):
        workspace = self.root / "workspace"
        workspace.mkdir()
        project = workspace / "Project.uproject"
        project.write_text("{}", encoding="utf-8")
        engine = self.root / "Engine"
        binaries = engine / "Binaries" / "Win64"
        binaries.mkdir(parents=True)
        ue = binaries / "UnrealEditor-Cmd.exe"
        ue.touch()
        (binaries / "UnrealEditor.exe").touch()
        (self.root / "runs").mkdir()
        self.out = self.root / "runs" / "code-planned"
        task_id = "TASK-LOCAL-0021"
        name = "ZhuGuoCity.M2.NewCase"
        selector = f"-ExecCmds=Automation RunTests ^{name}$"
        self.settings.update(ue_preflight=True, planned_core_tests=True, test_preflight={
            "task_id": task_id,
            "commands": [[str(ue), str(project), selector, "-abslog={output_dir}/older.log"]],
        })
        self.provider.config.update(runtime_dir=str(self.root), checks=[
            {"id": "ue-editor-build", "argv": [str(engine / "build.exe")]},
            {"id": "ue-task-validation", "argv": ["python", "ue_checks.py", "--engine", str(engine)]},
        ])
        (self.root / "state.json").write_text(json.dumps({
            "phase": "CODE", "task": {"id": task_id, "core_tests": [name]},
            "operation": {"kind": "code", "task_id": task_id, "output_dir": str(self.out)},
        }), encoding="utf-8")
        launch.side_effect = lambda *args, **kwargs: self.capture(events())

        self.provider.code("Continue", workspace, self.out, 120)

        argv = launch.call_args.args[0]
        matches = [arg for arg in argv if arg.startswith("Bash(") and selector in arg]
        self.assertEqual(len(matches), 1)
        self.assertIn(ue.as_posix(), matches[0])
        self.assertIn("core-1.log", matches[0])
        self.assertIn("-nullrhi", matches[0])
        prompt = launch.call_args.kwargs["stdin"].decode("utf-8")
        self.assertIn("同名核心测试", prompt)
        self.assertIn("test_batches（字符串数组的数组）", prompt)
        self.assertIn("每批最多16个、最多16批", prompt)
        self.assertIn("仅当本任务验收明确要求更多 M2/M3 测试时", prompt)
        self.assertIn("退出码为 0 不能替代测试通过", prompt)

    def test_mismatched_resume_or_trailing_result_cannot_be_reused(self):
        self.receipt(events(session=OTHER), requested_session=SESSION)
        self.assertIsNone(self.provider.recover("code", self.out))
        self.assertIsNone(read_session(self.out))
        rows = events()
        self.receipt([*rows, rows[-1]], requested_session=SESSION)
        self.assertIsNone(self.provider.recover("code", self.out))

    def test_same_session_task_notifications_preserve_the_foreground_result(self):
        rows = events()
        notification = [
            {"type": "system", "subtype": "task_updated", "session_id": SESSION},
            {"type": "system", "subtype": "task_notification", "session_id": SESSION},
            {"type": "system", "subtype": "background_tasks_changed", "session_id": SESSION},
            rows[0],
            {"type": "assistant", "session_id": SESSION, "error": "overloaded"},
            {**rows[-1], "is_error": True, "result": "Background task result.",
             "origin": {"kind": "task-notification", "producer": "session-task", "runId": "background-1"}},
        ]
        second = {**notification[-1], "origin": {**notification[-1]["origin"], "runId": "background-2"}}
        self.receipt([*rows, *notification, *notification[:-1], second], requested_session=SESSION)
        result = self.provider.recover("code", self.out)
        self.assertEqual(result, {"session_id": SESSION, "text": rows[-1]["result"], "completed": True})
        self.assertIsNone(classify_transient_failure("code", self.out))

        failed = events(is_error=True, extra=[{"type": "assistant", "session_id": SESSION, "error": "overloaded"}])
        self.receipt([*failed, *notification[:-1], {**second, "is_error": False}], requested_session=SESSION)
        self.assertIsNone(self.provider.recover("code", self.out))
        self.assertEqual(classify_transient_failure("code", self.out), {"reason": "server_unavailable"})

        for suffix in (
            notification[:-1],
            [notification[-1]],
            [{"type": "unexpected", "session_id": SESSION}],
            [*notification[:-1], {"type": "unexpected", "session_id": SESSION}, notification[-1]],
            [*notification[:-1], {**notification[-1], "session_id": OTHER}],
            [*notification[:-1], {**notification[-1], "origin": {"kind": "task-notification"}}],
            [*notification[:-1], rows[-1]],
            [*notification, *notification],
        ):
            with self.subTest(suffix=suffix):
                self.receipt([*rows, *suffix], requested_session=SESSION)
                self.assertIsNone(self.provider.recover("code", self.out))
        self.receipt([rows[0], notification[-1]], requested_session=SESSION)
        self.assertIsNone(self.provider.recover("code", self.out))

    def test_tool_content_is_never_saved_and_metadata_still_supports_recovery(self):
        rate = {"type": "rate_limit_event", "session_id": SESSION, "rate_limit_info": {"status": "allowed"}}
        small = {"type": "user", "session_id": SESSION, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-read", "content": "small-source"},
        ]}, "tool_use_result": {"file": {"filePath": "src/Small.cpp", "content": "small-source", "numLines": 1}}}
        tool_calls = {"type": "assistant", "session_id": SESSION, "message": {"content": [
            {"type": "tool_use", "id": "tool-edit", "name": "Edit",
             "input": {"old_string": "old-source", "new_string": "new-source"}},
            {"type": "tool_use", "id": "tool-write", "name": "Write",
             "input": {"content": "write-source", "file_path": "src/Small.cpp"}},
            {"type": "tool_use", "id": "tool-bash", "name": "Bash", "input": {"command": "command-source"}},
            {"type": "text", "text": "assistant-source"},
        ]}}
        payload = {"type": "user", "session_id": SESSION, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tool-edit", "content": "edit-source test-credential", "is_error": False},
            {"type": "tool_result", "tool_use_id": "tool-image", "content": [
                {"type": "image", "source": {"type": "base64", "data": "X" * 80_000}},
            ]},
        ]}, "tool_use_result": {"originalFile": "original-source" * 30_000, "filePath": "src/Worker.cpp",
                                "stdout": "stdout-source", "stderr": "stderr-source", "exitCode": 0}}
        rows = events(extra=[rate, small, tool_calls, payload])
        fixture = self.root / "fixture.jsonl"
        write_events(fixture, rows)
        self.provider.config["max_output_bytes"] = 1024
        self.settings["max_output_bytes"] = 8192
        environment = dict(safe_environment(), OPENAI_API_KEY="test-credential")
        emit = "from pathlib import Path; import sys; sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())"
        self.provider._invoke("code", [sys.executable, "-c", emit, str(fixture)], "Continue", self.root,
                              self.out, 10, environment, requested_session=SESSION, coding_provider="claude")

        output = (self.out / "stdout.log").read_text(encoding="utf-8")
        self.assertLess(len(output.encode("utf-8")), self.settings["max_output_bytes"])
        for omitted in ("test-credential", "small-source", "old-source", "new-source", "write-source", "command-source",
                        "assistant-source", "edit-source", "originalFile", "original-source", "stdout-source",
                        "stderr-source", "base64", '"input"'):
            self.assertNotIn(omitted, output)
        recorded = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(recorded[:2], rows[:2])
        self.assertEqual(recorded[-1], rows[-1])
        self.assertEqual(recorded[2]["tool_use_result"], {"file": {"filePath": "src/Small.cpp", "numLines": 1}})
        self.assertEqual(recorded[3]["message"]["content"], [
            {"type": "tool_use", "id": "tool-edit", "name": "Edit"},
            {"type": "tool_use", "id": "tool-write", "name": "Write"},
            {"type": "tool_use", "id": "tool-bash", "name": "Bash"},
        ])
        self.assertEqual(recorded[4]["message"]["content"], [
            {"type": "tool_result", "tool_use_id": "tool-edit", "is_error": False},
            {"type": "tool_result", "tool_use_id": "tool-image"},
        ])
        self.assertEqual(recorded[4]["tool_use_result"], {"filePath": "src/Worker.cpp", "exitCode": 0})
        self.assertTrue(self.provider.recover("code", self.out)["completed"])
        self.assertEqual(json.loads((self.out / "process.json").read_text())["max_output_bytes"], 8192)

    def test_partial_claude_record_cannot_save_source_or_look_like_a_valid_turn(self):
        partial = b'{"type":"assistant","message":{"content":[{"input":{"content":"partial-source'
        self.assertEqual(_claude_log_record(partial), b"{}\n")

    @patch("unattended.providers.time.time", return_value=1700000000)
    def test_subscription_rejection_preserves_weekly_reset_on_both_exit_codes(self, clock):
        rate = {"type": "rate_limit_event", "session_id": SESSION, "rate_limit_info": {
            "status": "rejected", "resetsAt": 1700250000, "errorCode": "credits_required",
        }}
        for code in (0, 1):
            with self.subTest(code=code):
                self.receipt(events(is_error=True, extra=[rate]), exit_code=code)
                self.assertEqual(classify_transient_failure("code", self.out), {
                    "reason": "claude_subscription_limit", "retry_after_seconds": 250000,
                })
                self.assertIsNone(self.provider.recover("code", self.out))
        self.receipt(events(extra=[rate]))
        self.assertIsNone(classify_transient_failure("code", self.out))
        self.assertTrue(self.provider.recover("code", self.out)["completed"])

    def test_auth_failure_warning_and_model_text_cannot_trigger_subscription_wait(self):
        rate = {"type": "rate_limit_event", "session_id": SESSION,
                "rate_limit_info": {"status": "allowed_warning", "resetsAt": 1700250000}}
        rows = events(is_error=True, extra=[rate])
        rows[-1]["result"] = "You've hit your limit · resets 5am (Asia/Shanghai)"
        self.receipt(rows)
        self.assertIsNone(classify_transient_failure("code", self.out))
        rate["rate_limit_info"]["status"] = "rejected"
        rows.insert(-1, {"type": "assistant", "session_id": SESSION, "error": "authentication_failed"})
        self.receipt(rows)
        self.assertIsNone(classify_transient_failure("code", self.out))

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_live_quota_failure_exits_into_the_persisted_wait_path(self, launch, auth):
        rate = {"type": "rate_limit_event", "session_id": SESSION,
                "rate_limit_info": {"status": "rejected"}}
        launch.side_effect = lambda *args, **kwargs: self.capture(events(is_error=True, extra=[rate]))
        with self.assertRaises(ProviderError) as failure:
            self.provider.code("Continue", self.root, self.out, 120)
        self.assertTrue(failure.exception.transient)
        self.assertEqual(failure.exception.retry_reason, "claude_subscription_limit")
        self.assertEqual(failure.exception.session_id, SESSION)
        self.assertNotIn("--resume", launch.call_args.args[0])
        self.assertEqual(classify_transient_failure("code", self.out), {"reason": "claude_subscription_limit"})
