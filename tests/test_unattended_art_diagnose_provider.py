"""A diagnostic fork can inspect one art task without becoming its producer."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from tests import test_unattended_art_integration as fixtures
from tests.test_unattended_providers import write_events
from unattended.config import ART_CODING_MODEL, ART_CODING_REASONING_EFFORT
from unattended.providers import (ART_DIAGNOSE_SCHEMA, AgentProviders, ProviderError,
                                  classify_transient_failure, read_session, validate_output)

SOURCE = "550e8400-e29b-41d4-a716-446655440000"
BRANCH = "550e8400-e29b-41d4-a716-446655440001"
HEAD = "a" * 40
RESULT = {"action": "continue", "diagnosis": "The local preview failed its mesh check.",
          "plan": "Inspect the evidence, then revise the allowed job and rerun the fixed preview.",
          "reason": "The fix stays within the approved batch and tool permissions.",
          "required_approvals": [], "questions": [], "additional_tripo_credits": 0, "head_sha": HEAD}


class ArtDiagnoseProviderTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.state["phase"] = "ART_DIAGNOSE"
        self.f.state["task"].update(kind="art", coding_model=ART_CODING_MODEL,
            coding_reasoning_effort=ART_CODING_REASONING_EFFORT, session_id=SOURCE,
            session_owner_task_id="TASK-ART-0001", candidate=HEAD,
            art_request={"config_file": str(self.f.batch),
                         "config_sha256": hashlib.sha256(self.f.batch.read_bytes()).hexdigest(),
                         "artifacts_dir": str(self.f.artifacts)})
        self.f.state["operation"].update(kind="art_diagnose", head_sha=HEAD, diagnosis_provider="claude",
            diagnosis_model=ART_CODING_MODEL, diagnosis_reasoning_effort=ART_CODING_REASONING_EFFORT)
        self.f.save_state()
        self.task_dir = self.f.runtime / "tasks" / "TASK-ART-0001"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "review-3.json").write_text('{"decision":"rework"}', encoding="utf-8")
        (self.f.runtime / "tasks" / "TASK-ART-OTHER").mkdir()
        self.provider = AgentProviders(self.f.config)
        authentication = patch("unattended.providers.claude_subscription_ready", return_value=True)
        self.auth = authentication.start()
        self.addCleanup(authentication.stop)
        launch = patch("unattended.providers.run_process")
        self.launch = launch.start()
        self.addCleanup(launch.stop)
        self.launch.side_effect = self.capture()

    def rows(self, *, branch=BRANCH, model=ART_CODING_MODEL, usage=True, result=None, error=False, extra=()):
        return [{"type": "system", "subtype": "init", "session_id": branch, "model": model},
                *extra,
                {"type": "result", "subtype": "error_during_execution" if error else "success",
                 "session_id": branch, "is_error": error, "result": "Diagnostic branch only.",
                 "structured_output": copy.deepcopy(RESULT if result is None else result),
                 **({"modelUsage": {model: {"inputTokens": 100, "outputTokens": 20}}} if usage else {})}]

    def capture(self, rows=None, *, exit_code=0, timeout=False):
        def launch(argv, **kwargs):
            out = kwargs["output_dir"]
            write_events(out / "stdout.log", self.rows() if rows is None else rows)
            (out / "stderr.log").write_text("", encoding="utf-8")
            return ProcessResult(exit_code, timeout, False, out / "stdout.log", out / "stderr.log")
        return launch

    def invoke(self):
        return self.provider.art_diagnose("Diagnose this art task.", self.f.workspace, self.f.out, 120,
                                          session_id=SOURCE)

    def next_out(self, name):
        self.f.out = self.f.runtime / "runs" / name
        self.f.state["operation"]["output_dir"] = str(self.f.out)
        self.f.save_state()

    def update_batch(self, overrides):
        self.f.batch.write_text(json.dumps({**self.f.batch_config, **overrides}), encoding="utf-8")
        self.f.state["task"]["art_request"]["config_sha256"] = hashlib.sha256(self.f.batch.read_bytes()).hexdigest()
        self.f.save_state()

    def test_schema_rejects_unscoped_actions_malformed_budget_and_ambiguous_outputs(self):
        self.assertEqual(validate_output("art_diagnose", RESULT), RESULT)
        for valid in ({"action": "needs_user", "questions": ["Which approved reference should apply?"]},
                      {"action": "needs_user", "required_approvals": ["style_reference"]},
                      {"action": "blocked"}):
            validate_output("art_diagnose", {**RESULT, **valid})
        cases = [{"extra": True}, {"action": "task"}, {"diagnosis": " "}, {"plan": ""}, {"reason": " "},
                 {"required_approvals": ["shell"]}, {"required_approvals": ["tool_permissions"]},
                 {"questions": ["Authorize changes?"]}, {"questions": [""]}, {"questions": "none"},
                 {"action": "needs_user"}, {"additional_tripo_credits": True},
                 {"additional_tripo_credits": -1}, {"additional_tripo_credits": 1.5},
                 {"head_sha": "abcd"}, {"head_sha": "a" * 41}]
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ProviderError):
                validate_output("art_diagnose", {**RESULT, **invalid})

    def test_native_fork_is_read_only_pinned_and_keeps_the_production_session_untouched(self):
        state_before = (self.f.runtime / "state.json").read_bytes()
        config_before = copy.deepcopy(self.f.config)
        self.assertEqual(self.invoke(), RESULT)
        argv = self.launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--resume") + 1], SOURCE)
        self.assertIn("--fork-session", argv)
        self.assertEqual(argv[argv.index("--model") + 1], ART_CODING_MODEL)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertFalse(any(part.startswith("Bash(") for part in argv))
        self.assertNotIn("--session-id", argv)
        policy = json.loads(argv[argv.index("--settings") + 1])
        self.assertTrue(policy["disableAllHooks"])
        permissions = policy["permissions"]
        self.assertTrue(permissions["blockReadsOutsideWorkingDirectories"])
        self.assertEqual(set(permissions["additionalDirectories"]),
                         {str(p.resolve()) for p in (self.f.workspace, self.f.artifacts, self.task_dir)})
        for directory in (self.f.workspace, self.f.artifacts, self.task_dir):
            pattern = directory.resolve().as_posix()
            if directory.drive:
                pattern = "/" + directory.drive[0].lower() + pattern[2:]
            self.assertIn(f"Edit(/{pattern}/**)", permissions["deny"])
        receipt = json.loads((self.f.out / "process.json").read_text())
        self.assertEqual(receipt["kind"], "art_diagnose")
        self.assertEqual(receipt["diagnosis_provider"], "claude")
        self.assertNotIn("planning_provider", receipt)
        self.assertNotIn("coding_provider", receipt)
        self.assertEqual(receipt["requested_session"], SOURCE)
        self.assertEqual(receipt["diagnosis_session"], BRANCH)
        self.assertEqual(receipt["task_id"], "TASK-ART-0001")
        self.assertEqual(receipt["head_sha"], HEAD)
        self.assertTrue(receipt["model_selection"]["model_verified"])
        self.assertEqual(read_session(self.f.out), BRANCH)
        self.assertEqual((self.f.runtime / "state.json").read_bytes(), state_before)
        self.assertEqual(self.f.config, config_before)
        self.assertEqual(json.loads((self.f.out / "result.json").read_text()), RESULT)

    def test_receipt_recovers_after_missing_result_and_sidecar_without_a_second_call_or_log_write(self):
        self.invoke()
        (self.f.out / "result.json").unlink()
        (self.f.out / "model-selection.json").unlink()
        before = {name: ((self.f.out / name).read_bytes(), (self.f.out / name).stat().st_mtime_ns)
                  for name in ("process.json", "stdout.log", "schema.json")}
        self.assertEqual(self.provider.recover("art_diagnose", self.f.out), RESULT)
        self.assertEqual(before, {name: ((self.f.out / name).read_bytes(), (self.f.out / name).stat().st_mtime_ns)
                                 for name in before})
        self.assertEqual(self.launch.call_count, 1)
        with self.assertRaisesRegex(ProviderError, "already used"):
            self.invoke()
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(before["schema.json"][1], (self.f.out / "schema.json").stat().st_mtime_ns)

    def test_wrong_model_missing_usage_or_nonforked_native_session_cannot_complete(self):
        for i, rows in enumerate((self.rows(model="claude-sonnet-5-5"), self.rows(usage=False),
                                  self.rows(branch=SOURCE), self.rows(branch=SOURCE.upper()))):
            self.next_out(f"diagnose-invalid-{i}")
            self.launch.side_effect = self.capture(rows)
            with self.subTest(i=i), self.assertRaises(ProviderError) as caught:
                self.invoke()
            self.assertIsNone(caught.exception.session_id)
            self.assertFalse((self.f.out / "result.json").exists())
            with self.assertRaises(ProviderError):
                self.provider.recover("art_diagnose", self.f.out)

    def test_recovery_rejects_changed_source_task_head_branch_and_model_sidecar(self):
        self.invoke()
        path = self.f.out / "process.json"
        original = json.loads(path.read_text())
        for change in ({"requested_session": BRANCH}, {"diagnosis_session": SOURCE},
                       {"session_id": SOURCE}, {"head_sha": "b" * 40}, {"task_id": "TASK-ART-OTHER"},
                       {"diagnosis_model": "claude-sonnet-5-5"}, {"strict_model_selection": False}):
            path.write_text(json.dumps({**original, **change}), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(ProviderError):
                self.provider.recover("art_diagnose", self.f.out)
        path.write_text(json.dumps(original), encoding="utf-8")
        sidecar = self.f.out / "model-selection.json"
        evidence = json.loads(sidecar.read_text())
        sidecar.write_text(json.dumps({**evidence, "diagnosis_session": SOURCE}), encoding="utf-8")
        with self.assertRaisesRegex(ProviderError, "model evidence"):
            self.provider.recover("art_diagnose", self.f.out)

    def test_returned_wrong_head_extra_field_and_changed_schema_are_not_recoverable(self):
        for i, value in enumerate(({**RESULT, "head_sha": "b" * 40}, {**RESULT, "session_id": BRANCH})):
            self.next_out(f"diagnose-invalid-output-{i}")
            self.launch.side_effect = self.capture(self.rows(result=value))
            with self.subTest(i=i), self.assertRaises(ProviderError):
                self.invoke()
        self.next_out("diagnose-wrong-schema")
        self.launch.side_effect = self.capture()
        self.invoke()
        (self.f.out / "schema.json").write_text(json.dumps({**ART_DIAGNOSE_SCHEMA, "additionalProperties": True}))
        with self.assertRaisesRegex(ProviderError, "schema"):
            self.provider.recover("art_diagnose", self.f.out)

    def test_wrong_phase_operation_task_workspace_and_source_fail_before_model(self):
        original = copy.deepcopy(self.f.state)
        for change in ("phase", "kind", "task", "out", "source", "owner", "model", "effort"):
            self.f.state = copy.deepcopy(original)
            if change == "phase": self.f.state["phase"] = "CODE"
            elif change == "kind": self.f.state["operation"]["kind"] = "code"
            elif change == "task": self.f.state["operation"]["task_id"] = "TASK-ART-OTHER"
            elif change == "out": self.f.state["operation"]["output_dir"] = str(self.f.out.parent / "foreign")
            elif change == "source": self.f.state["task"]["session_id"] = BRANCH
            elif change == "owner": self.f.state["task"]["session_owner_task_id"] = "TASK-ART-OTHER"
            elif change == "model": self.f.state["operation"]["diagnosis_model"] = "claude-sonnet-5-5"
            elif change == "effort": self.f.state["operation"]["diagnosis_reasoning_effort"] = "low"
            self.f.save_state()
            with self.subTest(change=change), self.assertRaises(ProviderError): self.invoke()
        self.f.state = original
        self.f.save_state()
        with self.assertRaises(ProviderError):
            self.provider.art_diagnose("Inspect", self.f.root, self.f.out, 120, session_id=SOURCE)
        self.launch.assert_not_called()

    def test_credentials_private_ledger_and_linked_task_evidence_are_never_granted(self):
        for change in ({"key_file": str(self.task_dir / "tripo.txt")},
                       {"private_state_dir": str(self.task_dir / "private")},
                       {"private_state_dir": str(self.f.workspace / "private")},
                       {"key_file": str(self.f.artifacts / "key.txt")}):
            self.update_batch(change)
            with self.subTest(change=change), self.assertRaisesRegex(ProviderError, "overlaps"):
                self.invoke()
        self.update_batch({})
        leaf = self.task_dir / "review-3.json"
        with patch("unattended.providers._reparse", side_effect=lambda path: path == leaf):
            with self.assertRaisesRegex(ProviderError, "linked diagnostic"):
                self.invoke()
        self.launch.assert_not_called()

    def test_changed_approved_config_and_missing_task_evidence_fail_closed(self):
        self.f.batch.write_text(json.dumps({**self.f.batch_config, "unexpected": True}), encoding="utf-8")
        with self.assertRaisesRegex(ProviderError, "queued version"): self.invoke()
        self.update_batch({})
        (self.task_dir / "review-3.json").unlink()
        self.task_dir.rmdir()
        with self.assertRaisesRegex(ProviderError, "missing"): self.invoke()
        self.launch.assert_not_called()

    def test_native_retry_metadata_is_classified_as_diagnosis_without_claiming_its_branch(self):
        rows = self.rows(error=True, extra=(
            {"type": "assistant", "session_id": BRANCH, "parent_tool_use_id": None, "error": "overloaded"},))
        self.launch.side_effect = self.capture(rows, exit_code=1)
        with self.assertRaises(ProviderError) as caught: self.invoke()
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.retry_reason, "server_unavailable")
        self.assertIsNone(caught.exception.session_id)
        self.assertEqual(classify_transient_failure("art_diagnose", self.f.out), {"reason": "server_unavailable"})
        self.assertIsNone(self.provider.recover("art_diagnose", self.f.out))
        self.assertEqual(self.f.state["task"]["session_id"], SOURCE)

    def test_quota_wait_preserves_fork_identity_but_timeout_is_not_reclassified(self):
        rows = self.rows(error=True, extra=(
            {"type": "rate_limit_event", "session_id": BRANCH, "rate_limit_info": {"status": "rejected"}},))
        self.launch.side_effect = self.capture(rows, exit_code=1)
        with self.assertRaises(ProviderError) as caught: self.invoke()
        self.assertEqual(caught.exception.retry_reason, "claude_subscription_limit")
        self.next_out("diagnose-timeout")
        self.launch.side_effect = self.capture(timeout=True)
        with self.assertRaises(ProviderError) as caught: self.invoke()
        self.assertTrue(caught.exception.interrupted)
        self.assertIsNone(caught.exception.session_id)
        self.assertIsNone(classify_transient_failure("art_diagnose", self.f.out))


if __name__ == "__main__":
    unittest.main()
