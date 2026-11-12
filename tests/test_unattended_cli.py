from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from unattended import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "config with spaces.json"
        self.config = types.ModuleType("unattended.config")
        self.config.init_config = Mock(return_value={"status": "initialized"})
        self.config.load_config = Mock(return_value={"project_path": "fake"})
        self.config.doctor = Mock(return_value={"ready": True})
        self.engine = Mock()
        self.engine.run.return_value = {"status": "waiting"}
        self.engine.status.return_value = {"status": "ready", "task": "任务一"}
        self.engine.pause.return_value = {"status": "paused"}
        self.engine.resume.return_value = {"status": "ready"}
        self.engine.retry.return_value = {"status": "ready"}
        self.engine.defer.return_value = {"status": "plan", "accepted_count": 8}
        self.engine_module = types.ModuleType("unattended.engine")
        self.engine_module.Engine = Mock(return_value=self.engine)
        self.burst = types.ModuleType("unattended.burst")
        self.burst.run_burst = Mock(return_value={"status": "completed", "accepted_count": 3})
        modules = patch.dict(sys.modules, {"unattended.config": self.config,
                                          "unattended.engine": self.engine_module,
                                          "unattended.burst": self.burst})
        modules.start()
        self.addCleanup(modules.stop)

    def execute(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_config_accepted_before_and_after_subcommand(self):
        for args in (["--config", str(self.path), "status"], ["status", "--config", str(self.path)]):
            code, output, error = self.execute(args)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["task"], "任务一")
            self.config.load_config.assert_called_with(self.path)
        self.engine.run.assert_not_called()

    def test_manual_rework_passes_explicit_flags_to_engine(self):
        code, _, error = self.execute(["retry", "--rework", "--new-session", "--config", str(self.path)])
        self.assertEqual(code, 0, error)
        self.engine.retry.assert_called_once_with(rework=True, new_session=True)

    def test_external_validation_retry_is_explicit_and_exclusive(self):
        code, _, error = self.execute(["retry", "--external-validation", "--config", str(self.path)])
        self.assertEqual(code, 0, error)
        self.engine.retry.assert_called_once_with(external_validation=True)
        with self.assertRaises(SystemExit):
            self.execute(["retry", "--external-validation", "--rework"])
        with self.assertRaises(SystemExit):
            self.execute(["retry", "--external-validation", "--new-session"])

    def test_replanning_is_distinct_from_blind_rework_and_keeps_code_session(self):
        code, _, error = self.execute(["retry", "--replan", "--config", str(self.path)])
        self.assertEqual(code, 0, error)
        self.engine.retry.assert_called_once_with(replan=True)
        for flags in (("--replan", "--rework"), ("--replan", "--external-validation"),
                      ("--replan", "--new-session")):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.execute(["retry", *flags])

    def test_defer_requires_reason_and_dispatches_without_running_models(self):
        reason = "用户暂缓HUD闪动，待手动确认且不阻塞其他开发"
        code, output, error = self.execute(["defer", "--reason", reason, "--config", str(self.path)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["accepted_count"], 8)
        self.engine.defer.assert_called_once_with(reason)
        self.engine.run.assert_not_called()
        with self.assertRaises(SystemExit):
            self.execute(["defer"])

    def test_init_loads_utf8_goal_file_without_starting_engine(self):
        goal = self.root / "goal.txt"
        goal.write_text("实现任务并通过验收", encoding="utf-8-sig")
        code, _, error = self.execute(["init", "--config", str(self.path), "--project", str(self.root), "--goal-file", str(goal)])
        self.assertEqual(code, 0, error)
        self.config.init_config.assert_called_once_with(self.path, self.root, "实现任务并通过验收")
        self.engine_module.Engine.assert_not_called()

    def test_run_once_is_single_step_and_no_extra_execute_gate(self):
        code, _, _ = self.execute(["run", "--once", "--config", str(self.path)])
        self.assertEqual(code, 0)
        self.engine.run.assert_called_once_with(once=True)

    def test_run_without_tasks_keeps_the_continuous_engine_entry(self):
        self.assertEqual(self.execute(["run", "--config", str(self.path)])[0], 0)
        self.engine.run.assert_called_once_with(once=False)
        self.burst.run_burst.assert_not_called()

    def test_finite_tasks_dispatch_to_burst_with_explicit_window_override(self):
        code, output, error = self.execute(["run", "--tasks", "3", "--ignore-window", "--config", str(self.path)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["accepted_count"], 3)
        self.burst.run_burst.assert_called_once_with(self.config.load_config.return_value, tasks=3, ignore_window=True)
        self.engine_module.Engine.assert_not_called()
        self.engine.run.assert_not_called()

    def test_finite_tasks_obey_the_window_without_override(self):
        self.assertEqual(self.execute(["--config", str(self.path), "run", "--tasks", "2"])[0], 0)
        self.burst.run_burst.assert_called_once_with(self.config.load_config.return_value, tasks=2, ignore_window=False)
        self.engine_module.Engine.assert_not_called()

    def test_invalid_burst_arguments_fail_before_loading_or_running(self):
        invalid = (["run", "--ignore-window"], ["run", "--once", "--ignore-window"],
                   ["run", "--tasks", "0"], ["run", "--tasks", "-1"],
                   ["run", "--tasks", "2.5"], ["run", "--tasks", "many"],
                   ["run", "--once", "--tasks", "3"])
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(SystemExit) as raised:
                self.execute(args)
            self.assertEqual(raised.exception.code, 2)
        self.config.load_config.assert_not_called()
        self.burst.run_burst.assert_not_called()
        self.engine_module.Engine.assert_not_called()

    def test_interrupted_burst_returns_130_and_preserves_its_json(self):
        self.burst.run_burst.return_value = {"status": "interrupted", "accepted_count": 1}
        code, output, _ = self.execute(["run", "--tasks", "3"])
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(output)["accepted_count"], 1)

    def test_failed_or_blocked_burst_returns_nonzero(self):
        for status in ("blocked", "failed"):
            with self.subTest(status=status):
                self.burst.run_burst.return_value = {"status": status}
                self.assertEqual(self.execute(["run", "--tasks", "3"])[0], 1)

    def test_pause_resume_retry_are_distinct_preserving_actions(self):
        for action in ("pause", "resume", "retry"):
            self.assertEqual(self.execute([action, "--config", str(self.path)])[0], 0)
            getattr(self.engine, action).assert_called_once_with()
        self.engine.run.assert_not_called()

    def test_configuration_error_reports_nonzero_json_without_traceback(self):
        self.config.load_config.side_effect = ValueError("项目目录不存在")
        code, output, error = self.execute(["status", "--config", str(self.path)])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(error)["error"], "项目目录不存在")

    def test_unready_doctor_and_blocked_run_return_nonzero(self):
        self.config.doctor.return_value = {"ready": False}
        self.assertEqual(self.execute(["doctor"])[0], 1)
        self.engine.run.return_value = {"status": "BLOCKED"}
        self.assertEqual(self.execute(["run", "--once"])[0], 1)
        self.engine.status.return_value = {"status": "blocked"}
        self.assertEqual(self.execute(["status"])[0], 0)

    def test_background_config_can_precede_or_follow_action(self):
        manager = Mock()
        manager.status.return_value = {"status": "stopped"}
        with patch("unattended.background.BackgroundManager", return_value=manager) as constructor:
            for args in (["--config", str(self.path), "background", "status"],
                         ["background", "--config", str(self.path), "status"],
                         ["background", "status", "--config", str(self.path)]):
                self.assertEqual(self.execute(args)[0], 0)
                constructor.assert_called_with(self.path)

    def test_demo_does_not_load_project_or_model_configuration(self):
        demo = types.ModuleType("unattended.demo")
        demo.run_demo = Mock(return_value={"status": "passed"})
        with patch.dict(sys.modules, {"unattended.demo": demo}):
            self.assertEqual(self.execute(["demo", "--output", str(self.root / "demo")])[0], 0)
        demo.run_demo.assert_called_once_with(self.root / "demo")
        self.config.load_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
