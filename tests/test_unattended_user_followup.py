"""Natural-boundary handoffs never interrupt CODE or bypass final gates."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_user_repair as repair_fixtures
from unattended import user_followup as followup
from unattended.full_flow_native import parse_full_flow_native
from unattended.storage import read_json, write_json, write_text

TARGET = repair_fixtures.TARGET


class UserFollowupTests(unittest.TestCase):
    def setUp(self):
        self.f = repair_fixtures.UserRepairTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.activate()
        self.engine, self.runtime, self.work = self.f.engine, self.f.runtime, self.f.work
        self.providers = self.f.f.providers
        self.state = self.f.f.state()
        self.op = deepcopy(self.engine._new_operation(self.state, "code"))
        self.request = {"version": 1, "id": "1" * 32, "task_id": self.state["task"]["id"],
            "repair_id": self.f.id, "repair_request_sha256": followup.file_sha(self.f.path),
            "session_id": TARGET, "source_operation": self.op, "source_head": self.op["head_sha"],
            "user_instruction": "User instruction, authored without a controller implementation plan.",
            "authorized_at": self.f.f.f.now.isoformat(), "max_turns": 3}
        self.path = self.runtime / "user-followups" / self.request["id"] / "request.json"
        write_json(self.path, self.request)
        self.verify = patch("unattended.user_followup.gitops.verify_candidate", side_effect=lambda *a: deepcopy(self.f.f.state()["task"]["candidate"])).start()
        patch("unattended.user_followup.gitops.git", return_value="").start()

    def state_now(self):
        return self.engine._load()

    def complete_source(self):
        state = self.state_now(); task = state["task"]
        out = Path(self.op["output_dir"])
        self.native(out, "Original completed CODE delivery.")
        model = parse_full_flow_native(out / "stdout.log", TARGET)[4]
        write_json(out / "process.json", {"exit_code": 0, "timed_out": False, "output_limit_exceeded": False,
            "task_id": task["id"], "head_sha": self.op["head_sha"], "requested_session": TARGET,
            "output_dir": str(out), "workspace": str(self.work), "strict_model_selection": True,
            "native_protocol": "full_flow_native_v1", "coding_model": followup.MODEL, "coding_reasoning_effort": "high",
            "user_repair_profile": task["user_repair"], "model_selection": model})
        task["delivery_text"] = "Original completed CODE delivery."
        self.engine._delivery(task, task["delivery_text"])
        state.update(phase="CHECK", operation=None)
        self.engine._save(state)

    def native(self, out, text):
        events = [{"type": "system", "subtype": "init", "session_id": TARGET, "model": followup.MODEL},
                  {"type": "result", "subtype": "success", "session_id": TARGET, "is_error": False,
                   "result": text, "modelUsage": {followup.MODEL: {}}}]
        write_text(out / "stdout.log", "\n".join(json.dumps(row) for row in events) + "\n")

    def interrupted_source(self):
        self.op.update(window_limited=False, timeout_seconds=7200)
        self.request["source_operation"] = deepcopy(self.op)
        write_json(self.path, self.request)
        state = self.state_now(); state["operation"] = deepcopy(self.op); self.engine._save(state)
        self.engine.queue_user_followup(self.path)
        self.complete_source()
        out = Path(self.op["output_dir"])
        process = read_json(out / "process.json")
        process.update(kind="code", coding_provider="claude", timed_out=True, session_id=TARGET,
                       model_selection={"model_verified": False})
        write_json(out / "process.json", process)
        write_text(out / "stdout.log", json.dumps({"type": "system", "subtype": "init", "session_id": TARGET,
            "model": followup.MODEL}) + "\n" + json.dumps({"type": "unknown_incomplete_lifecycle", "session_id": TARGET}) + "\n")
        state = self.state_now()
        state["task"]["candidate"] = {"head_sha": "c" * 40, "recovered_from_git": True}
        state["task"]["code_timeout_recoveries"] = [{"operation_sha256": self.engine._replanning_digest(self.op),
            "output_dir": str(out), "at": self.engine.now().isoformat(), "decision": "recover_committed_code"}]
        self.engine._save(state)
        patch("unattended.user_followup.gitops.head", return_value="c" * 40).start()
        return state

    def consume(self):
        self.engine.queue_user_followup(self.path)
        self.complete_source()
        state = self.state_now()
        self.assertTrue(self.engine._consume_user_followup(state, self.work))
        return self.state_now()

    def result(self, completed):
        def code(prompt, work, out, timeout, session_id=None):
            self.assertEqual(session_id, TARGET)
            self.assertIn(self.request["user_instruction"], prompt)
            self.native(out, "Actual coder report.")
            return {"completed": True, "session_id": TARGET, "text": "Actual coder report.",
                "model_selection": parse_full_flow_native(out / "stdout.log", TARGET)[4],
                "user_followup": {"completed": completed, "evidence": {"connected": completed,
                    "raw_stdout_sha256": followup.file_sha(out / "stdout.log"), "invocations": []}}}
        return code

    def test_queue_uses_independent_lock_and_preserves_running_state_bytes(self):
        before = self.engine.state_path.read_bytes()
        with patch.object(self.engine, "_locks", side_effect=AssertionError("controller lock forbidden")):
            first = self.engine.queue_user_followup(self.path)
            self.assertEqual(self.engine.queue_user_followup(self.path), first)
        self.assertEqual(self.engine.state_path.read_bytes(), before)
        self.assertEqual(first["status"], "queued")
        self.providers.code.assert_not_called()

    def test_queue_rejects_wrong_current_operation_and_changed_authorization(self):
        for key, value in (("session_id", "0" * 36), ("repair_request_sha256", "0" * 64), ("max_turns", 4)):
            request = deepcopy(self.request); request[key] = value; write_json(self.path, request)
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.engine.queue_user_followup(self.path)
        write_json(self.path, self.request); self.engine.queue_user_followup(self.path)
        changed = deepcopy(self.request); changed["user_instruction"] += " Changed"; write_json(self.path, changed)
        with self.assertRaisesRegex(ValueError, "changed"): self.engine.queue_user_followup(self.path)

    def test_just_completed_code_can_be_queued_but_old_delivery_cannot(self):
        self.complete_source()
        self.assertEqual(self.engine.queue_user_followup(self.path)["status"], "queued")
        state = self.state_now(); state["task"]["delivery_text"] = "A later unrelated delivery"
        self.engine._save(state)
        with self.assertRaisesRegex(ValueError, "current delivery"):
            self.engine._consume_user_followup(state, self.work)

    def test_authorization_after_completed_receipt_is_rejected(self):
        self.complete_source()
        self.request["authorized_at"] = (self.engine.now() + timedelta(days=40)).isoformat()
        write_json(self.path, self.request)
        with self.assertRaisesRegex(ValueError, "authorized after"):
            self.engine.queue_user_followup(self.path)

    def test_source_model_and_current_source_hash_are_required(self):
        self.engine.queue_user_followup(self.path); self.complete_source()
        path = Path(self.op["output_dir"]) / "stdout.log"
        write_text(path, path.read_text().replace(followup.MODEL, "claude-sonnet-5-5"))
        with self.assertRaises(ValueError): self.engine._consume_user_followup(self.state_now(), self.work)
        self.assertEqual(self.state_now()["phase"], "CHECK")

    def test_consume_preserves_business_scope_session_history_and_all_check_gates(self):
        before = self.state_now()
        state = self.consume(); task = state["task"]
        self.assertEqual(state["phase"], "CODE")
        for field in ("body", "core_tests", "acceptance_criteria", "base_sha", "reworks", "replanning_history", "user_repair", "session_id"):
            self.assertEqual(task[field], before["task"][field], field)
        self.assertEqual(self.engine._task_checks(task)[-1]["id"], "ue-full-flow-repair")
        self.assertEqual(state["expected_head"], before["expected_head"])
        self.assertEqual(task["check_results"], [])

    def test_no_commit_followup_can_complete_with_real_provider_proof_then_all_checks(self):
        self.consume(); self.providers.code.side_effect = self.result(True)
        state = self.state_now(); self.engine._code(state, self.work)
        self.assertEqual(state["phase"], "CHECK")
        self.assertEqual(state["task"]["user_followup"]["status"], "completed")
        self.assertEqual(state["task"]["reworks"], 10)
        self.assertEqual(state["task"]["check_results"], [])
        self.engine._user_followup_gate(state["task"])
        self.assertFalse(self.engine._consume_user_followup(state, self.work))
        self.assertEqual(self.providers.code.call_count, 1)
        self.assertEqual(read_json(self.runtime / "user-followups.json")["requests"][0]["status"], "completed")

    def test_three_incomplete_turns_are_exhausted_and_never_implicitly_accepted(self):
        self.consume(); self.providers.code.side_effect = self.result(False)
        for turn in range(1, 4):
            state = self.state_now(); self.engine._code(state, self.work)
            self.assertEqual(state["task"]["user_followup_attempts"][-1]["turn"], turn)
        self.assertEqual(state["phase"], "BLOCKED")
        self.assertEqual(state["task"]["user_followup"]["status"], "exhausted")
        self.engine._code(state, self.work)
        self.assertEqual(self.providers.code.call_count, 3)
        with self.assertRaises(ValueError): self.engine._user_followup_gate(state["task"])
        self.assertEqual(state["accepted"], [])

    def test_mismatched_session_and_dirty_candidate_cannot_complete(self):
        self.consume(); self.providers.code.side_effect = self.result(True)
        self.verify.side_effect = RuntimeError("dirty candidate")
        state = self.state_now()
        with self.assertRaisesRegex(RuntimeError, "dirty candidate"): self.engine._code(state, self.work)
        self.assertEqual(state["task"]["user_followup"]["status"], "active")
        self.assertEqual(state["phase"], "CODE")

    def test_crash_after_turn_receipt_recovers_without_a_second_native_invocation(self):
        self.consume(); self.providers.code.side_effect = self.result(True)
        original = self.engine._save
        def save(state):
            if state["phase"] == "CHECK": raise OSError("fixture after turn receipt")
            return original(state)
        with patch.object(self.engine, "_save", side_effect=save):
            with self.assertRaises(OSError): self.engine._code(self.state_now(), self.work)
        receipt = read_json(self.path.parent / "turn-1.json")
        self.providers.recover.return_value = receipt["result"]
        state = self.state_now(); self.engine._code(state, self.work)
        self.assertEqual(state["phase"], "CHECK")
        self.assertEqual(self.providers.code.call_count, 1)
        self.assertEqual(len(state["task"]["user_followup_attempts"]), 1)
        self.assertEqual(len(state["task"]["user_followup_results"]), 1)

    def test_completion_gate_rejects_tampered_raw_tool_evidence(self):
        self.consume(); self.providers.code.side_effect = self.result(True)
        state = self.state_now(); self.engine._code(state, self.work)
        out = Path(state["task"]["user_followup_attempts"][0]["output_dir"])
        write_text(out / "stdout.log", "changed raw evidence")
        with self.assertRaisesRegex(ValueError, "raw tool proof"):
            self.engine._user_followup_gate(state["task"])

    def test_pause_does_not_consume_queued_followup_or_start_a_provider(self):
        self.engine.queue_user_followup(self.path); self.complete_source()
        write_text(self.runtime / "PAUSE", "user-owned")
        self.assertEqual(self.engine.step()["status"], "paused")
        self.assertNotIn("user_followup", self.state_now()["task"])
        self.providers.code.assert_not_called()

    def test_three_failed_invocations_exhaust_without_timeout_replanning_or_a_fourth_call(self):
        self.consume()
        self.providers.code.side_effect = RuntimeError("fixture failed native call")
        with patch.object(self.engine, "_recover_code_timeout", side_effect=AssertionError("follow-up must not acquire another REPLAN window")):
            for _ in range(3):
                result = self.engine.step()
            self.assertEqual(result["phase"], "BLOCKED")
            self.assertEqual(self.state_now()["task"]["user_followup"]["status"], "exhausted")
            self.engine.step()
        self.assertEqual(self.providers.code.call_count, 3)
        self.assertEqual(len(self.state_now()["task"]["user_followup_attempts"]), 3)

    def test_completed_authorization_stays_task_scoped_without_new_followup_turns(self):
        self.consume(); self.providers.code.side_effect = self.result(True)
        state = self.state_now(); self.engine._code(state, self.work)
        task = state["task"]
        self.engine._providers_injected = False
        with patch("unattended.providers.AgentProviders") as provider:
            self.engine._user_repair_providers(task)
        config = provider.call_args.args[0]
        self.assertEqual(config["_user_followup_profile"], task["user_followup"])
        self.assertEqual(config["_user_repair_profile"], task["user_repair"])
        state["phase"] = "CODE"
        op = self.engine._new_operation(state, "code")
        self.assertEqual(op["user_followup_id"], self.request["id"])
        self.assertNotIn("user_followup_turn", op)
        self.assertEqual(len(task["user_followup_attempts"]), 1)
        self.assertEqual(self.engine._task_checks(task)[-1]["id"], "ue-full-flow-repair")

    def test_state_first_consumption_recovers_when_separate_queue_write_was_interrupted(self):
        self.engine.queue_user_followup(self.path); self.complete_source()
        with patch.object(self.engine, "_followup_queue_status", side_effect=OSError("queue write interrupted")):
            with self.assertRaises(OSError):
                self.engine._consume_user_followup(self.state_now(), self.work)
        state = self.state_now()
        self.assertEqual(state["phase"], "CODE")
        self.assertEqual(read_json(self.runtime / "user-followups.json")["requests"][0]["status"], "queued")
        self.providers.code.side_effect = self.result(True)
        self.engine._code(state, self.work)
        self.assertEqual(state["phase"], "CHECK")
        self.assertEqual(state["task"]["feedback"].count(self.request["user_instruction"]), 1)
        self.assertEqual(read_json(self.runtime / "user-followups.json")["requests"][0]["status"], "completed")

    def test_activation_audit_before_state_save_is_immutable_and_resumable(self):
        self.engine.queue_user_followup(self.path); self.complete_source()
        with patch.object(self.engine, "_save", side_effect=OSError("activation before state save")):
            with self.assertRaises(OSError): self.engine._consume_user_followup(self.state_now(), self.work)
        path = self.path.parent / "activation.json"; before = path.read_bytes()
        self.assertEqual(self.state_now()["phase"], "CHECK")
        self.engine._consume_user_followup(self.state_now(), self.work)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.state_now()["phase"], "CODE")

    def test_interrupted_source_preserves_failed_bytes_and_only_claims_verified_opening(self):
        state = self.interrupted_source()
        raw = {name: (Path(self.op["output_dir"]) / name).read_bytes() for name in ("stdout.log", "process.json")}
        request_bytes = self.path.read_bytes()
        with patch("unattended.full_flow_native.parse_full_flow_native", side_effect=AssertionError("incomplete stream must not be terminal-parsed")):
            self.engine._consume_user_followup(state, self.work)
        proof = read_json(self.path.parent / "source-interruption.json")
        self.assertEqual(proof["source_status"], "interrupted")
        self.assertTrue(proof["opening_verified"])
        self.assertFalse(proof["model_verified"]); self.assertFalse(proof["completed"])
        self.assertEqual(proof["candidate_head_sha"], "c" * 40)
        self.assertEqual(proof["original_process_model_selection"], {"model_verified": False})
        self.assertEqual(state["phase"], "CODE")
        self.assertEqual(state["task"]["user_followup_attempts"], [])
        self.assertEqual(state["task"]["user_followup"]["max_turns"], 3)
        self.assertEqual(state["task"]["reworks"], 10)
        self.assertEqual(self.path.read_bytes(), request_bytes)
        for name, value in raw.items(): self.assertEqual((Path(self.op["output_dir"]) / name).read_bytes(), value)
        self.assertEqual(followup.validate_followup_profile(state["task"], self.runtime)["source_status"], "interrupted")
        self.providers.code.side_effect = self.result(True)
        self.engine._code(self.state_now(), self.work)
        self.assertEqual(self.state_now()["phase"], "CHECK")
        self.assertEqual(self.state_now()["task"]["user_followup_attempts"][0]["turn"], 1)

    def test_interrupted_source_rejects_missing_recovery_wrong_op_and_dirty_candidate(self):
        state = self.interrupted_source()
        for field, value in (("decision", "replan"), ("operation_sha256", "0" * 64)):
            changed = deepcopy(state); changed["task"]["code_timeout_recoveries"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "exact native timeout"):
                self.engine._consume_user_followup(changed, self.work)
        with patch("unattended.user_followup.gitops.clean", return_value=False):
            with self.assertRaisesRegex(ValueError, "clean task candidate"):
                self.engine._consume_user_followup(state, self.work)
        self.assertFalse((self.path.parent / "source-interruption.json").exists())

    def test_interrupted_source_checkpoint_and_old_raw_remain_bound_after_activation(self):
        state = self.interrupted_source(); self.engine._consume_user_followup(state, self.work)
        process_path = Path(self.op["output_dir"]) / "process.json"
        process = read_json(process_path); process["timed_out"] = False; write_json(process_path, process)
        with self.assertRaisesRegex(ValueError, "interrupted source evidence"):
            followup.validate_followup_profile(state["task"], self.runtime)

    def test_interrupted_opening_wrong_session_or_model_never_dispatches(self):
        state = self.interrupted_source(); out = Path(self.op["output_dir"])
        raw = (out / "stdout.log").read_text(encoding="utf-8")
        for before, after in ((TARGET, "00000000-0000-0000-0000-000000000000"), (followup.MODEL, "claude-sonnet-5-5")):
            write_text(out / "stdout.log", raw.replace(before, after))
            with self.subTest(before=before), self.assertRaises(ValueError):
                self.engine._consume_user_followup(deepcopy(state), self.work)
        self.providers.code.assert_not_called()

