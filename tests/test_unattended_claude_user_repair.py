"""Task-scoped repair permissions and native recovery; no model or UE launches."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from unattended import providers

SESSION = "99db06e4-336d-4d8e-927d-1db262334adf"
SOURCE = "93f21123-fe41-4218-83e6-411af1a855d3"
MODEL = "claude-opus-5-5"
HEAD = "a" * 40


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event(kind, **fields):
    return {"type": kind, "session_id": SESSION, **fields}


def stream():
    return [event("system", subtype="task_notification"),
            event("system", subtype="init", model=MODEL),
            event("result", subtype="success", is_error=False, result="foreground", modelUsage={MODEL: {}}, result_index=0),
            event("system", subtype="task_notification"),
            event("system", subtype="init", model=MODEL),
            event("result", subtype="success", is_error=False, result="Committed after background checks.",
                  modelUsage={MODEL: {}}, result_index=1,
                  origin={"kind": "task-notification", "producer": "session-task", "runId": "owned-1"})]


class ClaudeUserRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = self.root / "runtime"
        self.work = self.runtime / "workspace"; self.work.mkdir(parents=True)
        (self.work / "ZhuGuoCity.uproject").write_text("{}")
        self.out = self.runtime / "runs" / "code-repair"
        self.out.parent.mkdir()
        self.task_id = "TASK-LOCAL-0091"
        self.taskdir = self.runtime / "tasks" / self.task_id; self.taskdir.mkdir(parents=True)
        self.engine = self.root / "UE_5.8" / "Engine"
        for name in ["Binaries/Win64/UnrealEditor.exe", "Binaries/Win64/UnrealEditor-Cmd.exe"]:
            p = self.engine / name; p.parent.mkdir(parents=True, exist_ok=True); p.touch()
        (self.engine / "Source/Programs/UnrealBuildTool").mkdir(parents=True)
        self.python = self.root / "Python" / "python.exe"; self.python.parent.mkdir(); self.python.touch()
        self.framework = self.root / "framework"; self.framework.mkdir()
        (self.framework / "full_repair_check.py").write_text("# trusted test fixture")
        self.run = self.runtime / "full-flow/runs" / ("b" * 32)
        full = {"task_id": self.task_id, "head_sha": HEAD, "run_dir": str(self.run),
                "engine_dir": str(self.engine), "request_sha256": "c" * 64}
        save(self.run / "request.json", full)
        self.visuals = [f"ZhuGuoCity.Local.Scene{n:02}Visual" for n in range(19)]
        save(self.run / "source-manifest.json", {"head_sha": HEAD,
             "automation_inventory": {"visual": {name: {} for name in self.visuals}}})
        save(self.run / "checks/retest/receipt.json", {"task_id": self.task_id, "head_sha": HEAD,
             "request_sha256": full["request_sha256"], "source_manifest_sha256": sha(self.run / "source-manifest.json"),
             "status": "failed", "finished_at": "2026-11-08T12:00:00+00:00"})
        (self.run / "report.md").write_text("Real report", encoding="utf8")
        save(self.run / "native/analysis-4/answer.json", {"session_id": SESSION})
        records = []
        for role, relative in [("full_flow_request", "request.json"), ("source_manifest", "source-manifest.json"),
                               ("retest", "checks/retest/receipt.json"), ("report", "report.md"),
                               ("model_answer", "native/analysis-4/answer.json")]:
            p = self.run / relative
            records.append({"role": role, "path": str(p), "sha256": sha(p), "size_bytes": p.stat().st_size})
        self.request_path = self.runtime / "user-repairs" / ("d" * 32) / "request.json"
        self.request = {"version": 1, "id": "d" * 32, "task_id": self.task_id,
                        "user_instruction": "Let Opus fix every verified issue.", "authorized_at": "2026-11-08T12:01:00+00:00",
                        "source_head": HEAD, "source_state_sha256": "e" * 64, "source_scope_sha256": "f" * 64,
                        "source_full_flow_run": str(self.run), "source_session_id": SOURCE, "target_session_id": SESSION,
                        "model": MODEL, "reasoning_effort": "high", "rework_window": 3, "max_cycles": 3, "evidence": records}
        save(self.request_path, self.request)
        self.profile = {key: self.request[key] for key in ["version", "id", "model", "reasoning_effort", "source_head",
                        "source_full_flow_run", "rework_window", "max_cycles"]}
        self.profile.update(epoch=self.request["id"], request_path=str(self.request_path), request_sha256=sha(self.request_path), status="active")
        task = {"id": self.task_id, "coding_provider": "claude", "session_id": SESSION,
                "session_owner_task_id": self.task_id, "user_repair": deepcopy(self.profile),
                "core_tests": ["ZhuGuoCity.M3." + name for name in ["DeathRules", "DeathFlow", "DeathHud", "DeathSave", "HealthFlow"]]}
        self.state = {"phase": "CODE", "task": task, "operation": {
            "kind": "code", "task_id": self.task_id, "output_dir": str(self.out), "head_sha": "9" * 40,
            "coding_provider": "claude", "coding_model": MODEL, "coding_reasoning_effort": "high", "requested_session_id": SESSION,
            "user_repair_id": self.profile["id"], "user_repair_request_sha256": self.profile["request_sha256"]}}
        self.persist_state()
        self.config = {"runtime_dir": str(self.runtime), "coding_provider": "claude", "_user_repair_profile": deepcopy(self.profile),
                       "claude": {"command": ["claude.exe"], "model": MODEL, "reasoning_effort": "high",
                                  "ue_preflight": True, "planned_core_tests": True},
                       "checks": [{"id": "ue-editor-build", "argv": [str(self.python), "-build", "{workspace}"]},
                                  {"id": "ue-task-validation", "argv": [str(self.python), "--engine", str(self.engine)]}]}
        self.provider = providers.AgentProviders(self.config)
        self.cache = self.root / "native-cache" / SESSION / "tasks"
        self.addCleanup(patch.stopall)
        patch("unattended.providers.__file__", str(self.framework / "providers.py")).start()
        patch("unattended.providers.sys.executable", str(self.python)).start()
        patch("unattended.full_flow_validation._task_output_directory", return_value=self.cache).start()
        patch("unattended.providers.claude_subscription_ready", return_value=True).start()
        self.launch = patch("unattended.providers.run_process").start()
        self.rows = stream()
        self.launch.side_effect = self.capture

    def persist_state(self):
        save(self.runtime / "state.json", self.state)

    def capture(self, *args, **kwargs):
        self.out.mkdir(exist_ok=True)
        formatter = kwargs["stdout_record_filter"]
        raw = b"".join(formatter((json.dumps(row) + "\n").encode()) for row in self.rows)
        (self.out / "stdout.log").write_bytes(raw)
        (self.out / "stderr.log").write_text("")
        return ProcessResult(0, False, False, self.out / "stdout.log", self.out / "stderr.log")

    def invoke(self):
        return self.provider.code("Perform this explicitly authorized repair.", self.work, self.out, 7200, session_id=SESSION)

    def test_exact_permissions_preserve_workspace_edits_and_all_required_tests(self):
        result = self.invoke()
        argv = self.launch.call_args.args[0]
        options = self.launch.call_args.kwargs
        self.assertEqual(argv[argv.index("--model") + 1], MODEL)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--resume") + 1], SESSION)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertNotIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", options["environment"])
        self.assertIn("Bash(git commit *)", argv)
        self.assertIn("Bash(git push *)", argv[argv.index("--disallowedTools"):argv.index("--allowedTools")])
        policy = json.loads(argv[argv.index("--settings") + 1])["permissions"]
        self.assertNotIn(str(self.runtime), policy["additionalDirectories"])
        self.assertNotIn(str(self.root), policy["additionalDirectories"])
        self.assertNotIn(str(self.work), policy["additionalDirectories"])
        for directory in [self.run, self.taskdir, self.framework, self.python.parent, self.cache]:
            self.assertIn(str(directory), policy["additionalDirectories"])
            pattern = directory.as_posix()
            if directory.drive: pattern = "/" + directory.drive[0].lower() + pattern[2:]
            self.assertIn(f"Edit(/{pattern}/**)", policy["deny"])
        bash = [item for item in argv if item.startswith("Bash(")]
        self.assertEqual(sum("full_repair_check.py" in item for item in bash), 1)
        self.assertTrue(next(item for item in bash if "full_repair_check.py" in item).endswith("--self-test)"))
        self.assertEqual(sum("Scene" in item and "Visual$" in item for item in bash), 19)
        self.assertTrue(any("RunTests ^ZhuGuoCity.M'" in item or "RunTests ^ZhuGuoCity.M\"" in item for item in bash))
        self.assertTrue(any("PackageIntegration$" in item for item in bash))
        for name in self.state["task"]["core_tests"]:
            self.assertTrue(any(name + "$" in item for item in bash))
        prompt = options["stdin"].decode()
        self.assertIn("run_in_background=true", prompt); self.assertIn("timeout=3600000", prompt)
        self.assertIn("不能代替提交后的独立 CHECK", prompt)
        self.assertEqual(result["text"], "Committed after background checks.")
        self.assertEqual(result["model_selection"]["turn_count"], 2)

    def test_recovery_uses_final_verified_turn_and_preserves_raw_log(self):
        result = self.invoke(); raw = (self.out / "stdout.log").read_bytes()
        (self.out / "result.json").unlink()
        (self.out / "model-selection.json").unlink()  # crash before optional sidecar remains recoverable
        self.assertEqual(self.provider.recover("code", self.out), result)
        self.assertEqual((self.out / "stdout.log").read_bytes(), raw)
        self.launch.assert_called_once()
        record = json.loads((self.out / "process.json").read_text())
        self.assertEqual(record["native_protocol"], "full_flow_native_v1")
        self.assertEqual(record["head_sha"], "9" * 40)  # successor operation, not original authorization HEAD
        self.assertEqual(record["user_repair_profile"], self.profile)

    def test_wrong_current_task_operation_model_or_session_cannot_gain_repair_permissions(self):
        original = deepcopy(self.state)
        cases = [("phase", "CHECK"), ("task_id", "OTHER"), ("coding_model", "claude-sonnet-5-5"),
                 ("requested_session_id", SOURCE), ("user_repair_request_sha256", "0" * 64), ("output_dir", str(self.out.parent / "other"))]
        for key, value in cases:
            self.state = deepcopy(original)
            if key == "phase": self.state[key] = value
            else: self.state["operation"][key] = value
            self.persist_state()
            with self.subTest(key=key), self.assertRaises(providers.ProviderError): self.invoke()
        self.launch.assert_not_called()

    def test_changed_request_or_source_evidence_is_rejected_before_launch(self):
        (self.run / "report.md").write_text("changed evidence")
        with self.assertRaisesRegex(providers.ProviderError, "evidence changed"): self.invoke()
        self.launch.assert_not_called()

    def test_later_wrong_model_or_foreign_session_never_becomes_delivery(self):
        for key, value in [("model", "claude-sonnet-5-5"), ("session_id", SOURCE)]:
            self.rows = stream(); self.rows[4][key] = value
            self.out = self.runtime / "runs" / ("code-" + key)
            self.state["operation"]["output_dir"] = str(self.out); self.persist_state()
            with self.subTest(key=key), self.assertRaises(providers.ProviderError): self.invoke()
            self.assertFalse((self.out / "result.json").exists())

    def test_later_quota_failure_is_transient_without_delivering_earlier_answer(self):
        self.rows.insert(5, event("rate_limit_event", rate_limit_info={"status": "rejected", "resetsAt": 2_000_000_000}))
        self.rows.insert(6, event("assistant", error="rate_limit", message={"content": []}))
        self.rows[-1].update(subtype="error_during_execution", is_error=True, result="usage limit")
        with self.assertRaises(providers.ProviderError) as caught: self.invoke()
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.retry_reason, "claude_subscription_limit")
        self.assertFalse((self.out / "result.json").exists())
        with self.assertRaises(providers.ProviderError): self.provider.recover("code", self.out)

    def test_recovery_cannot_cross_profile_or_replace_result_and_native_evidence(self):
        self.invoke()
        with self.assertRaises(providers.ProviderError): providers.AgentProviders({}).recover("code", self.out)
        saved = json.loads((self.out / "result.json").read_text())
        save(self.out / "result.json", saved | {"text": "forged pass"})
        with self.assertRaisesRegex(providers.ProviderError, "differs"): self.provider.recover("code", self.out)
        save(self.out / "result.json", saved)
        self.state["task"]["user_repair"]["status"] = "complete"; self.persist_state()
        with self.assertRaises(providers.ProviderError): self.provider.recover("code", self.out)

    def test_linked_evidence_subtree_cannot_expand_external_permissions(self):
        linked = self.run / "linked"; linked.mkdir()
        original = providers._reparse
        with patch("unattended.providers._reparse", side_effect=lambda path: Path(path) == linked or original(path)):
            with self.assertRaisesRegex(providers.ProviderError, "linked repair evidence"): self.invoke()
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
