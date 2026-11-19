"""Subscription captures keep their managed operation and immutable evidence."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests import test_unattended_code_timeout_replanning as timeout_fixtures
from tests import test_unattended_dashboard_current_attempt as current_fixtures
from tests import test_unattended_dashboard_replanning as planning_fixtures
from tests import test_unattended_dashboard_user_followup as dashboard_fixtures
from tests import test_unattended_user_followup as followup_fixtures
from unattended import user_followup
from unattended.storage import read_json, write_json, write_text


def fallback_capture(logical, kind, session, model, *, finished=True, fresh=False):
    """Keep original fixture output as the authenticated subscription attempt."""
    child = logical / "claude-subscription"
    child.mkdir()
    child_record = read_json(logical / "process.json") if (logical / "process.json").exists() else {}
    for name in ("process.json", "stdout.log", "stderr.log", "result.json", "ue-mcp-evidence.json"):
        path = logical / name
        if path.exists():
            path.rename(child / name)
    provider, model_field, effort_field = {
        "code": ("coding_provider", "coding_model", "coding_reasoning_effort"),
        "plan": ("planning_provider", "planning_model", "planning_reasoning_effort"),
    }[kind]
    write_json(logical / "process.json", {
        "kind": kind, provider: "claude", model_field: model, effort_field: "high",
        "claude_auth_mode": "api_key", "claude_subscription_fallback": True,
        "claude_api_provider": "firstParty", "exit_code": 1, "timed_out": False,
        "output_limit_exceeded": False, "requested_session": None if fresh else session})
    events = [
        {"type": "system", "subtype": "init", "session_id": session, "model": model},
        {"type": "assistant", "session_id": session, "error": "billing_error"},
        {"type": "result", "subtype": "success", "session_id": session, "is_error": True,
         "api_error_origin": "first_party", "api_error_status": 400, "terminal_reason": "api_error",
         "result": "Credit balance is too low"},
    ]
    write_text(logical / "stdout.log", "".join(json.dumps(event) + "\n" for event in events))
    write_text(logical / "stderr.log", "")
    if finished:
        child_record.update(kind=kind, claude_auth_mode="subscription", requested_session=session,
                            claude_capture_directory=str(child), **{provider: "claude", model_field: model, effort_field: "high"})
        child_record["claude_fallback_source"] = {
            "kind": kind, "session_id": session,
            "process_sha256": user_followup.file_sha(logical / "process.json"),
            "stdout_sha256": user_followup.file_sha(logical / "stdout.log")}
        write_json(child / "process.json", child_record)
    return child


class ClaudeFallbackEvidenceTests(unittest.TestCase):
    def fixture(self, cls):
        value = cls()
        value.setUp()
        self.addCleanup(value.doCleanups)
        return value

    def test_followup_source_uses_successful_child_but_stays_bound_to_logical_operation(self):
        fixture = self.fixture(followup_fixtures.UserFollowupTests)
        fixture.engine.queue_user_followup(fixture.path)
        fixture.complete_source()
        logical = Path(fixture.op["output_dir"])
        child = fallback_capture(logical, "code", followup_fixtures.TARGET, user_followup.MODEL)
        state = fixture.state_now()
        self.assertTrue(fixture.engine._consume_user_followup(state, fixture.work))
        activation = read_json(fixture.path.parent / "activation.json")
        source = activation["source_proof"]
        self.assertEqual(source["operation_output_dir"], str(logical))
        self.assertEqual(source["process_path"], str(child / "process.json"))
        self.assertEqual(source["stdout_sha256"], user_followup.file_sha(child / "stdout.log"))
        self.assertNotEqual(source["stdout_sha256"], user_followup.file_sha(logical / "stdout.log"))
        record = read_json(child / "process.json")
        record["output_dir"] = str(child)
        write_json(child / "process.json", record)
        with self.assertRaisesRegex(ValueError, "completed Claude/session receipt"):
            fixture.engine._followup_source_proof(fixture.request, state["task"])

    def test_completed_followup_hashes_child_process_and_stdout_and_rejects_modified_receipt(self):
        fixture = self.fixture(followup_fixtures.UserFollowupTests)
        fixture.consume()
        invoke = fixture.result(True)

        def code(prompt, work, out, timeout, **options):
            result = invoke(prompt, work, out, timeout, **options)
            write_json(out / "process.json", {"exit_code": 0, "output_dir": str(out)})
            fallback_capture(out, "code", followup_fixtures.TARGET, user_followup.MODEL)
            return result

        fixture.providers.code.side_effect = code
        state = fixture.state_now()
        fixture.engine._code(state, fixture.work)
        receipt = read_json(fixture.path.parent / "turn-1.json")
        logical = Path(receipt["operation"]["output_dir"])
        child = logical / "claude-subscription"
        proof = receipt["native_receipt"]
        self.assertEqual(proof["operation_output_dir"], str(logical))
        self.assertEqual(proof["stdout_path"], str(child / "stdout.log"))
        self.assertEqual(proof["process_sha256"], user_followup.file_sha(child / "process.json"))
        fixture.engine._user_followup_gate(state["task"])
        with (child / "process.json").open("ab") as stream:
            stream.write(b" ")
        with self.assertRaisesRegex(ValueError, "raw tool proof"):
            fixture.engine._user_followup_gate(state["task"])

    def test_current_native_code_uses_live_child_activity_and_clock_without_hiding_tampering(self):
        fixture = self.fixture(current_fixtures.CurrentCodeAttemptTests)
        with (fixture.run / "stdout.log").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "assistant", "session_id": fixture.task["session_id"],
                "message": {"content": [{"type": "tool_use", "name": "Edit"}]}}) + "\n")
        child = fallback_capture(fixture.run, "code", fixture.task["session_id"], "claude-sonnet-5-5", finished=False)
        value = fixture.snapshot()
        self.assertIsNone(value["error"])
        self.assertFalse(value["status"]["attempt_finished"])
        self.assertIsNone(value["status"]["error"])
        self.assertEqual(value["status"]["previous_error"], fixture.error)
        self.assertTrue(any("修改文件" in row["text"] for row in value["activity"]))
        self.assertEqual(next(row for row in value["workflow"]["nodes"] if row["id"] == "code")["state"], "running")
        write_json(child / "process.json", {"kind": "code", "claude_auth_mode": "subscription", "exit_code": 0})
        self.assertIsNotNone(fixture.snapshot()["error"])

    def test_current_claude_planner_uses_child_opening_and_both_attempts_are_discoverable(self):
        fixture = self.fixture(planning_fixtures.DashboardReplanningTests)
        fallback_capture(fixture.run, "plan", fixture.branch, "claude-opus-5-5", finished=False)
        value = fixture.snapshot()
        self.assertIsNone(value["error"])
        self.assertFalse(value["status"]["attempt_finished"])
        self.assertEqual(fixture.node(value)["state"], "running")
        self.assertEqual(fixture.node(value)["model"], "claude-opus-5-5")
        detail = fixture.detail()
        self.assertTrue(any("Claude Code 订阅续接" in row["title"] for row in detail["logs"]))
        plans = [row for row in detail["artifacts"] if "重新分析修复计划" in row["title"]]
        self.assertTrue(plans)
        self.assertIn(fixture.result["body"].encode(), fixture.read(plans[0]["id"])[0])

    def test_dashboard_followup_tool_proof_reads_actual_child_and_preserves_logical_binding(self):
        fixture = self.fixture(dashboard_fixtures.UserFollowupDashboardTests)
        proof = fixture.proof()
        logical = fixture.f.f.run
        fixture.base.write(logical / "process.json", {
            "task_id": fixture.task["id"], "head_sha": fixture.op["head_sha"], "output_dir": str(logical),
            "user_followup_profile": fixture.profile, "user_followup_turn": 2, "user_followup": proof})
        fixture.base.write(logical / "ue-mcp-evidence.json", proof)
        fallback_capture(logical, "code", fixture.task["session_id"], "claude-opus-5-5")
        detail = fixture.f.detail()
        row = next(row for row in detail["artifacts"] if "ue-mcp-evidence.json" in row["title"])
        body = fixture.read(row["id"])
        self.assertIn("mcp__ue__status", body)
        self.assertNotIn("PRIVATE_MACHINE_CONFIG", body)
        self.assertNotIn(fixture.task["session_id"], body)
        self.assertTrue(any("Claude Code 订阅续接" in row["title"] for row in detail["logs"]))

    def test_child_timeout_drives_replanning_and_fingerprints_both_immutable_attempts(self):
        fixture = self.fixture(timeout_fixtures.CodeTimeoutReplanningTests)
        op = deepcopy(fixture.legacy())
        logical = Path(op["output_dir"])
        child = fallback_capture(logical, "code", timeout_fixtures.SESSION, "claude-sonnet-5-5")
        task = fixture.f.state()["task"]
        self.assertEqual(fixture.engine._code_timeout_receipt(task, fixture.work, op), logical)
        self.assertEqual(fixture.engine.step()["phase"], "REPLAN")
        task = fixture.f.state()["task"]
        self.assertEqual(task["code_timeout_recoveries"][0]["output_dir"], str(logical))
        files = task["replanning"]["fingerprint"]["evidence_files"]
        for directory in (logical, child):
            for name in ("process.json", "stdout.log", "stderr.log"):
                self.assertEqual(files[str(directory / name)], user_followup.file_sha(directory / name))

    def test_interrupted_repair_child_retains_original_session_and_followup_source_proof(self):
        from unattended.user_repair import read_repair_session
        fixture = self.fixture(followup_fixtures.UserFollowupTests)
        fixture.interrupted_source()
        logical = Path(fixture.op["output_dir"])
        child = fallback_capture(logical, "code", followup_fixtures.TARGET, user_followup.MODEL)
        state = fixture.state_now()
        task = state["task"]
        self.assertEqual(read_repair_session(task, fixture.runtime, logical), followup_fixtures.TARGET)
        self.assertEqual(fixture.engine._code_timeout_receipt(task, fixture.work, fixture.op), logical)
        self.assertTrue(fixture.engine._consume_user_followup(state, fixture.work))
        proof = read_json(fixture.path.parent / "source-interruption.json")
        self.assertEqual(proof["process_path"], str(child / "process.json"))
        self.assertEqual(proof["stdout_sha256"], user_followup.file_sha(child / "stdout.log"))
        user_followup.validate_followup_profile(state["task"], fixture.runtime)
        write_text(child / "stdout.log", (child / "stdout.log").read_text().replace(user_followup.MODEL, "claude-sonnet-5-5"))
        self.assertIsNone(read_repair_session(task, fixture.runtime, logical))
        with self.assertRaises(ValueError):
            fixture.engine._code_timeout_receipt(task, fixture.work, fixture.op)

    def test_initial_api_session_can_resume_in_subscription_and_recover_a_child_timeout(self):
        fixture = self.fixture(timeout_fixtures.CodeTimeoutReplanningTests)
        op = deepcopy(fixture.legacy())
        op["requested_session_id"] = None
        state = fixture.f.state()
        state["operation"] = op
        fixture.engine._save(state)
        logical = Path(op["output_dir"])
        fallback_capture(logical, "code", timeout_fixtures.SESSION, "claude-sonnet-5-5", fresh=True)
        self.assertEqual(fixture.engine._code_timeout_receipt(state["task"], fixture.work, op), logical)
        self.assertEqual(fixture.engine.step()["phase"], "REPLAN")
        source = fixture.f.state()["task"]["replanning"]["source_operation"]
        self.assertIsNone(source["requested_session_id"])
        self.assertEqual(read_json(logical / "claude-subscription/process.json")["requested_session"], timeout_fixtures.SESSION)


if __name__ == "__main__":
    unittest.main()
