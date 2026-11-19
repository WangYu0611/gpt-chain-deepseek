"""Native API billing failure can continue through the saved Pro subscription."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from tests.test_unattended_providers import PLAN
from unattended import providers


SESSION = "550e8400-e29b-41d4-a716-446655440000"
MODEL = "claude-fable-5-2"


def billing(*, session=SESSION, model=MODEL):
    return [{"type": "system", "subtype": "init", "session_id": session, "model": model},
            {"type": "assistant", "session_id": session, "error": "billing_error"},
            {"type": "result", "subtype": "success", "session_id": session, "is_error": True,
             "api_error_origin": "first_party", "api_error_status": 400, "terminal_reason": "api_error",
             "result": "Credit balance is too low", "modelUsage": {model: {}}}]


def completed(*, session=SESSION, model=MODEL, structured=None):
    result = {"type": "result", "subtype": "success", "session_id": session, "is_error": False,
              "result": "Continued from the existing task checkpoint.", "modelUsage": {model: {}}}
    if structured is not None:
        result["structured_output"] = structured
    return [{"type": "system", "subtype": "init", "session_id": session, "model": model}, result]


def capture(rows, code, kwargs):
    out = kwargs["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    formatter = kwargs["stdout_record_filter"]
    (out / "stdout.log").write_bytes(b"".join(formatter((json.dumps(row) + "\n").encode()) for row in rows))
    (out / "stderr.log").write_text("", encoding="utf-8")
    return ProcessResult(code, False, False, out / "stdout.log", out / "stderr.log")


class ClaudeBillingFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / "code"
        central = self.root / "secrets.local.json"
        central.write_text(json.dumps({"ANTHROPIC_API_KEY": "private-anthropic-test-key"}), encoding="utf-8")
        route = self.root / "network.local.json"
        route.write_text(json.dumps({"proxy_url": "http://127.0.0.1:17893", "region": "SG"}), encoding="utf-8")
        self.settings = {"command": ["claude.exe"], "auth_mode": "api_key", "subscription_fallback": True,
                         "model": MODEL, "reasoning_effort": "high"}
        self.config = {"coding_provider": "claude", "planning_provider": "claude", "claude": self.settings,
                       "secrets_file": str(central), "network_file": str(route)}
        self.provider = providers.AgentProviders(self.config)
        self.addCleanup(patch.stopall)
        self.auth = patch.object(providers, "claude_subscription_ready", return_value=True).start()
        self.launch = patch.object(providers, "run_process").start()

    def launches(self, *attempts):
        queue = list(attempts)
        self.launch.side_effect = lambda *args, **kwargs: capture(*queue.pop(0), kwargs)

    def test_code_billing_fallback_preserves_session_model_permissions_proxy_and_offline_recovery(self):
        rows = billing()
        rows.insert(1, {"type": "assistant", "session_id": SESSION, "message": {"content": [
            {"type": "tool_use", "id": "tool-existing-edit", "name": "Edit", "input": {"content": "existing edit"}}]}})
        self.launches((rows, 1), (completed(), 0))
        result = self.provider.code("Continue the issued task.", self.root, self.out, 120)
        self.assertTrue(result["completed"])
        first, second = self.launch.call_args_list
        original, fallback = first.args[0], second.args[0]
        self.assertEqual(fallback[fallback.index("--resume") + 1], SESSION)
        self.assertEqual(fallback[fallback.index("--model") + 1], MODEL)
        self.assertEqual(fallback[fallback.index("--effort") + 1], "high")
        policy = json.loads(fallback[fallback.index("--settings") + 1])
        self.assertEqual(policy["forceLoginMethod"], "claudeai")
        original_policy = json.loads(original[original.index("--settings") + 1])
        self.assertEqual(policy["permissions"], original_policy["permissions"])
        self.assertEqual([part for part in fallback if part.startswith("Bash(")],
                         [part for part in original if part.startswith("Bash(")])
        self.assertIn("ANTHROPIC_API_KEY", first.kwargs["environment"])
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            self.assertNotIn(name, second.kwargs["environment"])
        self.assertEqual(second.kwargs["environment"]["HTTPS_PROXY"], "http://127.0.0.1:17893")
        self.assertIn("从中断点继续", second.kwargs["stdin"].decode())
        child = self.out / "claude-subscription"
        self.assertEqual(providers.claude_result_directory("code", self.out), child)
        primary = json.loads((self.out / "process.json").read_text())
        receipt = json.loads((child / "process.json").read_text())
        self.assertEqual(primary["claude_auth_mode"], "api_key")
        self.assertEqual(receipt["claude_auth_mode"], "subscription")
        self.assertEqual(receipt["claude_fallback_source"]["stdout_sha256"], hashlib.sha256((self.out / "stdout.log").read_bytes()).hexdigest())
        self.assertEqual(providers.AgentProviders({}).recover("code", self.out), result)
        self.assertEqual(providers.read_session(self.out), SESSION)
        self.assertEqual(self.launch.call_count, 2)

    def test_default_and_critical_readonly_plans_keep_the_requested_fable_model_and_schema(self):
        for label, options in (("routine", {}), ("critical", {"model": MODEL, "reasoning_effort": "high"})):
            out = self.root / label
            self.launches((billing(), 1), (completed(structured=PLAN), 0))
            self.assertEqual(self.provider.plan("Plan the task.", self.root, out, 120, tiered=False, **options), PLAN)
            child = out / "claude-subscription"
            self.assertEqual((child / "schema.json").read_bytes(), (out / "schema.json").read_bytes())
            argv = self.launch.call_args.args[0]
            self.assertEqual(argv[argv.index("--model") + 1], MODEL)
            self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
            self.assertEqual(providers.AgentProviders({}).recover("plan", out), PLAN)

    def test_successful_api_result_is_recovered_without_fallback_even_when_text_mentions_credit(self):
        rows = completed()
        rows[-1]["result"] = "Credit balance is too low"
        self.launches((rows, 0))
        self.assertTrue(self.provider.code("Task", self.root, self.out, 120)["completed"])
        self.assertEqual(self.launch.call_count, 1)
        self.auth.assert_not_called()
        self.assertFalse((self.out / "claude-subscription").exists())

    def test_only_exact_firstparty_native_billing_metadata_authorizes_fallback(self):
        variants = []
        for key, value in (("api_error_origin", "tool"), ("api_error_status", 401),
                           ("terminal_reason", "unknown"), ("result", "Credit balance too low")):
            rows = billing()
            rows[-1][key] = value
            variants.append((key, rows))
        rows = billing()
        rows[1]["parent_tool_use_id"] = "tool-result-spoof"
        variants.append(("tool-spoof", rows))
        rows = billing()
        rows[1]["error"] = "authentication_error"
        variants.append(("other-error", rows))
        for label, rows in variants:
            self.out = self.root / label
            self.launch.reset_mock()
            self.auth.reset_mock()
            self.launches((rows, 1))
            with self.subTest(label=label), self.assertRaises(providers.ProviderError):
                self.provider.code("Task", self.root, self.out, 120)
            self.assertEqual(self.launch.call_count, 1)
            self.auth.assert_not_called()

    def test_older_billing_terminal_without_status_is_supported_but_custom_gateway_is_excluded(self):
        rows = billing()
        rows[-1].pop("api_error_status")
        self.launches((rows, 1), (completed(), 0))
        self.assertTrue(self.provider.code("Task", self.root, self.out, 120)["completed"])
        central = Path(self.config["secrets_file"])
        settings = json.loads(central.read_text())
        settings["ANTHROPIC_BASE_URL"] = "https://custom.example/v1"
        central.write_text(json.dumps(settings), encoding="utf-8")
        self.out = self.root / "gateway"
        self.launch.reset_mock()
        self.launches((billing(), 1))
        with self.assertRaises(providers.ProviderError):
            self.provider.code("Task", self.root, self.out, 120)
        self.assertEqual(self.launch.call_count, 1)

    def test_changed_parent_receipt_or_child_model_cannot_supply_recovered_delivery(self):
        self.launches((billing(), 1), (completed(), 0))
        self.provider.code("Task", self.root, self.out, 120)
        path = self.out / "process.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaises(providers.ProviderError):
            self.provider.recover("code", self.out)
        path.write_bytes(original)
        child = self.out / "claude-subscription/process.json"
        receipt = json.loads(child.read_text())
        receipt["coding_model"] = "another-model"
        child.write_text(json.dumps(receipt))
        with self.assertRaises(providers.ProviderError):
            self.provider.recover("code", self.out)

    def test_subscription_native_receipt_must_verify_the_same_requested_model_for_plan_and_code(self):
        for kind in ("code", "plan"):
            self.out = self.root / ("wrong-native-" + kind)
            rows = completed(model="another-model", structured=PLAN if kind == "plan" else None)
            self.launches((billing(), 1), (rows, 0))
            with self.subTest(kind=kind), self.assertRaises(providers.ProviderError):
                getattr(self.provider, kind)("Task", self.root, self.out, 120)
            with self.assertRaises(providers.ProviderError):
                self.provider.recover(kind, self.out)

    def test_subscription_failure_is_classified_from_the_bound_child_and_never_replayed_in_recovery(self):
        rows = completed()
        rows.insert(1, {"type": "assistant", "session_id": SESSION, "error": "overloaded"})
        rows[-1].update(is_error=True, subtype="error_during_execution")
        self.launches((billing(), 1), (rows, 1))
        with self.assertRaises(providers.ProviderError) as caught:
            self.provider.code("Task", self.root, self.out, 120)
        self.assertTrue(caught.exception.transient)
        self.assertEqual(providers.classify_transient_failure("code", self.out), {"reason": "server_unavailable"})
        self.assertIsNone(self.provider.recover("code", self.out))
        self.assertEqual(self.launch.call_count, 2)

    def test_subscription_mode_keeps_the_fallback_flag_inactive(self):
        self.settings["auth_mode"] = "subscription"
        self.launches((completed(), 0))
        self.assertTrue(self.provider.code("Task", self.root, self.out, 120)["completed"])
        self.assertEqual(self.launch.call_count, 1)
        self.assertNotIn("ANTHROPIC_API_KEY", self.launch.call_args.kwargs["environment"])

    def test_managed_code_that_committed_before_billing_is_left_for_controller_reconciliation(self):
        runtime = self.root / "runtime"
        work = runtime / "workspace"
        (work / ".git").mkdir(parents=True)
        self.out = runtime / "runs" / "code-managed"
        self.out.parent.mkdir()
        (runtime / "state.json").write_text(json.dumps({"operation": {"kind": "code", "output_dir": str(self.out)}}),
                                            encoding="utf-8")
        self.config["runtime_dir"] = str(runtime)
        self.launches((billing(), 1))
        with patch("unattended.gitops.head", side_effect=["a" * 40, "b" * 40]):
            with self.assertRaises(providers.ProviderError) as caught:
                self.provider.code("Task", work, self.out, 120)
        self.assertEqual(caught.exception.session_id, SESSION)
        self.assertEqual(self.launch.call_count, 1)
        self.auth.assert_not_called()
        self.assertFalse((self.out / "claude-subscription").exists())
        self.assertEqual(providers.read_session(self.out), SESSION)

    def test_managed_repair_fallback_uses_logical_operation_and_verifies_child_native_model_receipt(self):
        from tests.test_unattended_claude_user_repair import ClaudeUserRepairTests, MODEL as REPAIR_MODEL, SESSION as REPAIR_SESSION, stream
        fixture = ClaudeUserRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config["claude"].update(auth_mode="api_key", subscription_fallback=True)
        fixture.config.update(secrets_file=self.config["secrets_file"], network_file=self.config["network_file"])
        failed = billing(session=REPAIR_SESSION, model=REPAIR_MODEL)
        failed.insert(0, {"type": "system", "subtype": "task_notification", "session_id": REPAIR_SESSION})
        queue = [(failed, 1), (stream(), 0)]
        fixture.launch.side_effect = lambda *args, **kwargs: capture(*queue.pop(0), kwargs)
        result = fixture.invoke()
        self.assertEqual(result["model_selection"]["turn_count"], 2)
        self.assertTrue(result["model_selection"]["model_verified"])
        self.assertEqual(fixture.provider.recover("code", fixture.out), result)
        receipt = json.loads((fixture.out / "claude-subscription/process.json").read_text())
        self.assertEqual(receipt["output_dir"], str(fixture.out))
        self.assertEqual(receipt["requested_session"], REPAIR_SESSION)

    def test_managed_art_diagnosis_fallback_preserves_source_fork_and_readonly_permissions(self):
        from tests.test_unattended_art_diagnose_provider import ArtDiagnoseProviderTests
        fixture = ArtDiagnoseProviderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        settings = fixture.f.config["claude"]
        settings.update(auth_mode="api_key", subscription_fallback=True)
        fixture.f.config.update(secrets_file=self.config["secrets_file"], network_file=self.config["network_file"])
        rows = fixture.rows()
        branch = rows[0]["session_id"]
        model = rows[0]["model"]
        queue = [(billing(session=branch, model=model), 1), (rows, 0)]
        fixture.launch.side_effect = lambda *args, **kwargs: capture(*queue.pop(0), kwargs)
        result = fixture.invoke()
        self.assertEqual(fixture.provider.recover("art_diagnose", fixture.f.out), result)
        argv = fixture.launch.call_args.args[0]
        self.assertIn("--fork-session", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")

    def test_managed_background_billing_uses_the_last_verified_native_turn(self):
        from tests.test_unattended_claude_user_repair import ClaudeUserRepairTests, MODEL as REPAIR_MODEL, SESSION as REPAIR_SESSION, stream
        fixture = ClaudeUserRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config["claude"].update(auth_mode="api_key", subscription_fallback=True)
        fixture.config.update(secrets_file=self.config["secrets_file"], network_file=self.config["network_file"])
        prefix = completed(session=REPAIR_SESSION, model=REPAIR_MODEL)
        prefix[-1]["result_index"] = 0
        failed = billing(session=REPAIR_SESSION, model=REPAIR_MODEL)
        failed[-1].update(result_index=1, origin={"kind": "task-notification", "producer": "session-task", "runId": "billing-notice"})
        rows = prefix + [{"type": "system", "subtype": "task_notification", "session_id": REPAIR_SESSION}] + failed
        queue = [(rows, 1), (stream(), 0)]
        fixture.launch.side_effect = lambda *args, **kwargs: capture(*queue.pop(0), kwargs)
        self.assertTrue(fixture.invoke()["model_selection"]["model_verified"])
        self.assertEqual(fixture.launch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
