"""MCP handoff display reads only bound public receipts, never configuration."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from tests import test_unattended_dashboard_user_repair as fixtures
from unattended import dashboard_evidence as evidence


class UserFollowupDashboardTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.UserRepairDashboardTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.base, self.task, self.state = self.f.base, self.f.task, self.f.state
        self.identity = "f" * 32; self.root = self.base.runtime / "user-followups" / self.identity
        self.request = {"version": 1, "id": self.identity, "task_id": self.task["id"], "repair_id": self.f.identity,
            "repair_request_sha256": self.task["user_repair"]["request_sha256"], "session_id": self.task["session_id"],
            "source_head": "a" * 40, "max_turns": 3, "user_instruction": "Let Claude connect UE MCP itself."}
        path = self.root / "request.json"; self.base.write(path, self.request)
        self.profile = {key: self.request[key] for key in ("version", "id", "task_id", "repair_id", "repair_request_sha256", "session_id", "source_head", "max_turns")}
        self.profile.update(request_path=str(path), request_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), status="active")
        self.task["user_followup"] = self.profile
        self.op = self.state["operation"]
        self.op.update(user_followup_id=self.identity, user_followup_request_sha256=self.profile["request_sha256"], user_followup_turn=2)
        self.f.f.save()

    def proof(self):
        return {"completed": True, "evidence": {"connected": True, "server_name": "ue", "registered_tools": ["mcp__ue__status"],
            "invocations": [{"tool_name": "mcp__ue__status", "completed": True, "is_error": False}],
            "raw_stdout_sha256": hashlib.sha256((self.f.f.run / "stdout.log").read_bytes()).hexdigest(),
            "config_path": "PRIVATE_MACHINE_CONFIG", "script": "PRIVATE_SCRIPT_CONTENT"}}

    def read(self, ref):
        return evidence.read_node_evidence(self.base.config_path, "development", "code", ref)[0].decode()

    def test_owned_code_reports_actual_turn_without_changing_main_repair_or_history(self):
        value = self.f.snapshot()
        self.assertIn("Claude自主接入UE MCP，回合2/3", value["status"]["summary"])
        code = next(row for row in value["workflow"]["nodes"] if row["id"] == "code")
        self.assertEqual(code["title"], "Claude自主接入UE MCP，回合2/3")
        self.assertEqual((code["model"], code["effort"]), ("claude-opus-5-5", "high"))
        self.assertEqual(value["task"]["reworks"], 10)
        self.op["user_followup_id"] = "0" * 32; self.f.f.save()
        self.assertNotIn("回合2/3", self.f.snapshot()["status"]["summary"])

    def test_bound_turn_delivery_and_current_connection_receipt_exclude_configuration_and_scripts(self):
        proof = self.proof(); result = {"text": "Claude independently connected and checked the tool.\n```python\nPRIVATE_SCRIPT_CONTENT\n```", "user_followup": proof}
        path = self.root / "turn-1.json"
        turn_op = {**self.op, "user_followup_turn": 1}
        self.base.write(path, {"version": 1, "id": self.identity, "task_id": self.task["id"], "request_sha256": self.profile["request_sha256"],
            "operation": turn_op, "candidate": {"head_sha": "a" * 40}, "result": result})
        path.with_suffix(".md").write_text(result["text"], encoding="utf-8")
        self.task["user_followup_results"] = [{"id": self.identity, "turn": 1, "receipt_path": str(path),
            "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "completed": False}]
        self.base.write(self.f.f.run / "process.json", {"task_id": self.task["id"], "head_sha": self.op["head_sha"], "output_dir": str(self.f.f.run),
            "user_followup_profile": self.profile, "user_followup_turn": 2, "user_followup": proof})
        self.base.write(self.f.f.run / "ue-mcp-evidence.json", proof); self.f.f.save()
        detail = self.f.detail(); rows = [row for row in detail["artifacts"] if "MCP" in row["title"] or "ue-mcp-evidence" in row["title"]]
        self.assertTrue(any("turn-1.json" in row["title"] for row in rows))
        self.assertTrue(any("turn-1.md" in row["title"] for row in rows))
        self.assertTrue(any("ue-mcp-evidence.json" in row["title"] for row in rows))
        text = "\n".join(self.read(row["id"]) for row in rows)
        self.assertIn("mcp__ue__status", text); self.assertIn("Claude independently connected", text)
        self.assertNotIn("PRIVATE_MACHINE_CONFIG", text); self.assertNotIn("PRIVATE_SCRIPT_CONTENT", text)
        self.assertNotIn(self.task["session_id"], text)
        self.task["user_followup_results"][0]["receipt_sha256"] = "0" * 64; self.f.f.save()
        self.assertFalse(any("turn-1" in row["title"] for row in self.f.detail()["artifacts"]))

    def test_completed_requires_actual_connection_proof_and_exhausted_does_not_claim_success(self):
        self.profile["status"] = "completed"; self.f.f.save()
        self.assertNotIn("user_followup", self.f.snapshot()["task"])
        with patch("unattended.user_followup.completed_followup_evidence", return_value=self.proof()):
            self.assertIn("实际连接与工具调用已证", self.f.snapshot()["task"]["user_followup"]["summary"])
        bad = self.proof(); bad["evidence"]["connected"] = False
        with patch("unattended.user_followup.completed_followup_evidence", return_value=bad):
            self.assertNotIn("user_followup", self.f.snapshot()["task"])
        self.profile["status"] = "exhausted"; self.f.f.save()
        self.assertIn("仍未证实", self.f.snapshot()["task"]["user_followup"]["summary"])

    def test_foreign_request_or_task_cannot_borrow_a_handoff_label(self):
        self.profile["request_sha256"] = "0" * 64; self.f.f.save()
        value = self.f.snapshot()
        self.assertNotIn("user_followup", value["task"])
        self.assertIn("待核实", value["task"]["user_followup_error"])

    def notification_prefix(self):
        session = self.task["session_id"]
        return [
            {"type": "system", "subtype": "task_notification", "session_id": session},
            {"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-5-5"},
            {"type": "result", "subtype": "success", "session_id": session, "origin": {"kind": "task-notification"},
             "is_error": False, "result_index": 0},
            {"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-5-5"},
            {"type": "system", "subtype": "thinking_tokens", "session_id": session},
            {"type": "assistant", "session_id": session, "message": {"content": [{"type": "tool_use", "name": "Read"}]}}
        ]

    def write_opening(self, events):
        (self.f.f.run / "stdout.log").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def test_same_session_background_turn_prefix_preserves_current_native_model_and_active_clock(self):
        self.write_opening(self.notification_prefix())
        self.state["last_error"] = "ProviderError: Provider exceeded its time limit"; self.f.f.save()
        value = self.f.snapshot(); code = next(row for row in value["workflow"]["nodes"] if row["id"] == "code")
        self.assertEqual((code["model"], code["effort"], code["model_source"], code["state"]), ("claude-opus-5-5", "high", "recorded", "running"))
        self.assertFalse(value["status"]["attempt_finished"])
        self.assertIsNotNone(value["status"]["elapsed_seconds"])
        self.assertIsNone(value["status"]["error"])
        self.assertIn("previous_error", value["status"])
        # Model identity survives a terminal event in the bounded header;
        # only the owning process receipt determines controller completion.
        events = self.notification_prefix() + [{"type": "result", "subtype": "success", "session_id": self.task["session_id"], "is_error": False}]
        self.write_opening(events)
        value = self.f.snapshot(); code = next(row for row in value["workflow"]["nodes"] if row["id"] == "code")
        self.assertEqual(code["model"], "claude-opus-5-5")
        self.assertFalse(value["status"]["attempt_finished"])

    def test_prefix_foreign_session_conflicting_models_unrecognized_metadata_and_foreign_operation_are_rejected(self):
        original = deepcopy(self.op)
        for variant in ("session", "model", "metadata", "no_current_init", "operation"):
            with self.subTest(variant=variant):
                self.op.clear(); self.op.update(deepcopy(original))
                events = self.notification_prefix()
                if variant == "session": events[0]["session_id"] = "550e8400-e29b-41d4-a716-446655440099"
                elif variant == "model": events[3]["model"] = "claude-sonnet-5-5"
                elif variant == "metadata": events[0]["subtype"] = "unrecognized_notification"
                elif variant == "no_current_init": events = events[:3]
                else: self.op["user_followup_request_sha256"] = "0" * 64
                self.write_opening(events); self.f.f.save()
                value = self.f.snapshot(); code = next(row for row in value["workflow"]["nodes"] if row["id"] == "code")
                self.assertNotEqual(code["model_source"], "recorded")


if __name__ == "__main__":
    unittest.main()
