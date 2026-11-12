from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from windowrelay.observer import WindowObserver, collect_ui
from windowrelay.windows import WindowInfo


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(42, "目标对话", "chrome.exe", (1000, 700), 96)
        self.config = {
            target: {"window": {"process_name": "chrome.exe", "title_contains": "目标"},
                     "calibration": {"client_size": [1000, 700], "dpi": 96}}
            for target in ("gpt", "dsh")
        }
        self.backend = Mock()
        self.backend.find_window.return_value = self.window
        self.collector = Mock(return_value={
            "text": "本轮回复 [[RELAY_DONE:abc]]", "buttons": ["发送消息"],
            "enabled_buttons": ["发送消息"], "editable_text": "", "editable_present": True,
            "editable_readable": True, "truncated": False,
        })
        self.observer = WindowObserver(self.config, self.backend, self.collector)

    def test_idle_requires_a_positively_readable_empty_input(self):
        observed = self.observer.snapshot("gpt")
        self.assertTrue(observed.available)
        self.assertFalse(observed.busy)
        self.assertTrue(observed.idle)
        self.assertFalse(observed.blocked)
        self.assertEqual(observed.text, self.collector.return_value["text"])
        self.assertEqual(len(observed.fingerprint), 64)
        self.assertEqual([item[0] for item in self.backend.method_calls], ["find_window", "find_window"])
        self.collector.assert_called_once_with(42)

    def test_missing_stop_is_not_itself_idle(self):
        self.collector.return_value.update(buttons=[], enabled_buttons=[], editable_present=False)
        observed = self.observer.snapshot("gpt")
        self.assertFalse(observed.available)
        self.assertIsNone(observed.busy)
        self.assertFalse(observed.idle)

    def test_disabled_send_does_not_prove_idle(self):
        self.collector.return_value.update(enabled_buttons=[], editable_present=False)
        observed = self.observer.snapshot("gpt")
        self.assertFalse(observed.available)
        self.assertFalse(observed.idle)

    def test_enabled_send_without_identified_editor_is_unknown(self):
        self.collector.return_value.update(editable_present=False)
        observed = self.observer.snapshot("dsh")
        self.assertFalse(observed.available)
        self.assertIsNone(observed.busy)
        self.assertFalse(observed.idle)

    def test_stop_without_identified_editor_still_reports_busy(self):
        self.collector.return_value.update(editable_present=False, buttons=["停止生成"])
        self.collector.return_value.pop("editable_readable")
        observed = self.observer.snapshot("dsh")
        self.assertTrue(observed.available)
        self.assertTrue(observed.busy)
        self.assertFalse(observed.idle)

    def test_editor_without_explicit_readability_is_unknown(self):
        self.collector.return_value.pop("editable_readable")
        observed = self.observer.snapshot("dsh")
        self.assertFalse(observed.available)
        self.assertFalse(observed.idle)

    def test_visible_empty_editor_is_positive_idle_without_enabled_send(self):
        self.collector.return_value.update(buttons=[], enabled_buttons=[])
        observed = self.observer.snapshot("gpt")
        self.assertTrue(observed.available)
        self.assertTrue(observed.idle)

    def test_busy_controls_override_idle_controls_and_completion_text(self):
        for label in ("Stop generating", "停止生成", "停止", "2 个后台任务运行中", "排队发送", "插话发送"):
            with self.subTest(label=label):
                self.collector.return_value["buttons"] = [label, "发送消息"]
                observed = self.observer.snapshot("dsh")
                self.assertTrue(observed.available)
                self.assertTrue(observed.busy)
                self.assertFalse(observed.idle)

    def test_nonempty_draft_waits_without_claiming_approval(self):
        self.collector.return_value["editable_text"] = "还未发送的任务指令"
        observed = self.observer.snapshot("dsh")
        self.assertTrue(observed.available)
        self.assertFalse(observed.blocked)
        self.assertTrue(observed.draft_present)
        self.assertEqual(observed.reason_code, "draft_present")
        self.assertEqual(observed.blocked_controls, ())
        self.assertIn("检测到未发送文字，保留等待", observed.reason)
        self.assertFalse(observed.idle)

    def test_blocked_matches_current_buttons_not_historical_text(self):
        self.collector.return_value["text"] = "之前的记录：等待回答，批准一次。"
        observed = self.observer.snapshot("gpt")
        self.assertFalse(observed.blocked)
        self.collector.return_value["buttons"].append("批准一次")
        self.assertFalse(self.observer.snapshot("gpt").blocked)  # Disabled/historical controls do not require approval.
        self.collector.return_value["enabled_buttons"].append("批准一次")
        observed = self.observer.snapshot("gpt")
        self.assertTrue(observed.blocked)
        self.assertEqual(observed.blocked_controls, ("批准一次",))
        self.assertEqual(observed.reason_code, "approval_controls")
        self.assertFalse(observed.idle)

    def test_configurable_button_patterns(self):
        self.config["dsh"]["completion"] = {
            "busy_regex": "^任务执行中$", "idle_regex": ["^可发送$"], "blocked_regex": "^请批准$",
        }
        self.collector.return_value.update(buttons=["可发送"], enabled_buttons=["可发送"])
        self.assertTrue(self.observer.snapshot("dsh").idle)
        self.collector.return_value["buttons"] = ["任务执行中"]
        self.assertTrue(self.observer.snapshot("dsh").busy)
        self.collector.return_value["buttons"] = ["请批准"]
        self.collector.return_value["enabled_buttons"] = ["请批准"]
        self.assertTrue(self.observer.snapshot("dsh").blocked)

    def test_ambiguous_placeholder_preserves_text_and_diagnostics_without_idle(self):
        placeholder = "Message ChatGPT"
        self.collector.return_value.update(editable_text=placeholder, editable_readable=False,
                                           editor_ambiguous=True, editor_source="named_text_ambiguous",
                                           editor_candidates=[{"name": placeholder, "read_source": "text", "text_preview": placeholder}])
        observed = self.observer.snapshot("gpt")
        self.assertFalse(observed.available)
        self.assertFalse(observed.idle)
        self.assertFalse(observed.blocked)
        self.assertEqual(observed.editable_text, placeholder)
        self.assertEqual(observed.draft_preview, placeholder)
        self.assertEqual(observed.reason_code, "editor_ambiguous")
        self.assertEqual(observed.editor_source, "named_text_ambiguous")
        self.assertEqual(observed.editor_candidates[0]["text_preview"], placeholder)

    def test_diagnostics_are_limited_without_overwriting_original_draft(self):
        draft = "x" * 1000
        self.collector.return_value.update(editable_text=draft, editor_source="named",
                                           editor_candidates=[{"name": "n" * 200, "value_preview": draft, "bounding_rect": [1, 2, 3, 4]}] * 10)
        observed = self.observer.snapshot("gpt")
        self.assertEqual(observed.editable_text, draft)
        self.assertEqual(len(observed.draft_preview), 160)
        self.assertEqual(len(observed.editor_candidates), 8)
        self.assertEqual(len(observed.editor_candidates[0]["value_preview"]), 160)
        self.assertEqual(observed.editor_candidates[0]["bounding_rect"], [1, 2, 3, 4])

    def test_empty_partial_or_malformed_output_cannot_mean_complete(self):
        for change in ({"text": " "}, {"truncated": True}, {"truncated": None},
                       {"buttons": "Send"}, {"editable_text": None}, {"editable_readable": False},
                       {"error": "provider failed"}):
            with self.subTest(change=change):
                baseline = dict(self.collector.return_value)
                self.collector.return_value.update(change)
                observed = self.observer.snapshot("gpt")
                self.assertFalse(observed.available)
                self.assertFalse(observed.idle)
                self.collector.return_value = baseline

    def test_timeout_is_unknown_and_not_completed(self):
        self.collector.side_effect = subprocess.TimeoutExpired("powershell", 12)
        observed = self.observer.snapshot("gpt")
        self.assertFalse(observed.available)
        self.assertIsNone(observed.busy)
        self.assertFalse(observed.idle)

    def test_geometry_validation_precedes_collector_without_sender_validation(self):
        self.config["gpt"]["calibration"]["dpi"] = 120
        self.assertFalse(self.observer.snapshot("gpt").available)
        self.collector.assert_not_called()
        self.backend.validate.assert_not_called()
        self.backend.activate.assert_not_called()

    def test_identity_or_geometry_change_during_read_is_unknown(self):
        changed = WindowInfo(99, "目标对话", "chrome.exe", (1000, 700), 96)
        self.backend.find_window.side_effect = [self.window, changed]
        observed = self.observer.snapshot("gpt")
        self.assertFalse(observed.available)
        self.assertFalse(observed.idle)

    def test_no_input_calibration_or_dedicated_draft_flag_is_needed_to_observe(self):
        self.assertNotIn("input", self.config["gpt"]["calibration"])
        self.assertTrue(self.observer.snapshot("gpt").available)

    def test_invalid_regex_is_unknown_before_reading_application(self):
        self.config["gpt"]["completion"] = {"busy_regex": "["}
        self.assertFalse(self.observer.snapshot("gpt").available)
        self.collector.assert_not_called()

    def test_fingerprint_is_stable_and_changes_with_new_reply(self):
        first = self.observer.snapshot("gpt").fingerprint
        self.assertEqual(first, self.observer.snapshot("gpt").fingerprint)
        self.collector.return_value["text"] += " 新内容"
        self.assertNotEqual(first, self.observer.snapshot("gpt").fingerprint)


