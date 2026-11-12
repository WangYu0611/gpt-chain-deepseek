from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from unattended.providers import (AgentProviders, ProviderError, classify_transient_failure,
                                  parse_codex_events, read_session, validate_output)


PLAN = {"action": "task", "title": "Implement a feature", "body": "Make the requested change.",
        "acceptance_criteria": ["The required check passes"], "reason": "Next step"}
REVIEW = {"decision": "accepted", "summary": "Checked the change", "feedback": "", "head_sha": "a" * 40}
CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "0199abc-def"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "result"}},
    {"type": "turn.completed", "usage": {}},
]
LARGE_CODEX_TOOL_EVENTS = [
    {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "a" * (590 * 1024)}},
    {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "b" * (72 * 1024)}},
]
DSH_EVENTS = [
    {"type": "session", "sessionId": "session-example"},
    {"type": "status", "phase": "turn_start", "turn": 1},
    {"type": "tool_call", "name": "pwsh", "arguments": "git commit"},
    {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
    {"type": "final", "text": "Committed and verified."},
]


def write_events(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.providers = AgentProviders({"codex": {"command": ["codex.exe"]},
                                         "dsh": {"command": ["dsh.exe"], "verified_version": "0.2.1-rc.3"}})

    def fake_codex(self, argv, **kwargs):
        out = kwargs["output_dir"]
        write_events(out / "stdout.log", CODEX_EVENTS)
        (out / "stderr.log").write_text("", encoding="utf-8")
        value = REVIEW if "Review" in kwargs["stdin"].decode() else PLAN
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps(value), encoding="utf-8")
        return ProcessResult(0, False, False, out / "stdout.log", out / "stderr.log")

    def fake_dsh(self, argv, **kwargs):
        out = kwargs["output_dir"]
        write_events(out / "stdout.log", DSH_EVENTS)
        (out / "stderr.log").write_text("", encoding="utf-8")
        return ProcessResult(0, False, False, out / "stdout.log", out / "stderr.log")

    def fake_codex_failure(self, out, error, *, extra_events=(), timed_out=False):
        events = CODEX_EVENTS[:2] + list(extra_events) + [{"type": "turn.failed", "error": error}]
        write_events(out / "stdout.log", events)
        (out / "stderr.log").write_text("", encoding="utf-8")
        return ProcessResult(1, timed_out, False, out / "stdout.log", out / "stderr.log")

    @patch("unattended.providers.run_process")
    def test_plan_uses_read_only_noninteractive_cli_and_recovers_without_calling(self, process):
        process.side_effect = self.fake_codex
        out = self.root / "plan"
        result = self.providers.plan("Plan", self.root, out, 30)
        self.assertEqual(result, PLAN)
        argv = process.call_args.args[0]
        self.assertEqual(argv[:6], ["codex.exe", "--no-daemon", "-a", "never", "exec", "--sandbox"])
        self.assertIn("read-only", argv)
        self.assertIn("--output-schema", argv)
        self.assertEqual(argv[-1], "-")
        self.assertEqual(process.call_args.kwargs["stdin"], b"Plan")
        self.assertEqual(self.providers.recover("plan", out), PLAN)
        self.assertEqual(process.call_count, 1)
        self.assertTrue((out / "process.json").is_file())
        self.assertTrue((out / "result.json").is_file())

    @patch("unattended.providers.run_process")
    def test_opted_in_claude_plan_requests_exact_core_tests_and_recovers_legacy_plans(self, process):
        legacy = self.root / "legacy"
        process.side_effect = self.fake_codex
        self.providers.plan("Plan", self.root, legacy, 30)
        self.providers.config.update(coding_provider="claude", claude={"planned_core_tests": True})
        planned = PLAN | {"core_tests": ["ZhuGuoCity.M2.Save"]}

        def run(argv, **kwargs):
            result = self.fake_codex(argv, **kwargs)
            schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
            self.assertIn("core_tests", schema["required"])
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(planned), encoding="utf-8")
            return result

        process.side_effect = run
        out = self.root / "planned"
        self.assertEqual(self.providers.plan("Plan", self.root, out, 30), planned)
        self.assertEqual(self.providers.recover("plan", out), planned)
        self.assertEqual(self.providers.recover("plan", legacy), PLAN)
        for invalid in ([], ["ZhuGuoCity.M2.*"], ["ZhuGuoCity.M2.Save"] * 2):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ProviderError, "invalid core_tests"):
                validate_output("plan", planned | {"core_tests": invalid})
        with self.assertRaisesRegex(ProviderError, "empty core_tests"):
            validate_output("plan", planned | {"action": "complete"})

    @patch("unattended.providers.run_process")
    def test_review_is_structured_and_commit_bound(self, process):
        process.side_effect = self.fake_codex
        self.assertEqual(self.providers.review("Review", self.root, self.root / "review", 30), REVIEW)

    @patch("unattended.providers.run_process")
    def test_local_secrets_are_only_passed_to_the_matching_provider(self, process):
        self.providers.config["config_path"] = str(self.root / "unattended.local.json")
        self.providers.config["codex"]["auth_mode"] = "api_key"
        (self.root / "secrets.local.json").write_text(json.dumps({
            "CODEX_API_KEY": "synthetic-codex-secret",
            "DEEPSEEK_API_KEY": "synthetic-deepseek-secret",
        }), encoding="utf-8")
        with patch.dict("os.environ", {"CODEX_API_KEY": "", "DEEPSEEK_API_KEY": ""}):
            process.side_effect = self.fake_codex
            self.providers.plan("Plan", self.root, self.root / "plan", 30)
            codex = process.call_args.kwargs
            self.assertEqual(codex["environment"]["CODEX_API_KEY"], "synthetic-codex-secret")
            self.assertNotIn("DEEPSEEK_API_KEY", codex["environment"])
            self.assertEqual(codex["secrets"], ["synthetic-codex-secret"])

            process.side_effect = self.fake_dsh
            self.providers.code("Code", self.root, self.root / "code", 30)
            dsh = process.call_args.kwargs
            self.assertEqual(dsh["environment"]["DEEPSEEK_API_KEY"], "synthetic-deepseek-secret")
            self.assertNotIn("CODEX_API_KEY", dsh["environment"])
            self.assertEqual(dsh["secrets"], ["synthetic-deepseek-secret"])

    @patch("unattended.providers._codex_api_key", return_value="synthetic-api-test-secret")
    @patch("unattended.providers.run_process")
    def test_api_auth_keeps_key_out_of_argv_and_forces_the_official_endpoint(self, process, key):
        self.providers.config["codex"]["auth_mode"] = "api_key"
        process.side_effect = self.fake_codex
        with patch.dict("os.environ", {"OPENAI_API_KEY": "unrelated-test-secret",
                                        "OPENAI_BASE_URL": "https://example.invalid"}):
            self.providers.review("Review", self.root, self.root / "api-review", 30)
        argv = process.call_args.args[0]
        environment = process.call_args.kwargs["environment"]
        self.assertIn('model_provider="unattended_openai_api"', argv)
        provider = next(arg for arg in argv if arg.startswith("model_providers.unattended_openai_api="))
        self.assertIn('base_url="https://api.openai.com/v1"', provider)
        self.assertIn('env_key="CODEX_API_KEY"', provider)
        self.assertIn("requires_openai_auth=false", provider)
        self.assertNotIn("OPENAI_BASE_URL", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(environment["CODEX_API_KEY"], key.return_value)
        self.assertNotIn(key.return_value, " ".join(argv))
        self.assertEqual(process.call_args.kwargs["secrets"], [key.return_value])

    @patch("unattended.providers._codex_api_key", return_value=None)
    @patch("unattended.providers.run_process")
    def test_api_auth_requires_key_instead_of_falling_back_to_saved_login(self, process, _key):
        self.providers.config["codex"]["auth_mode"] = "api_key"
        with self.assertRaisesRegex(ProviderError, "CODEX_API_KEY is required"):
            self.providers.plan("Plan", self.root, self.root / "missing-key", 30)
        process.assert_not_called()

    @patch("unattended.providers._codex_api_key", return_value="synthetic-api-test-secret")
    @patch("unattended.providers.subprocess.run")
    @patch("unattended.providers.run_process")
    def test_billing_failure_switches_to_chatgpt_and_recovers_same_stage(self, process, login, key):
        self.providers.config["codex"].update(auth_mode="api_key", subscription_fallback=True,
                                               model="gpt-6-astra", reasoning_effort="high")
        login.return_value = subprocess.CompletedProcess([], 0, b"", b"Logged in using ChatGPT\n")
        for index, (error, events) in enumerate((
                ({"status": 429, "code": "credit_balance_exhausted", "type": "insufficient_quota"}, ()),
                ({"message": "Quota exceeded. Check your plan and billing details."}, LARGE_CODEX_TOOL_EVENTS),
                ({"message": ('unexpected status 429 Too Many Requests: {"error": '
                              '{"code": "organization_spend_limit_exceeded"}}, url: https://api.openai.com/v1/responses')}, ()),
        )):
            with self.subTest(error=error):
                process.reset_mock()
                login.reset_mock()
                kind, prompt, expected = ("review", "Review", REVIEW) if index % 2 else ("plan", "Plan", PLAN)
                out = self.root / (kind + "-billing-" + str(index))
                process.side_effect = lambda argv, **kwargs: (
                    self.fake_codex(argv, **kwargs) if kwargs["output_dir"].parent == out else
                    self.fake_codex_failure(kwargs["output_dir"], error, extra_events=events))
                with patch.dict("os.environ", {"CODEX_API_KEY": key.return_value, "OPENAI_API_KEY": "unrelated-test-secret",
                                                "OPENAI_BASE_URL": "https://example.invalid"}):
                    actual = getattr(self.providers, kind)(prompt, self.root, out, 30)
                self.assertEqual(actual, expected)
                self.assertEqual(process.call_count, 2)
                login.assert_called_once()
                self.assertEqual(login.call_args.args[0], ["codex.exe", "--no-daemon", "login", "status"])
                primary, fallback = process.call_args_list
                self.assertEqual(primary.kwargs["environment"]["CODEX_API_KEY"], key.return_value)
                self.assertIn('model_provider="unattended_openai_api"', primary.args[0])
                self.assertEqual(primary.kwargs["stdin"], fallback.kwargs["stdin"])
                self.assertLessEqual(fallback.kwargs["timeout_seconds"], primary.kwargs["timeout_seconds"])
                self.assertEqual(fallback.kwargs["output_dir"], out / "codex-subscription")
                self.assertIn('forced_login_method="chatgpt"', fallback.args[0])
                self.assertIn('model_provider="openai"', fallback.args[0])
                self.assertIn("gpt-6-astra", fallback.args[0])
                self.assertIn('model_reasoning_effort="high"', fallback.args[0])
                for environment in (fallback.kwargs["environment"], login.call_args.kwargs["env"]):
                    self.assertTrue({"CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"}.isdisjoint(environment))
                (out / "result.json").unlink()  # Simulate a stop before publishing the aggregate result.
                self.assertEqual(self.providers.recover(kind, out), expected)
                self.assertEqual(process.call_count, 2)

    @patch("unattended.providers._codex_api_key", return_value="synthetic-api-test-secret")
    @patch("unattended.providers.subprocess.run")
    @patch("unattended.providers.run_process")
    def test_only_confirmed_terminal_api_billing_errors_switch_auth(self, process, login, _key):
        self.providers.config["codex"].update(auth_mode="api_key", subscription_fallback=True)
        cases = [
            ({"status": 429}, (), False, "rate_limited"),
            ({"status": 503}, (), False, "server_unavailable"),
            ({"status": 401, "code": "invalid_api_key"}, (), False, None),
            ({"status": 429, "code": "slow_down", "type": "insufficient_quota"}, (), False, "rate_limited"),
            ({"message": "your project quota may be exceeded"}, (), False, None),
            ({"status": 429}, ({"type": "item.completed", "item": {"text": "insufficient_quota"}},), False, "rate_limited"),
            ({"status": 429, "code": "insufficient_quota"}, (), True, None),
            ({"type": "insufficient_quota"}, [CODEX_EVENTS[-1], *LARGE_CODEX_TOOL_EVENTS], False, None),
        ]
        for index, (error, events, timed_out, retry_reason) in enumerate(cases):
            with self.subTest(error=error, timed_out=timed_out):
                process.reset_mock()
                process.side_effect = lambda _argv, **kwargs: self.fake_codex_failure(
                    kwargs["output_dir"], error, extra_events=events, timed_out=timed_out)
                out = self.root / f"not-billing-{index}"
                with self.assertRaises(ProviderError) as failure:
                    self.providers.plan("Plan", self.root, out, 30)
                self.assertEqual(failure.exception.retry_reason, retry_reason)
                self.assertFalse((out / "codex-subscription").exists())
                self.assertEqual(process.call_count, 1)
        process.reset_mock()
        process.side_effect = self.fake_codex
        self.assertEqual(self.providers.plan("Plan", self.root, self.root / "api-restored", 30), PLAN)
        self.assertEqual(process.call_count, 1)
        login.assert_not_called()

    @patch("unattended.providers._codex_api_key", return_value="synthetic-api-test-secret")
    @patch("unattended.providers.subprocess.run")
    @patch("unattended.providers.run_process")
    def test_subscription_preflight_and_nested_transient_failure(self, process, login, _key):
        self.providers.config["codex"].update(auth_mode="api_key", subscription_fallback=True)
        login.return_value = subprocess.CompletedProcess([], 0, b"Logged in using an API key\n", b"")
        process.side_effect = lambda _argv, **kwargs: self.fake_codex_failure(
            kwargs["output_dir"], {"type": "insufficient_quota"})
        with self.assertRaisesRegex(ProviderError, "requires a saved ChatGPT login"):
            self.providers.plan("Plan", self.root, self.root / "missing-subscription", 30)
        self.assertEqual(process.call_count, 1)

        process.reset_mock()
        login.return_value = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT\n", b"")
        out = self.root / "subscription-unavailable"
        process.side_effect = lambda _argv, **kwargs: self.fake_codex_failure(
            kwargs["output_dir"], {"type": "insufficient_quota"} if kwargs["output_dir"] == out else {"status": 503})
        with self.assertRaises(ProviderError) as failure:
            self.providers.review("Review", self.root, out, 30)
        self.assertEqual(failure.exception.retry_reason, "server_unavailable")
        self.assertEqual(classify_transient_failure("review", out), {"reason": "server_unavailable"})
        self.assertIsNone(self.providers.recover("review", out))
        self.assertEqual(process.call_count, 2)

    @patch("unattended.providers._codex_api_key", return_value="synthetic-api-test-secret")
    @patch("unattended.providers.subprocess.run")
    @patch("unattended.providers.run_process")
    def test_api_and_subscription_limits_propagate_wait_reason_and_durable_metadata(self, process, login, _key):
        self.providers.config["codex"].update(auth_mode="api_key", subscription_fallback=True)
        login.return_value = subprocess.CompletedProcess([], 0, b"Logged in using ChatGPT\n", b"")
        out = self.root / "both-limits"
        limit = {"message": "You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more credits or try again at Nov 11th, 2026 2:50 PM.",
                 "retry_after_seconds": 2700}
        process.side_effect = lambda _argv, **kwargs: self.fake_codex_failure(
            kwargs["output_dir"], {"message": "Quota exceeded. Check your plan and billing details."} if kwargs["output_dir"] == out else limit)
        with self.assertRaises(ProviderError) as failure:
            self.providers.review("Review", self.root, out, 30)
        self.assertTrue(failure.exception.transient)
        self.assertEqual(failure.exception.retry_reason, "codex_subscription_limit")
        self.assertEqual(failure.exception.retry_after_seconds, 2700)
        record = json.loads((out / "codex-subscription" / "process.json").read_text(encoding="utf-8"))
        self.assertTrue(record["transient"])
        self.assertEqual(record["retry_reason"], "codex_subscription_limit")
        self.assertEqual(record["retry_after_seconds"], 2700)
        self.assertEqual(classify_transient_failure("review", out), {"reason": "codex_subscription_limit", "retry_after_seconds": 2700})
        self.assertIsNone(self.providers.recover("review", out))
        self.assertEqual(process.call_count, 2)

    @patch("unattended.providers.run_process")
    def test_codex_model_and_effort_reach_both_planner_and_reviewer(self, process):
        self.providers.config["codex"].update(model="gpt-6-astra", reasoning_effort="high")
        process.side_effect = self.fake_codex
        for role, prompt in (("plan", "Plan"), ("review", "Review")):
            getattr(self.providers, role)(prompt, self.root, self.root / (role + "-astra"), 30)
            argv = process.call_args.args[0]
            self.assertEqual(argv[argv.index("--model") + 1], "gpt-6-astra")
            self.assertEqual(argv[argv.index("-c") + 1], 'model_reasoning_effort="high"')
            self.assertEqual(argv[-1], "-")

    @patch("unattended.providers.run_process")
    def test_dsh_max_thinking_and_transport_retry_coexist_in_last_overlay(self, process):
        self.providers.config["dsh"].update(model="deepseek-flash", reasoning_effort="max", request_retries=2)
        process.side_effect = self.fake_dsh
        out = self.root / "flash-max"
        self.providers.code("Implement", self.root, out, 30)
        policy = json.loads((out / "invocation-policy.yml").read_text(encoding="utf-8"))
        selection = next(x for x in policy if x["id"] == "agent-default-model")["config"]
        self.assertEqual(selection, {"provider": "deepseek-account", "model": "deepseek-flash", "reasoningEffort": "max"})
        account = [x for x in policy if x["id"] == "llm-deepseek-account"]
        self.assertEqual(len(account), 1)
        self.assertEqual(account[0]["config"]["thinking"], "enabled")
        self.assertEqual(account[0]["config"]["retryPolicy"]["maxRetries"], 2)
        argv = process.call_args.args[0]
        self.assertEqual(argv[argv.index("--patch") + 1], str(out / "invocation-policy.yml"))

    @patch("unattended.providers.run_process")
    def test_dsh_effort_without_retry_or_explicit_model_is_not_dropped(self, process):
        process.side_effect = self.fake_dsh
        for effort, thinking in (("max", "enabled"), ("off", "disabled")):
            self.providers.config["dsh"]["reasoning_effort"] = effort
            out = self.root / ("effort-" + effort)
            self.providers.code("Implement", self.root, out, 30)
            policy = json.loads((out / "invocation-policy.yml").read_text())
            self.assertEqual(next(x for x in policy if x["id"] == "agent-default-model")["config"], {"reasoningEffort": effort})
            self.assertEqual(next(x for x in policy if x["id"] == "llm-deepseek-account")["config"]["thinking"], thinking)

    @patch("unattended.providers.run_process")
    def test_invalid_model_settings_do_not_launch_or_silently_use_defaults(self, process):
        for role, effort in (("codex", "maximum"), ("codex", []), ("dsh", "medium"), ("dsh", True)):
            with self.subTest(role=role, effort=effort):
                self.providers.config[role]["reasoning_effort"] = effort
                with self.assertRaises(ProviderError):
                    if role == "codex":
                        self.providers.plan("Plan", self.root, self.root / "bad-effort", 30)
                    else:
                        self.providers.code("Implement", self.root, self.root / "bad-effort", 30)
                self.providers.config[role].pop("reasoning_effort")
        process.assert_not_called()

    @patch("unattended.providers.run_process")
    def test_role_specific_config_guidance_reaches_only_the_selected_role(self, process):
        self.providers.config["codex"].update(plan_instructions="Planner guidance", review_instructions="Reviewer guidance")
        process.side_effect = self.fake_codex
        self.providers.plan("Plan", self.root, self.root / "plan-guidance", 30)
        self.assertEqual(process.call_args.kwargs["stdin"], b"Plan\nPlanner guidance")
        self.providers.review("Review", self.root, self.root / "review-guidance", 30)
        self.assertEqual(process.call_args.kwargs["stdin"], b"Review\nReviewer guidance")

    @patch("unattended.providers.run_process")
    def test_coding_config_supplies_fixed_validation_bridge_instructions(self, process):
        self.providers.config["dsh"]["instructions"] = "Write a commit-bound validation request"
        process.side_effect = self.fake_dsh
        self.providers.code("Implement", self.root, self.root / "code-guidance", 30)
        self.assertEqual(process.call_args.kwargs["stdin"], b"Implement\nWrite a commit-bound validation request")

    @patch("unattended.providers.run_process")
    def test_transport_retries_are_explicit_bounded_and_do_not_retry_other_errors(self, process):
        self.providers.config["dsh"]["request_retries"] = 2
        process.side_effect = self.fake_dsh
        out = self.root / "request-retries"
        self.providers.code("Implement", self.root, out, 30)
        policy = json.loads((out / "invocation-policy.yml").read_text(encoding="utf-8"))
        self.assertFalse(next(x for x in policy if x["id"] == "llm-retry")["disabled"])
        retry = next(x for x in policy if x["id"] == "llm-deepseek-account")["config"]["retryPolicy"]
        self.assertEqual(retry["maxRetries"], 2)
        self.assertEqual(retry["retryableCodes"], ["TRANSPORT"])
        self.assertEqual(process.call_count, 1)

    @patch("unattended.providers.run_process")
    def test_dsh_runs_full_headless_tools_and_resumes_exact_session(self, process):
        process.side_effect = self.fake_dsh
        out = self.root / "code"
        result = self.providers.code("Implement and commit", self.root, out, 30, "session-example")
        argv = process.call_args.args[0]
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], "session-example")
        policy = json.loads((out / "invocation-policy.yml").read_text())
        self.assertEqual(policy[0]["config"]["mode"], "workspace-write")
        self.assertEqual(policy[1]["config"]["policy"], "never")
        self.assertEqual(result, {"session_id": "session-example", "text": "Committed and verified.", "completed": True})
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in out.iterdir()}
        self.assertEqual(self.providers.recover("code", out), result)
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in out.iterdir()})
        self.assertEqual(process.call_count, 1)

    @patch("unattended.providers.run_process")
    def test_timeout_keeps_session_and_process_facts(self, process):
        def timed_out(argv, **kwargs):
            out = kwargs["output_dir"]
            write_events(out / "stdout.log", DSH_EVENTS[:2])
            (out / "stderr.log").write_text("", encoding="utf-8")
            return ProcessResult(-1, True, False, out / "stdout.log", out / "stderr.log")
        process.side_effect = timed_out
        out = self.root / "timeout"
        with self.assertRaises(ProviderError) as caught:
            self.providers.code("Implement", self.root, out, 1)
        self.assertTrue(caught.exception.interrupted)
        self.assertEqual(caught.exception.session_id, "session-example")
        self.assertEqual(read_session(out), "session-example")
        self.assertIsNone(self.providers.recover("code", out))
        self.assertFalse((out / "result.json").exists())
        self.assertTrue(json.loads((out / "process.json").read_text())["timed_out"])

    @patch("unattended.providers.run_process")
    def test_failed_process_cannot_be_recovered_from_good_prose(self, process):
        def failure(argv, **kwargs):
            result = self.fake_dsh(argv, **kwargs)
            return ProcessResult(1, False, False, result.stdout_path, result.stderr_path)
        process.side_effect = failure
        out = self.root / "failed"
        with self.assertRaises(ProviderError):
            self.providers.code("Implement", self.root, out, 10)
        self.assertIsNone(self.providers.recover("code", out))

    @patch("unattended.providers.run_process")
    def test_used_invocation_directory_is_never_replayed(self, process):
        process.side_effect = self.fake_dsh
        out = self.root / "once"
        self.providers.code("Implement", self.root, out, 10)
        with self.assertRaises(ProviderError):
            self.providers.code("Implement", self.root, out, 10)
        self.assertEqual(process.call_count, 1)

    @patch("unattended.providers.run_process")
    def test_launch_failure_records_incomplete_operation(self, process):
        process.side_effect = OSError("not found")
        out = self.root / "launch-failure"
        with self.assertRaises(ProviderError):
            self.providers.code("Implement", self.root, out, 10)
        record = json.loads((out / "process.json").read_text())
        self.assertTrue(record["launch_or_capture_failed"])
        self.assertIsNone(self.providers.recover("code", out))

    @patch("unattended.providers.run_process")
    def test_metadata_noise_has_only_one_explicit_accepted_dialect(self, process):
        def metadata(argv, **kwargs):
            result = self.fake_dsh(argv, **kwargs)
            raw = result.stdout_path.read_text()
            first, rest = raw.split("\n", 1)
            notice = "[llm-deepseek-account] model listing from https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s)"
            result.stdout_path.write_text(first + "\n" + notice + "\n" + rest, encoding="utf-8")
            return result
        process.side_effect = metadata
        self.assertTrue(self.providers.code("Implement", self.root, self.root / "metadata", 10)["completed"])

    @patch("unattended.providers.run_process")
    def test_unknown_stdout_noise_rejects_completion(self, process):
        def noise(argv, **kwargs):
            result = self.fake_dsh(argv, **kwargs)
            with result.stdout_path.open("a", encoding="utf-8") as output:
                output.write("some unexpected log\n")
            return result
        process.side_effect = noise
        with self.assertRaises(ProviderError):
            self.providers.code("Implement", self.root, self.root / "noise", 10)

    @patch("unattended.providers.run_process")
    def test_dsh_session_mismatch_is_rejected(self, process):
        process.side_effect = self.fake_dsh
        with self.assertRaises(ProviderError):
            self.providers.code("Implement", self.root, self.root / "mismatch", 10, "other-session")

    def test_invalid_command_and_session_do_not_launch(self):
        for command in (["dsh.cmd"], "dsh", [], ["dsh", "\0"]):
            provider = AgentProviders({"dsh": {"command": command}})
            with self.subTest(command=command), self.assertRaises(ProviderError):
                provider.code("Implement", self.root, self.root / "bad", 10)
        with self.assertRaises(ProviderError):
            self.providers.code("Implement", self.root, self.root / "bad-session", 10, "../escape")

    def test_partial_stream_session_can_be_extracted_without_completion(self):
        out = self.root / "partial"
        out.mkdir()
        write_events(out / "stdout.log", DSH_EVENTS[:1])
        self.assertEqual(read_session(out), "session-example")
        self.assertIsNone(self.providers.recover("code", out))

    def test_codex_protocol_checks_turn_boundaries_and_failure(self):
        path = self.root / "events"
        write_events(path, CODEX_EVENTS)
        self.assertEqual(parse_codex_events(path, exit_code=0)["session_id"], "0199abc-def")
        variants = [CODEX_EVENTS[:-1], CODEX_EVENTS + [{"type": "item.completed"}],
                    CODEX_EVENTS[1:], CODEX_EVENTS[:1] + CODEX_EVENTS[2:],
                    CODEX_EVENTS[:2] + [{"type": "error", "message": "failed"}]]
        for index, rows in enumerate(variants):
            with self.subTest(index=index):
                write_events(path, rows)
                with self.assertRaises(ProviderError):
                    parse_codex_events(path, exit_code=0)
        write_events(path, CODEX_EVENTS)
        with self.assertRaises(ProviderError):
            parse_codex_events(path, exit_code=1)

    def test_codex_transport_reconnect_progress_requires_successful_complete_turn(self):
        path = self.root / "reconnecting-events"
        notices = [{"type": "error", "message": f"Reconnecting... {n}/5 (stream disconnected before completion: tls handshake eof)"}
                   for n in (2, 3)]
        rows = CODEX_EVENTS[:2] + notices + CODEX_EVENTS[2:]
        write_events(path, rows)
        result = parse_codex_events(path, exit_code=0)
        self.assertEqual(result["reconnect_notices"], 2)
        self.assertEqual(result["session_id"], "0199abc-def")
        for incomplete in (rows[:-1], rows[:2] + notices + [{"type": "turn.failed"}],
                           rows + notices[:1], rows[:2] + notices + [{"type": "error", "message": "Authentication failed"}] + rows[-1:]):
            with self.subTest(incomplete=incomplete):
                write_events(path, incomplete)
                with self.assertRaises(ProviderError):
                    parse_codex_events(path, exit_code=0)
        write_events(path, rows)
        with self.assertRaises(ProviderError):
            parse_codex_events(path, exit_code=1)

    def test_codex_does_not_ignore_unknown_or_malformed_error_events(self):
        path = self.root / "terminal-errors"
        for message in ("Reconnecting... 0/5 (stream disconnected before completion: tls handshake eof)",
                        "Reconnecting... 6/5 (stream disconnected before completion: tls handshake eof)",
                        "Reconnecting... 1/99 (stream disconnected before completion: tls handshake eof)",
                        "Reconnecting... 1/5 (authentication failed)",
                        "Reconnecting... 1/5 (stream disconnected before completion: eof)\nextra", None):
            with self.subTest(message=message):
                write_events(path, CODEX_EVENTS[:2] + [{"type": "error", "message": message}] + CODEX_EVENTS[2:])
                with self.assertRaises(ProviderError):
                    parse_codex_events(path, exit_code=0)

    def test_codex_request_timeout_reconnect_requires_successful_complete_turn(self):
        path = self.root / "request-timeout-events"
        notice = {"type": "error", "message": "Reconnecting... 2/5 (request timed out)"}
        rows = CODEX_EVENTS[:2] + [notice] + CODEX_EVENTS[2:]
        write_events(path, rows)
        self.assertEqual(parse_codex_events(path, exit_code=0)["reconnect_notices"], 1)
        rejected = (rows[:-1], rows[:-1] + [{"type": "turn.failed"}],
                    CODEX_EVENTS[:1] + [notice] + CODEX_EVENTS[1:], rows + [notice],
                    rows[:-1] + [{"type": "error", "message": "Authentication failed"}] + rows[-1:],
                    rows[:-1] + [{"type": "error", "message": "Unknown terminal error"}] + rows[-1:])
        for bad in rejected:
            with self.subTest(events=bad):
                write_events(path, bad)
                with self.assertRaises(ProviderError):
                    parse_codex_events(path, exit_code=0)
        write_events(path, rows)
        with self.assertRaises(ProviderError):
            parse_codex_events(path, exit_code=1)

    def test_codex_timeout_reconnect_does_not_allow_invalid_numbering_or_unknown_prose(self):
        path = self.root / "invalid-request-timeout-events"
        for message in ("Reconnecting... 0/5 (request timed out)",
                        "Reconnecting... 6/5 (request timed out)",
                        "Reconnecting... 1/11 (request timed out)",
                        "Reconnecting... 1/0 (request timed out)",
                        "Reconnecting... 1/05 (request timed out)",
                        "Reconnecting... 1/5 (request timed out)\nextra",
                        "Reconnecting... 1/5 (request timed out: unauthorized)",
                        "Reconnecting... 1/5 (request timeout)"):
            with self.subTest(message=message):
                write_events(path, CODEX_EVENTS[:2] + [{"type": "error", "message": message}] + CODEX_EVENTS[2:])
                with self.assertRaises(ProviderError):
                    parse_codex_events(path, exit_code=0)

    @patch("unattended.providers.run_process")
    def test_completed_request_timeout_plan_and_review_recover_without_model_calls(self, process):
        notice = {"type": "error", "message": "Reconnecting... 2/5 (request timed out)"}
        for kind, value in (("plan", PLAN), ("review", REVIEW)):
            with self.subTest(kind=kind):
                out = self.root / (kind + "-timeout-recovery")
                out.mkdir()
                write_events(out / "stdout.log", CODEX_EVENTS[:2] + [notice] + CODEX_EVENTS[2:])
                (out / "process.json").write_text(json.dumps({
                    "kind": kind, "exit_code": 0, "timed_out": False, "output_limit_exceeded": False}), encoding="utf-8")
                (out / "final.json").write_text(json.dumps(value), encoding="utf-8")
                before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in out.iterdir()}
                self.assertEqual(self.providers.recover(kind, out), value)
                self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in out.iterdir()})
        process.assert_not_called()

    def test_codex_network_wait_notice_requires_a_successful_complete_turn(self):
        path = self.root / "network-wait-events"
        wait = {"type": "error", "message": "Reconnecting... waiting for network (Connection failed: error sending request)"}
        numbered = [{"type": "error", "message": f"Reconnecting... {n}/5 (stream disconnected before completion: tls handshake eof)"}
                    for n in (2, 3, 4, 5)]
        rows = CODEX_EVENTS[:2] + numbered + [wait] * 3 + CODEX_EVENTS[2:]
        write_events(path, rows)
        self.assertEqual(parse_codex_events(path, exit_code=0)["reconnect_notices"], 7)
        rejected = (rows[:-1], rows[:-1] + [{"type": "turn.failed"}],
                    rows[:-1] + [{"type": "error", "message": "Authentication failed"}] + rows[-1:],
                    CODEX_EVENTS[:1] + [wait] + CODEX_EVENTS[1:], rows + [wait])
        for bad in rejected:
            with self.subTest(events=bad):
                write_events(path, bad)
                with self.assertRaises(ProviderError):
                    parse_codex_events(path, exit_code=0)
        write_events(path, rows)
        with self.assertRaises(ProviderError):
            parse_codex_events(path, exit_code=1)

    def test_codex_network_wait_does_not_allow_arbitrary_error_messages(self):
        path = self.root / "unknown-network-errors"
        for message in ("Reconnecting... waiting for network (Authentication failed)",
                        "Reconnecting... waiting for network (Connection failed: other error)",
                        "Reconnecting... waiting for network (Connection failed: error sending request)\nextra",
                        "Reconnecting... waiting for network", "Reconnecting... retrying unknown failure"):
            with self.subTest(message=message):
                write_events(path, CODEX_EVENTS[:2] + [{"type": "error", "message": message}] + CODEX_EVENTS[2:])
                with self.assertRaisesRegex(ProviderError, "terminal or unknown error"):
                    parse_codex_events(path, exit_code=0)

    @patch("unattended.providers.run_process")
    def test_completed_network_reconnect_review_retains_rework_and_recovers_without_call(self, process):
        def reconnecting_review(argv, **kwargs):
            result = self.fake_codex(argv, **kwargs)
            wait = {"type": "error", "message": "Reconnecting... waiting for network (Connection failed: error sending request)"}
            write_events(result.stdout_path, CODEX_EVENTS[:2] + [wait] * 3 + CODEX_EVENTS[2:])
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(REVIEW | {
                "decision": "rework", "feedback": "Use the loaded Clock to drive simulation"}), encoding="utf-8")
            return result
        process.side_effect = reconnecting_review
        out = self.root / "review-network-recovery"
        actual = self.providers.review("Review", self.root, out, 30)
        self.assertEqual(actual["decision"], "rework")
        self.assertEqual(self.providers.recover("review", out), actual)
        self.assertEqual(process.call_count, 1)

    def test_structured_output_has_no_extra_keys_or_missing_task_evidence(self):
        variants = [PLAN | {"extra": True}, PLAN | {"action": "unknown"},
                    PLAN | {"acceptance_criteria": []}, PLAN | {"title": ""},
                    PLAN | {"acceptance_criteria": [False]}]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                validate_output("plan", value)
        with self.assertRaises(ProviderError):
            validate_output("review", REVIEW | {"head_sha": "short"})

    @patch("unattended.providers.run_process")
    def test_doctor_never_launches_a_process(self, process):
        report = self.providers.doctor()
        self.assertFalse(report["sends_model_request"])
        self.assertEqual(report["authentication"], "not_checked")
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
