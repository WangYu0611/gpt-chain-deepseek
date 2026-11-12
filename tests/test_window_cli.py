from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

import desktop_relay as cli
from windowrelay.windows import WindowInfo
from windowrelay.observer import Observation


class WindowCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "config.json"
        # Direct messages need no repository, commit prefixes or polling interval.
        self.config = {
            "runtime_dir": "runtime", "gpt": {"url": "https://chatgpt.com/c/example"},
            "dsh": {"project": "ue_game"},
        }
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        for name in ("run", "Popen"):
            forbidden = patch.object(cli.subprocess, name, side_effect=AssertionError("Direct messages must not start Git or subprocesses"))
            forbidden.start()
            self.addCleanup(forbidden.stop)
        forbidden_native = patch.object(cli, "native", side_effect=AssertionError("Tests must not use the real desktop"))
        forbidden_native.start()
        self.addCleanup(forbidden_native.stop)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = cli.main(["--config", str(self.path), *args])
        return result, output.getvalue()

    def test_config_does_not_need_repository_or_polling(self):
        config, runtime = cli.load_config(self.path)
        self.assertEqual(config, self.config)
        self.assertEqual(runtime, self.folder / "runtime")
        self.assertFalse(runtime.exists())

    def test_default_gpt_and_dsh_commands_only_preview_without_sender_or_state(self):
        for target in ("gpt", "dsh"):
            with self.subTest(target=target), patch.object(cli, "sender_for") as sender:
                result, output = self.invoke(target)
            self.assertEqual(result, 0)
            value = json.loads(output)
            self.assertEqual(value["status"], "dry_run")
            self.assertEqual(value["target"], target)
            self.assertEqual(value["message"], cli.DEFAULT_MESSAGES[target])
            sender.assert_not_called()
            self.assertFalse((self.folder / "runtime").exists())

    def test_gpt_send_only_validates_and_sends_to_gpt(self):
        sender = Mock()
        sender.validate.return_value = Mock()
        with patch.object(cli, "sender_for", return_value=sender):
            result, output = self.invoke("gpt", "--send")
        self.assertEqual(result, 0)
        self.assertEqual(sender.method_calls, [call.validate("gpt"), call.send_gpt(cli.DEFAULT_MESSAGES["gpt"], ANY)])
        sender.send_dsh.assert_not_called()
        value = json.loads(output)
        self.assertEqual(value["status"], "send_action_completed")
        self.assertIsNone(value["server_received"])

    def test_dsh_send_uses_only_dsh_and_custom_message(self):
        sender = Mock()
        with patch.object(cli, "sender_for", return_value=sender):
            result, output = self.invoke("dsh", "--send", "--message", "拉取 main，完成 TASK-2。")
        self.assertEqual(result, 0)
        self.assertEqual(sender.method_calls, [call.validate("dsh"), call.send_dsh("拉取 main，完成 TASK-2。", ANY)])
        sender.send_gpt.assert_not_called()
        self.assertEqual(json.loads(output)["message"], "拉取 main，完成 TASK-2。")

    def test_missing_calibration_fails_before_pending_is_written(self):
        sender = Mock()
        sender.validate.side_effect = RuntimeError("请先校准 GPT")
        with patch.object(cli, "sender_for", return_value=sender):
            result, output = self.invoke("gpt", "--send")
        self.assertEqual(result, 1)
        self.assertIn("请先校准 GPT", output)
        sender.validate.assert_called_once_with("gpt")
        sender.send_gpt.assert_not_called()
        sender.send_dsh.assert_not_called()
        self.assertFalse((self.folder / "runtime" / "direct-state.json").exists())

    def test_preview_is_offline_and_does_not_write_state(self):
        with patch.object(cli, "sender_for") as sender:
            result, output = self.invoke("preview", "--target", "gpt", "--message", "请检查这次交付。")
        self.assertEqual(result, 0)
        self.assertEqual(output.strip(), "请检查这次交付。")
        self.assertFalse((self.folder / "runtime").exists())
        sender.assert_not_called()

    def test_menu_only_sends_the_directions_selected_by_user(self):
        sender = Mock()
        with patch.object(cli, "sender_for", return_value=sender), patch("builtins.input", side_effect=["g", "d", "q"]):
            result, output = self.invoke("manual", "--send")
        self.assertEqual(result, 0)
        self.assertEqual(sender.method_calls, [
            call.validate("gpt"), call.send_gpt(cli.DEFAULT_MESSAGES["gpt"], ANY),
            call.validate("dsh"), call.send_dsh(cli.DEFAULT_MESSAGES["dsh"], ANY),
        ])
        self.assertIn("不自动判断任务完成", output)

    def test_menu_preview_does_not_construct_sender_or_write_state(self):
        with patch.object(cli, "sender_for") as sender, patch("builtins.input", side_effect=["d", "q"]):
            result, output = self.invoke("manual")
        self.assertEqual(result, 0)
        self.assertIn('"target": "dsh"', output)
        self.assertNotIn('"target": "gpt"', output)
        sender.assert_not_called()
        self.assertFalse((self.folder / "runtime").exists())

    def test_automatic_run_does_not_ask_for_g_or_d(self):
        sender, observer, engine = Mock(), Mock(), Mock()
        engine.step.side_effect = [
            {"status": "awaiting", "target": "gpt"},
            {"status": "completed", "target": "gpt", "next_target": "dsh"},
            {"status": "awaiting", "target": "dsh"},
            {"status": "paused", "target": "dsh"},
        ]
        desktop = Mock()
        with patch.object(cli, "sender_for", return_value=sender), patch.object(cli, "observer_for", return_value=observer), patch.object(cli, "AutoRelay", return_value=engine), patch.object(cli, "native", return_value=(lambda: desktop, None, None, None)), patch.object(cli, "wait_for_observation", side_effect=[True, True, True, True, True, False]), patch("builtins.input", side_effect=AssertionError("Automatic loop must not ask for a direction")):
            result, output = self.invoke("run", "--send")
        self.assertEqual(result, 0)
        self.assertEqual(engine.step.call_count, 4)
        self.assertIn("自动循环", output)
        self.assertEqual(sender.method_calls, [call.validate("gpt"), call.validate("dsh")])

    def test_automatic_preview_has_no_sender_or_observer_and_writes_no_state(self):
        with patch.object(cli, "sender_for") as sender, patch.object(cli, "observer_for") as observer, patch("builtins.input", side_effect=AssertionError("No menu in automatic mode")):
            result, output = self.invoke("run", "--start-with", "dsh")
        self.assertEqual(result, 0)
        value = json.loads(output)
        self.assertEqual(value["status"], "dry_run")
        self.assertEqual(value["target"], "dsh")
        sender.assert_not_called()
        observer.assert_not_called()
        self.assertFalse((self.folder / "runtime").exists())

    def test_automatic_once_exits_after_one_step(self):
        sender, engine = Mock(), Mock()
        engine.step.return_value = {"status": "awaiting", "target": "gpt"}
        with patch.object(cli, "sender_for", return_value=sender), patch.object(cli, "observer_for"), patch.object(cli, "AutoRelay", return_value=engine), patch.object(cli, "native", return_value=(Mock(), None, None, None)), patch.object(cli, "wait_for_observation", return_value=True):
            result, _ = self.invoke("run", "--send", "--once")
        self.assertEqual(result, 0)
        engine.step.assert_called_once_with(execute=True, start_with="gpt")

    def test_locked_desktop_waits_without_reading_apps_or_sending(self):
        sender, engine, observer, desktop = Mock(), Mock(), Mock(), Mock()
        desktop.is_desktop_available.return_value = False
        with patch.object(cli, "sender_for", return_value=sender), patch.object(cli, "observer_for", return_value=observer), patch.object(cli, "AutoRelay", return_value=engine), patch.object(cli, "native", return_value=(lambda: desktop, None, None, None)), patch.object(cli, "wait_for_observation", return_value=True):
            result, output = self.invoke("run", "--send", "--once")
        self.assertEqual(result, 0)
        self.assertIn("waiting_desktop_unlock", output)
        engine.step.assert_not_called()
        sender.validate.assert_not_called()
        sender.send_dsh.assert_not_called()
        sender.send_gpt.assert_not_called()
        observer.snapshot.assert_not_called()

    def test_resume_preserves_automatic_turn_instead_of_resetting_it(self):
        runtime = self.folder / "runtime"
        runtime.mkdir()
        (runtime / "PAUSE").touch()
        engine = Mock()
        engine.status.return_value = {"state": {"phase": "paused", "pause_code": "pause_requested", "pending": None}}
        engine.resume.return_value = {"status": "resumed", "target": "gpt"}
        with patch.object(cli, "AutoRelay", return_value=engine):
            result, _ = self.invoke("resume")
        self.assertEqual(result, 0)
        engine.resume.assert_called_once_with()
        engine.reset.assert_not_called()
        self.assertFalse((runtime / "PAUSE").exists())

    def test_probe_only_observes_the_selected_target(self):
        observation = Observation(target="gpt", available=True, busy=False, idle=True, blocked=False,
                                  editable_text="", reason="ready", text="private response", buttons=("Send",), fingerprint="f")
        observer = Mock()
        observer.snapshot.return_value = observation
        with patch.object(cli, "observer_for", return_value=observer), patch.object(cli, "sender_for") as sender:
            result, output = self.invoke("probe", "--target", "gpt")
        self.assertEqual(result, 0)
        observer.snapshot.assert_called_once_with("gpt")
        sender.assert_not_called()
        self.assertNotIn("private response", output)
        self.assertTrue(json.loads(output)["available"])

    def test_probe_draft_text_is_only_printed_with_explicit_diagnostic_option(self):
        observation = Observation(target="gpt", available=True, busy=False, idle=False, blocked=False,
                                  editable_text="private unfinished draft", reason="draft", text="conversation", fingerprint="f",
                                  draft_present=True, editor_source="named", draft_preview="private unfinished draft",
                                  reason_code="draft_present")
        observer = Mock()
        observer.snapshot.return_value = observation
        with patch.object(cli, "observer_for", return_value=observer):
            result, output = self.invoke("probe", "--target", "gpt")
            detailed_result, detailed_output = self.invoke("probe", "--target", "gpt", "--diagnostic")
        self.assertEqual((result, detailed_result), (0, 0))
        self.assertNotIn("private unfinished draft", output)
        self.assertFalse(json.loads(output)["blocked"])
        self.assertTrue(json.loads(output)["draft_present"])
        self.assertEqual(json.loads(detailed_output)["draft_preview"], "private unfinished draft")

    def test_doctor_can_validate_one_target_without_touching_the_other(self):
        sender = Mock()
        with patch.object(cli, "sender_for", return_value=sender):
            result, output = self.invoke("doctor", "--target", "gpt")
        self.assertEqual(result, 0)
        self.assertEqual(sender.method_calls, [call.validate("gpt")])
        self.assertEqual([item["check"] for item in json.loads(output)["checks"]], ["gpt"])
        self.assertFalse((self.folder / "runtime").exists())

    def test_runtime_cannot_live_inside_project(self):
        self.config["dsh"]["project_path"] = str(self.folder / "project")
        self.config["runtime_dir"] = str(self.folder / "project" / "relay-data")
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "目录之外"):
            cli.load_config(self.path)

    def test_calibration_coordinate_read_does_not_require_foreground(self):
        info = WindowInfo(42, "DeepSeek Harness", "DeepSeek Harness.exe", (100, 100), 96)
        desktop = Mock()
        desktop.is_escape.return_value = False
        cursor = Mock(return_value=(50, 60))
        with patch.object(cli, "native", return_value=(None, cursor, None, lambda: [info])), patch("builtins.input", return_value=""), patch.object(cli.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            point = cli.capture_point(info, "input", desktop, cursor)
        self.assertEqual(point, [50, 60])
        desktop.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