@unittest.skipUnless(os.name == "nt", "PowerShell collector is Windows-only")
class CollectorCommandTests(unittest.TestCase):
    def test_editor_classifier_and_preference_with_fake_controls_only(self):
        helper = Path(__file__).resolve().parents[1] / "windowrelay" / "read_ui.ps1"
        path_literal = str(helper).replace("'", "''")
        # Extract and exercise only two pure functions. Never execute the helper's
        # top level, load UIA providers, resolve a HWND, or read a real application.
        command = f"""
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{path_literal}',[ref]$tokens,[ref]$errors)
if ($errors.Count) {{ throw 'Helper syntax error' }}
$definitions=$ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in @('Get-EditorCandidate','Select-EditorState','Test-EditorPlaceholder') }},$true)
foreach ($definition in $definitions) {{ . ([scriptblock]::Create($definition.Extent.Text)) }}
$pattern=($ast.ParamBlock.Parameters | Where-Object {{ $_.Name.VariablePath.UserPath -eq 'EditorRegex' }}).DefaultValue.Value
$fakeNamed=New-Object 'System.Collections.Generic.List[string]'; $fakeNamed.Add('')
$fakeFallback=New-Object 'System.Collections.Generic.List[string]'; $fakeFallback.Add('只读工具输出不应成为草稿')
$fakeEmpty=New-Object 'System.Collections.Generic.List[string]'
$result=@{{
    inherited=Get-EditorCandidate 'ControlType.Custom' '发消息或创建任务, / 调用指令' $false $false $false $pattern
    staticLabel=Get-EditorCandidate 'ControlType.Text' '发消息或创建任务, / 调用指令' $true $false $false $pattern
    readonlyOutput=Get-EditorCandidate 'ControlType.Edit' '代码输出' $true $true $true $pattern
    omnibox=Get-EditorCandidate 'ControlType.Edit' '地址和搜索栏' $false $true $false $pattern
    actualEdit=Get-EditorCandidate 'ControlType.Edit' '发消息或创建任务, / 调用指令' $false $true $false $pattern
    preferred=Select-EditorState -NamedDrafts $fakeNamed.ToArray() -NamedUnreadable $false -FallbackDrafts $fakeFallback.ToArray() -FallbackUnreadable $false
    absent=Select-EditorState -NamedDrafts $fakeEmpty.ToArray() -NamedUnreadable $false -FallbackDrafts $fakeEmpty.ToArray() -FallbackUnreadable $false
    placeholder=Test-EditorPlaceholder 'Message ChatGPT' 'Message ChatGPT' $pattern
    ordinaryDraft=Test-EditorPlaceholder '用户实际输入的任务说明' 'Message ChatGPT' $pattern
    ambiguous=Select-EditorState -NamedDrafts @() -NamedUnreadable $false -FallbackDrafts @() -FallbackUnreadable $false -NamedAmbiguous @('Message ChatGPT')
    realValue=Select-EditorState -NamedDrafts @('Message ChatGPT') -NamedUnreadable $false -FallbackDrafts @() -FallbackUnreadable $false
}}
ConvertTo-Json -InputObject $result -Depth 5 -Compress
"""
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        executable = Path(os.environ["WINDIR"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, encoding="utf-8", timeout=12, check=True)
        value = json.loads(result.stdout)
        self.assertTrue(value["inherited"]["Candidate"])
        self.assertFalse(value["inherited"]["Required"])
        for label in ("staticLabel", "readonlyOutput", "omnibox"):
            self.assertFalse(value[label]["Candidate"])
        self.assertTrue(value["actualEdit"]["Named"])
        self.assertEqual(value["preferred"]["Text"], "")
        self.assertEqual(value["preferred"]["Source"], "named")
        self.assertTrue(value["preferred"]["Readable"])
        self.assertFalse(value["absent"]["Present"])
        self.assertTrue(value["placeholder"])
        self.assertFalse(value["ordinaryDraft"])
        self.assertEqual(value["ambiguous"]["Text"], "Message ChatGPT")
        self.assertFalse(value["ambiguous"]["Readable"])
        self.assertTrue(value["ambiguous"]["Ambiguous"])
        self.assertEqual(value["realValue"]["Text"], "Message ChatGPT")
        self.assertTrue(value["realValue"]["Readable"])

    def test_helper_has_utf8_bom_for_windows_powershell_51(self):
        helper = Path(__file__).resolve().parents[1] / "windowrelay" / "read_ui.ps1"
        self.assertTrue(helper.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_default_editor_labels_cover_known_dsh_composers_but_not_workspace_or_omnibox(self):
        helper = Path(__file__).resolve().parents[1] / "windowrelay" / "read_ui.ps1"
        source = helper.read_text(encoding="utf-8-sig")
        match = re.search(r"\[string\]\$EditorRegex\s*=\s*'([^']+)'", source)
        self.assertIsNotNone(match)
        pattern = re.compile(match.group(1), re.IGNORECASE)
        labels = (
            "发消息或创建任务, / 调用指令, @ 文件或对话",
            "Message or run a task, / commands, @ files or sessions",
            "描述你想要构建的内容, / 调用指令, @ 文件或对话",
            "Describe what you want to build, / commands, @ files or sessions",
            "描述你的任务以生成计划", "describe your task to generate plan",
            "Cmd/Ctrl+Enter 插话发送全部排队消息", "Cmd/Ctrl+Enter steers all queued messages",
        )
        for label in labels:
            with self.subTest(label=label):
                self.assertIsNotNone(pattern.search(label))
        for label in ("选择一个工作区开始", "Choose a workspace to start", "Session unavailable",
                      "地址和搜索栏", "Address and search bar", "历史标题：发消息或创建任务"):
            with self.subTest(label=label):
                self.assertIsNone(pattern.search(label))

    def test_helper_is_read_only_bounded_and_process_scoped(self):
        result = Mock(returncode=0, stdout=json.dumps({"text": "test", "buttons": [], "truncated": False}), stderr="")
        with patch("windowrelay.observer.subprocess.run", return_value=result) as run:
            value = collect_ui(42)
        self.assertEqual(value["text"], "test")
        args, options = run.call_args
        self.assertEqual(args[0][:7], ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(args[0][-2:], ["-WindowHandle", "42"])
        self.assertEqual(options["timeout"], 12)
        self.assertNotIn("Set-ExecutionPolicy", args[0])


if __name__ == "__main__":
    unittest.main()
