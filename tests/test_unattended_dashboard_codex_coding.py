"""Native Codex development stays distinct from ordinary Responses API review."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import unittest

from tests import test_unattended_dashboard_state as fixtures
from unattended import dashboard_evidence, dashboard_flows, dashboard_state
from unattended.config import load_config


class CodexCodingDashboardTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DashboardStateTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config, self.state = self.f.config, self.f.state
        self.config.update(coding_provider="codex", planning_provider="claude",
            claude={"model": "claude-fable-5-2", "reasoning_effort": "high"})
        self.config["codex"].update(model="gpt-6.2", reasoning_effort="high", review_transport="responses_api",
            coding_model="gpt-6.2-sol", coding_reasoning_effort="high")
        self.f.write(self.f.config_path, self.config)
        actual = load_config(self.f.config_path)
        keys = ("project_path", "goal", "branch", "base_ref", "checks", "push", "remote", "task_prefix")
        self.state["binding"] = hashlib.sha256(json.dumps({k: actual.get(k) for k in keys}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.head, self.session = "a" * 40, "019a-codex-thread"
        self.task = {"id": "TASK-1", "title": "Current feature", "body": "Original scope", "reworks": 0,
            "coding_provider": "codex", "base_sha": self.head, "code_base": self.head,
            "candidate": {"head_sha": self.head}, "session_id": self.session, "session_owner_task_id": "TASK-1",
            "planning_level": "routine", "planning_model": "claude-fable-5-2", "planning_reasoning_effort": "high",
            "check_results": []}
        self.run = self.f.runtime / "runs" / ("code-" + "c" * 32)
        self.run.mkdir(parents=True)
        self.op = {"kind": "code", "task_id": "TASK-1", "head_sha": self.head,
            "coding_provider": "codex", "coding_model": "gpt-6.2-sol", "coding_reasoning_effort": "high",
            "output_dir": str(self.run), "started_at": "2026-11-02T11:00:00+00:00"}
        self.state.update(phase="CODE", task=self.task, operation=self.op)
        self.events = [{"type": "thread.started", "thread_id": self.session}, {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "PRIVATE_COMMAND_ARGUMENT"}}]
        self.logs()

    def logs(self):
        (self.run / "stdout.log").write_text("".join(json.dumps(event) + "\n" for event in self.events), encoding="utf-8")

    def snapshot(self):
        return self.f.snapshot()

    @staticmethod
    def developer(snapshot):
        return next(row for row in snapshot["modules"] if row["id"] == "developer")

    def receipt(self):
        value = {"kind": "code", "coding_provider": "codex", "coding_model": "gpt-6.2-sol",
            "coding_reasoning_effort": "high", "coding_protocol": "codex_exec_v1", "coding_sandbox": "workspace-write",
            "codex_auth_mode": "chatgpt", "session_id": self.session, "requested_session": self.session,
            "exit_code": 0, "timed_out": False, "output_limit_exceeded": False}
        self.f.write(self.run / "process.json", value)
        os.utime(self.run / "process.json", (self.f.now.timestamp(), self.f.now.timestamp()))
        return value

    def test_live_native_code_shows_sol_high_and_thread_without_responses_review_progress(self):
        self.task["session_id"] = None
        value = self.snapshot()
        self.assertIsNone(value["error"])
        self.assertEqual(value["status"]["actor"], "Codex")
        self.assertEqual(value["task"]["session_id"], self.session)
        self.assertEqual(value["task"]["session_provider"], "Codex")
        self.assertFalse(value["status"]["attempt_finished"])
        self.assertIsNone(value["api_review"])
        self.assertEqual(value["codex_auth"], {"policy": "chatgpt", "current": None, "fallback": False})
        coder = self.developer(value)
        self.assertEqual((coder["model"], coder["effort"], coder["model_source"], coder["state"]),
                         ("gpt-6.2-sol", "high", "configured", "running"))
        self.assertEqual(value["models"]["codex"]["model"], "gpt-6.2")
        self.assertEqual(value["models"]["codex_coding"]["model"], "gpt-6.2-sol")
        flow = dashboard_flows.collect_flows(self.f.config_path, value)["development"]
        code = next(row for row in flow["nodes"] if row["id"] == "code")
        self.assertEqual((code["actor"], code["model"], code["model_source"]), ("Codex", "gpt-6.2-sol", "configured"))
        self.assertNotIn("PRIVATE_COMMAND_ARGUMENT", json.dumps(value))

    def test_native_receipt_confirms_subscription_and_completion_without_claiming_observed_model(self):
        self.events.append({"type": "turn.completed"})
        self.logs()
        self.receipt()
        value = self.snapshot()
        self.assertTrue(value["status"]["attempt_finished"])
        self.assertEqual(value["codex_auth"], {"policy": "chatgpt", "current": "chatgpt", "fallback": False})
        self.assertEqual(self.developer(value)["model_source"], "configured")
        detail = dashboard_evidence.collect_node_detail(self.f.config_path, "development", "code", snapshot=value)
        receipt = next(row for row in detail["artifacts"] if "执行回执" in row["title"])
        body = dashboard_evidence.read_node_evidence(self.f.config_path, "development", "code", receipt["id"])[0].decode()
        self.assertIn("codex_exec_v1", body)
        self.assertIn("workspace-write", body)

    def test_native_model_metadata_is_recorded_and_config_change_does_not_relabel_old_invocation(self):
        self.receipt()
        self.config["codex"]["coding_model"] = "next-coding-model"
        self.f.write(self.f.config_path, self.config)
        value = self.snapshot()
        self.assertEqual((self.developer(value)["model"], self.developer(value)["model_source"], self.developer(value)["configured_model"]),
                         ("gpt-6.2-sol", "configured", "next-coding-model"))
        self.assertEqual(value["models"]["codex_coding"]["model"], "gpt-6.2-sol")
        self.events[0]["model"] = "gpt-6.2-sol"
        self.logs()
        self.assertEqual(self.developer(self.snapshot())["model_source"], "recorded")

    def test_foreign_native_thread_task_head_or_protocol_cannot_supply_current_code_model(self):
        baseline = self.receipt()
        original = deepcopy(self.op)
        for variant in ("thread", "task", "head", "protocol", "request", "model"):
            with self.subTest(variant=variant):
                record = deepcopy(baseline)
                self.op.clear(); self.op.update(original)
                self.events[0]["thread_id"] = self.session
                if variant == "thread": self.events[0]["thread_id"] = "foreign-thread"
                elif variant == "task": self.op["task_id"] = "OTHER"
                elif variant == "head": self.op["head_sha"] = "b" * 40
                elif variant == "protocol": record["protocol"] = "openai_responses_v1"
                elif variant == "request": record["requested_session"] = "foreign-thread"
                else: record["coding_model"] = "foreign-model"
                self.logs()
                self.f.write(self.run / "process.json", record)
                developer = self.developer(self.snapshot())
                self.assertIsNone(developer["model"])
                self.assertNotEqual(developer["model_source"], "recorded")

    def test_codex_coding_does_not_accept_a_dsh_session_record(self):
        self.task["session_id"] = None
        self.events[0] = {"type": "session", "sessionId": self.session}
        self.logs()
        self.assertIsNone(self.snapshot()["task"]["session_id"])

    def test_review_still_uses_direct_responses_route_and_separate_review_model(self):
        self.state["phase"] = "REVIEW"
        self.op.update(kind="review", output_dir=str(self.f.runtime / "runs" / ("review-" + "d" * 32)))
        out = Path(self.op["output_dir"])
        self.f.write(out / "process.json", {"kind": "review", "protocol": "openai_responses_v1", "status": "running",
            "exit_code": None, "head_sha": self.head, "turns": []})
        value = self.snapshot()
        self.assertEqual(value["codex_auth"]["current"], "responses_api")
        self.assertEqual(value["api_review"]["api_status"], "running")
        self.assertEqual(value["models"]["codex"]["model"], "gpt-6.2")
        self.assertEqual(value["models"]["codex_coding"]["model"], "gpt-6.2-sol")
        self.assertFalse(self.developer(value)["current"])


if __name__ == "__main__":
    unittest.main()
