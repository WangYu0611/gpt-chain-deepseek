"""Scheduling and failure gates must hold before any agent is started."""
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from unattended.calendar import BEIJING
from unattended.engine import Engine
from unattended.providers import ProviderError
from unattended.storage import read_json, write_json


class EngineGateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.calendar = self.root / "calendar.json"
        write_json(self.calendar, {"years": [2026], "holidays": ["2026-10-01"], "workdays": [], "overrides": {}})
        self.config = {
            "version": 1, "config_path": str(self.root / "config.json"), "project_path": str(self.root / "project"),
            "runtime_dir": str(self.root / "runtime"), "goal": "A concrete feature", "branch": "unattended/test",
            "base_ref": "HEAD", "task_prefix": "TASK-TEST", "schedule": {"holidays_file": str(self.calendar)},
            "checks": [{"id": "behavior", "argv": ["python.exe", "verify.py"], "timeout_seconds": 30}],
            "close_policy": "strict", "step_timeout_seconds": 3600, "poll_seconds": 1,
            "max_failures": 3, "max_reworks": 3, "push": False, "remote": "origin", "codex": {}, "dsh": {},
        }
        self.providers = Mock()
        self.providers.plan.return_value = {"action": "complete", "reason": "Nothing remains"}
        self.now = datetime(2026, 11, 2, 19, tzinfo=BEIJING)
        self.engine = Engine(self.config, self.providers, now=lambda: self.now)
        self.addCleanup(patch.stopall)
        patch.object(Engine, "_locks", side_effect=lambda: nullcontext()).start()
        self.workspace = patch("unattended.engine.gitops.ensure_workspace", return_value=self.root / "runtime/workspace").start()
        patch("unattended.engine.gitops.head", return_value="a" * 40).start()
        patch("unattended.engine.gitops.clean", return_value=True).start()

    def test_missing_checks_stops_before_clone_or_model(self):
        self.config["checks"] = []
        self.assertEqual(self.engine.run()["status"], "needs_setup")
        self.workspace.assert_not_called()
        self.providers.plan.assert_not_called()

    def test_weekday_daytime_never_starts_a_step(self):
        self.now = datetime(2026, 11, 2, 12, tzinfo=BEIJING)
        result = self.engine.step()
        self.assertEqual(result["status"], "waiting_window")
        self.assertEqual(result["schedule"]["next_open"], "2026-11-02T18:00:00+08:00")
        self.workspace.assert_not_called()
        self.providers.plan.assert_not_called()

    def test_weekend_daytime_is_allowed(self):
        self.now = datetime(2026, 11, 7, 12, tzinfo=BEIJING)
        self.assertEqual(self.engine.step()["status"], "done")
        self.providers.plan.assert_called_once()

    def test_official_holiday_daytime_is_allowed(self):
        self.now = datetime(2026, 10, 1, 12, tzinfo=BEIJING)
        self.assertEqual(self.engine.step()["status"], "done")

    def test_missing_year_waits_for_calendar_update(self):
        self.now = datetime(2027, 1, 1, 19, tzinfo=BEIJING)
        self.assertEqual(self.engine.step()["status"], "calendar_update_required")
        self.workspace.assert_not_called()
        self.providers.plan.assert_not_called()
        write_json(self.calendar, {"years": [2026, 2027], "holidays": [], "workdays": [], "overrides": {}})
        self.assertEqual(self.engine.step()["status"], "done")

    def test_planned_core_tests_are_written_to_task_book_and_checkpoint(self):
        self.config["coding_provider"] = "claude"
        self.config["claude"] = {"planned_core_tests": True}
        names = ["ZhuGuoCity.M2.Transport", "ZhuGuoCity.M2.TransportSave"]
        self.providers.plan.return_value = {
            "action": "task", "title": "原料运输", "body": "仓库到工坊", "reason": "",
            "acceptance_criteria": ["到货后开工，存档可续作"], "core_tests": names,
        }
        self.assertEqual(self.engine.step()["phase"], "CODE")
        task = read_json(self.engine.state_path)["task"]
        self.assertEqual(task["core_tests"], names)
        directory = self.engine.runtime / "tasks" / task["id"]
        self.assertEqual(read_json(directory / "task.json")["core_tests"], names)
        self.assertIn("\n".join("- " + name for name in names),
                      (directory / "TASK.md").read_text(encoding="utf-8"))

    def test_headless_codex_leaves_ue_compiler_probes_and_core_tests_to_check(self):
        self.config.update(coding_provider="codex", headless=True)
        self.config["codex"]["planned_core_tests"] = True
        self.providers.plan.return_value = {
            "action": "task", "title": "原料运输", "body": "仓库到工坊", "reason": "",
            "acceptance_criteria": ["到货后开工"], "core_tests": ["ZhuGuoCity.M2.Transport"],
        }
        self.assertEqual(self.engine.step()["phase"], "CODE")
        planning_prompt = self.providers.plan.call_args.args[0]
        self.assertIn("全部 UE 测试由提交后的独立 CHECK 构建并执行", planning_prompt)
        self.assertNotIn("提交前预跑", planning_prompt)
        state = read_json(self.engine.state_path)
        task_book = self.engine.runtime / "tasks" / state["task"]["id"] / "TASK.md"
        self.assertIn("提交后独立 CHECK 核心测试（NullRHI）", task_book.read_text(encoding="utf-8"))

        state["task"]["code_resume"] = True
        self.providers.code.return_value = {"completed": True, "text": "待 CHECK"}
        with patch("unattended.engine.gitops.head", side_effect=["a" * 40, "a" * 40, "b" * 40]), \
                patch("unattended.engine.gitops.verify_candidate", return_value={"head_sha": "b" * 40}):
            self.engine._code(state, self.root / "runtime/workspace")
        coding_prompt = self.providers.code.call_args.args[0]
        self.assertIn("禁止调用 MSVC cl.exe（含 /Zs 语法检查、/P 或 /EP 预处理", coding_prompt)
        self.assertIn("重放或改写 .rsp 响应文件", coding_prompt)
        self.assertIn("恢复会话中的验证要求", coding_prompt)
        self.assertIn("validation-request.json", coding_prompt)
        self.assertEqual(state["phase"], "CHECK")

    def test_pause_survives_a_new_controller_and_preserves_sequence(self):
        self.engine.pause()
        fresh = Engine(self.config, self.providers, now=lambda: self.now)
        self.assertEqual(fresh.step()["status"], "paused")
        self.providers.plan.assert_not_called()
        fresh.resume()
        self.assertEqual(fresh.step()["status"], "done")
        self.assertEqual(read_json(fresh.state_path)["sequence"], 1)

    def test_repeated_provider_failures_block_and_retry_keeps_task_sequence(self):
        self.providers.plan.side_effect = RuntimeError("Provider unavailable")
        self.providers.recover.return_value = None
        for _ in range(3):
            result = self.engine.step()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.providers.plan.call_count, 3)
        self.engine.step()
        self.assertEqual(self.providers.plan.call_count, 3)
        self.assertEqual(self.engine.retry()["phase"], "PLAN")

    def failed_api_review(self):
        self.config["codex"].update(review_transport="responses_api", model="gpt-6-astra")
        state = self.engine._load()
        state.update(phase="REVIEW", expected_head="a" * 40,
                     task={"id": "TASK-TEST-0001", "candidate": {"head_sha": "a" * 40},
                           "reworks": 0, "check_results": [{"passed": True}]})
        operation = self.engine._new_operation(state, "review")
        source = Path(operation["output_dir"])
        write_json(source / "process.json", {
            "protocol": "openai_responses_v1", "kind": "review", "status": "failed", "exit_code": 1,
            "transient": True, "retry_reason": "transport", "timed_out": False, "output_limit_exceeded": False,
            "head_sha": "a" * 40, "workspace": str((self.engine.runtime / "workspace").resolve()),
            "turns": [{"number": 1, "tools_file": "responses/turn-0001.tools.json"}],
        })
        self.providers.recover.return_value = None
        result = {"decision": "accepted", "summary": "Reviewed fixed candidate", "feedback": "", "head_sha": "a" * 40}
        self.providers.review.return_value = result
        self.providers.resume_review.return_value = result
        error = ProviderError("Temporary transport failure", transient=True, retry_reason="transport")
        self.engine._schedule_provider_retry(state, "REVIEW", error, self.engine.runtime / "workspace")
        return source

    def test_api_review_retry_waits_then_persists_continuation_before_launch_without_replaying_code(self):
        source = self.failed_api_review()
        before = (source / "process.json").read_bytes()
        self.assertEqual(self.engine.step()["status"], "waiting_provider")
        self.providers.resume_review.assert_not_called()
        self.now += timedelta(seconds=181)
        result = self.providers.resume_review.return_value
        def resume(prompt, work, out, timeout, *, previous_out):
            saved = read_json(self.engine.state_path)["operation"]
            self.assertEqual(saved["resume_from"], str(source))
            self.assertEqual(saved["output_dir"], str(out))
            self.assertEqual(saved["task_id"], "TASK-TEST-0001")
            self.assertEqual(saved["head_sha"], "a" * 40)
            self.assertNotEqual(out, source)
            self.assertEqual(previous_out, source)
            return result
        self.providers.resume_review.side_effect = resume
        self.assertEqual(self.engine.step()["phase"], "PUBLISH")
        self.assertEqual((source / "process.json").read_bytes(), before)
        self.providers.review.assert_not_called()
        self.providers.code.assert_not_called()
        self.providers.plan.assert_not_called()

    def test_api_review_continuation_source_survives_a_crash_before_the_next_request(self):
        source = self.failed_api_review()
        self.now += timedelta(seconds=181)
        self.providers.resume_review.side_effect = SystemExit("Simulated supervisor crash")
        with self.assertRaises(SystemExit):
            self.engine.step()
        interrupted = read_json(self.engine.state_path)["operation"]
        self.assertEqual(interrupted["resume_from"], str(source))
        self.providers.resume_review.side_effect = None
        fresh = Engine(self.config, self.providers, now=lambda: self.now)
        self.assertEqual(fresh.step()["phase"], "PUBLISH")
        self.assertEqual(self.providers.resume_review.call_args.kwargs["previous_out"], source)
        self.assertNotEqual(str(self.providers.resume_review.call_args.args[2]), interrupted["output_dir"])
        self.providers.review.assert_not_called()

    def test_api_review_continuation_rejects_an_external_source_without_calling_the_provider(self):
        self.failed_api_review()
        state = read_json(self.engine.state_path)
        state.pop("provider_retry")
        self.engine._new_operation(state, "review", resume_from=self.root / "outside-review")
        result = self.engine.step()
        self.assertEqual(result["failures"], 1)
        self.assertIn("managed runs", result["last_error"])
        self.providers.resume_review.assert_not_called()
        self.providers.review.assert_not_called()

    def test_api_review_with_a_different_source_head_starts_a_fresh_review(self):
        source = self.failed_api_review()
        receipt = read_json(source / "process.json")
        receipt["head_sha"] = "b" * 40
        write_json(source / "process.json", receipt)
        self.now += timedelta(seconds=181)
        self.assertEqual(self.engine.step()["phase"], "PUBLISH")
        self.providers.resume_review.assert_not_called()
        self.providers.review.assert_called_once()

    def test_api_review_provider_without_continuation_capability_keeps_fresh_review_compatibility(self):
        self.failed_api_review()
        self.now += timedelta(seconds=181)
        provider = SimpleNamespace(recover=Mock(return_value=None), review=Mock(return_value=self.providers.review.return_value))
        engine = Engine(self.config, provider, now=lambda: self.now)
        self.assertEqual(engine.step()["phase"], "PUBLISH")
        provider.review.assert_called_once()
        self.assertEqual(read_json(self.engine.state_path)["sequence"], 1)

    def test_state_cannot_be_reused_for_a_changed_goal(self):
        self.engine.step()
        self.config["goal"] = "A different project objective"
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.engine.status()

    def test_strict_timeout_is_limited_by_the_remaining_window(self):
        self.now = datetime(2026, 11, 3, 7, 59, 50, tzinfo=BEIJING)
        self.assertEqual(self.engine._timeout(3600), (10.0, True))

    def _check_output(self, script, **settings):
        work = self.root / "runtime/workspace"
        work.mkdir(parents=True)
        self.config["checks"] = [{"id": "behavior", "argv": [sys.executable, "-c", script, "{workspace}"],
                                  "timeout_seconds": 10, **settings}]
        state = self.engine._load()
        state["phase"] = "CHECK"
        state["expected_head"] = "a" * 40
        state["task"] = {"id": "TASK-TEST-0001", "candidate": {"head_sha": "a" * 40}, "check_results": []}
        self.engine._save(state)
        self.engine.step()
        return read_json(self.engine.state_path)["task"]["check_results"][0]

    def test_check_expands_workspace_and_requires_success_evidence(self):
        result = self._check_output("from pathlib import Path; import sys; assert Path(sys.argv[1]) == Path.cwd(); print('PASS behavior')",
                                    success_patterns=["PASS behavior"])
        self.assertTrue(result["passed"])
        self.assertNotIn("{workspace}", result["argv"][-1])

    def test_zero_exit_without_requested_test_receipt_fails(self):
        result = self._check_output("print('No matching tests')", success_patterns=["Test Completed. Result={Success}"])
        self.assertFalse(result["passed"])
        self.assertTrue(result["patterns"]["missing_success"])

    def test_failure_receipt_overrides_zero_exit_and_success_text(self):
        result = self._check_output("print('PASS behavior'); print('Result={Fail}')",
                                    success_patterns=["PASS behavior"], failure_patterns=["Result={Fail}"])
        self.assertFalse(result["passed"])

    def test_failed_editor_build_skips_tests_and_preserves_compiler_evidence(self):
        work = self.root / "runtime/workspace"
        work.mkdir(parents=True)
        test_marker = self.root / "stale-test-ran"
        self.config["checks"] = [
            {"id": "ue-editor-build", "timeout_seconds": 10,
             "argv": [sys.executable, "-c", "import sys; print('error C2535', file=sys.stderr); sys.exit(6)"]},
            {"id": "ue-CatalogGroups", "timeout_seconds": 10,
             "argv": [sys.executable, "-c", "import sys; from pathlib import Path; Path(sys.argv[1]).touch()", str(test_marker)]},
        ]
        state = self.engine._load()
        head = "a" * 40
        state["phase"], state["expected_head"] = "CHECK", head
        state["task"] = {"id": "TASK-TEST-0001", "candidate": {"head_sha": head}, "check_results": []}
        self.engine._save(state)

        self.assertEqual(self.engine.step()["phase"], "REVIEW")
        state = read_json(self.engine.state_path)
        build, skipped = state["task"]["check_results"]
        self.assertEqual(build["exit_code"], 6)
        self.assertIn("error C2535", Path(build["stderr_path"]).read_text(encoding="utf-8"))
        self.assertEqual((skipped["id"], skipped["head_sha"], skipped["status"], skipped["reason"]),
                         ("ue-CatalogGroups", head, "NOT_RUN", "editor_build_failed"))
        self.assertFalse(skipped["passed"])
        self.assertFalse(test_marker.exists())

        # A checkpoint written by an older runner can already contain the failure.
        state["phase"] = "CHECK"
        state["task"]["check_results"] = [build]
        self.engine._save(state)
        fresh = Engine(self.config, self.providers, now=lambda: self.now)
        self.assertEqual(fresh.step()["phase"], "REVIEW")
        self.assertEqual(read_json(fresh.state_path)["task"]["check_results"], [build, skipped])
        self.assertFalse(test_marker.exists())


if __name__ == "__main__":
    unittest.main()
