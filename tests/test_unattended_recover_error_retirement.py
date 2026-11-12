"""Rejected successful CLI receipts must not spend the failure budget repeatedly."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_recovery as recovery
from unattended.providers import AgentProviders
from unattended.storage import write_json


class RejectedProviderOutputTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery.RecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.runtime = Path(self.f.config["runtime_dir"])

    def invalid_output(self, kind, directory):
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "process.json", {"kind": kind, "exit_code": 0, "timed_out": False,
                   "output_limit_exceeded": False, "launch_or_capture_failed": False})
        events = [{"type": "thread.started", "thread_id": "thread-rejected"},
                  {"type": "turn.started"}, {"type": "turn.completed"}]
        (directory / "stdout.log").write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
        write_json(directory / "final.json", {"not": "the required schema"})

    def test_live_invalid_plan_is_counted_once_and_restart_creates_new_attempt(self):
        calls = []
        def plan(prompt, cwd, out, timeout):
            calls.append(out)
            if len(calls) == 1:
                self.invalid_output("plan", out)
                return AgentProviders({}).recover("plan", out)
            return dict(recovery.PLAN)
        with patch.object(self.f.providers, "plan", side_effect=plan):
            failed = self.f.restart().step()
            self.assertEqual((failed["phase"], failed["failures"]), ("PLAN", 1))
            state = self.f.load()
            self.assertIsNone(state["operation"])
            audit = calls[0] / "output-rejection.json"
            self.assertEqual(state["provider_output_failures"], [str(audit)])
            original_final = (calls[0] / "final.json").read_bytes()
            self.assertEqual(self.f.restart().step()["phase"], "CODE")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual((calls[0] / "final.json").read_bytes(), original_final)
        self.assertEqual(self.f.load()["failures"], 0)

    def test_rejected_subscription_output_retires_the_plan_attempt(self):
        def plan(prompt, cwd, out, timeout):
            child = out / "codex-subscription"
            self.invalid_output("plan", child)
            process = json.loads((child / "process.json").read_text(encoding="utf-8"))
            write_json(child / "process.json", dict(process, codex_auth_mode="chatgpt"))
            write_json(out / "process.json", dict(process, exit_code=1, codex_auth_mode="api_key",
                                                  codex_subscription_fallback=True))
            events = [{"type": "thread.started", "thread_id": "thread-quota"}, {"type": "turn.started"},
                      {"type": "turn.failed", "error": {"code": "credit_balance_exhausted"}}]
            (out / "stdout.log").write_text("\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8")
            return AgentProviders({}).recover("plan", out)

        with patch.object(self.f.providers, "plan", side_effect=plan):
            failed = self.f.restart().step()
        self.assertEqual((failed["phase"], failed["failures"]), ("PLAN", 1))
        state = self.f.load()
        self.assertIsNone(state["operation"])
        [audit] = state["provider_output_failures"]
        self.assertEqual(Path(audit).parent.name, "codex-subscription")

    def test_invalid_saved_review_preserves_candidate_and_counts_once_before_fresh_review(self):
        self.f.plan()
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        self.assertEqual(self.f.restart().step()["phase"], "REVIEW")
        state = self.f.load()
        task = dict(state["task"])
        op = self.f.engine._new_operation(state, "review")
        self.invalid_output("review", Path(op["output_dir"]))
        old_recover = self.f.providers.recover
        def recover(kind, out):
            if str(out) == op["output_dir"]:
                return AgentProviders({}).recover(kind, out)
            return old_recover(kind, out)
        with patch.object(self.f.providers, "recover", side_effect=recover):
            failed = self.f.restart().step()
            self.assertEqual((failed["phase"], failed["failures"]), ("REVIEW", 1))
            state = self.f.load()
            self.assertIsNone(state["operation"])
            self.assertEqual(state["task"], task)
            self.assertEqual(self.f.providers.review_calls, 0)
            self.assertEqual(self.f.restart().step()["phase"], "PUBLISH")
        self.assertEqual(self.f.providers.review_calls, 1)
        self.assertEqual(self.f.load()["task"], task)

    def test_valid_saved_plan_is_consumed_without_new_model_call_or_rejection(self):
        from unattended import gitops
        work = gitops.ensure_workspace(self.f.config)
        state = self.f.engine._load()
        state["expected_head"] = gitops.head(work)
        op = self.f.engine._new_operation(state, "plan")
        self.f.providers.recovered[("plan", op["output_dir"])] = dict(recovery.PLAN)
        self.assertEqual(self.f.restart().step()["phase"], "CODE")
        self.assertEqual(self.f.providers.plan_calls, 0)
        self.assertNotIn("provider_output_failures", self.f.load())

    def test_three_distinct_invalid_outputs_still_exhaust_the_real_failure_budget(self):
        calls = []
        def invalid_plan(prompt, cwd, out, timeout):
            calls.append(out)
            self.invalid_output("plan", out)
            return AgentProviders({}).recover("plan", out)
        with patch.object(self.f.providers, "plan", side_effect=invalid_plan):
            for count in range(1, 4):
                result = self.f.restart().step()
                self.assertEqual(result["failures"], count)
                self.assertIsNone(self.f.load()["operation"])
                self.assertNotIn("provider_retry", self.f.load())
        self.assertEqual(result["phase"], "BLOCKED")
        self.assertEqual(len(set(calls)), 3)
        self.assertEqual(len(self.f.load()["provider_output_failures"]), 3)


if __name__ == "__main__":
    unittest.main()
