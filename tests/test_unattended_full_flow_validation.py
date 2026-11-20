from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unattended import full_flow_validation as flow
from unattended.storage import read_json, write_json


class FullFlowOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name).resolve() / "runtime"
        self.source = self.runtime / "workspace"
        self.source.mkdir(parents=True)
        self.head = "a" * 40
        self.session = "93f21123-fe41-4218-83e6-411af1a855d3"
        self.branch = "99db06e4-336d-4d8e-927d-1db262334adf"
        self.state = {"phase": "BLOCKED", "operation": None, "task": {
            "id": "TASK-LOCAL-0091", "session_id": self.session, "reworks": 9}}
        write_json(self.runtime / "state.json", self.state)
        self.original_state = (self.runtime / "state.json").read_bytes()
        self.config = {"runtime_dir": str(self.runtime), "codex": {"command": ["codex"],
                       "model": "gpt-6.2-astra", "reasoning_effort": "ultra"}}
        self.addCleanup(patch.stopall)
        patch.object(flow, "load_config", return_value=self.config).start()
        patch.object(flow.gitops, "head", return_value=self.head).start()
        patch.object(flow.gitops, "clean", return_value=True).start()

    def create(self):
        return Path(flow.create(Path("ignored.json"))["request"])

    def plan(self, request):
        with patch.object(flow, "_native_turn", return_value=self.answer()):
            flow.invoke(Path("ignored"), request, "plan")

    def answer(self):
        return {"session_id": self.branch, "answer": "test report", "model_selection": {"model_verified": False}}

    def receipt(self, request, stage, *, status="passed", receipt_stage=None):
        value = read_json(request)
        write_json(request.parent / "checks" / stage / "receipt.json", {
            **{key: value[key] for key in ("id", "task_id", "head_sha", "request_sha256")},
            "stage": receipt_stage or stage, "status": status,
            "finished_at": None if status == "running" else "2026-11-20T00:00:00Z"})

    def test_preserves_task_and_user_pause(self):
        pause = self.runtime / "PAUSE"
        pause.write_text("user paused\n", encoding="utf-8")
        request = self.create()
        self.assertEqual(pause.read_text(encoding="utf-8"), "user paused\n")
        self.assertFalse(read_json(request.parent / "operator.json")["owns_pause"])
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.original_state)
        self.assertEqual(read_json(request)["request_sha256"], flow.canonical_hash(read_json(request)))

    def test_does_not_interrupt_an_active_main_operation(self):
        self.state.update(phase="CHECK", operation={"kind": "check"})
        write_json(self.runtime / "state.json", self.state)
        with self.assertRaisesRegex(RuntimeError, "idle"):
            self.create()
        self.assertFalse((self.runtime / "PAUSE").exists())

    def test_does_not_duplicate_pending_operator_job(self):
        self.create()
        with self.assertRaisesRegex(RuntimeError, "already pending"):
            self.create()

    def test_rejects_modified_request_and_plan_binding(self):
        request = self.create()
        value = read_json(request)
        self.plan(request)
        answer = request.parent / "native/plan/answer.json"
        write_json(answer, {**read_json(answer), "head_sha": "b" * 40})
        with self.assertRaisesRegex(ValueError, "another request"):
            flow.invoke(Path("ignored"), request, "test")
        write_json(request, {**value, "head_sha": "b" * 40})
        with self.assertRaisesRegex(ValueError, "request SHA"):
            flow._request(request)

    def test_legacy_request_is_readable_but_cannot_invoke_a_model(self):
        request = self.create()
        value = read_json(request)
        value.update(model="claude-fable-5-2", effort="high")
        value["request_sha256"] = flow.canonical_hash(value)
        write_json(request, value)
        self.assertEqual(flow._request(request)[0]["model"], "claude-fable-5-2")
        with patch.object(flow, "_native_turn") as native:
            with self.assertRaisesRegex(RuntimeError, "Historical Claude requests"):
                flow.invoke(Path("ignored"), request, "plan")
        native.assert_not_called()

    def test_plan_uses_fresh_readonly_codex_through_the_project_proxy(self):
        request = self.create()
        endpoint = "http://127.0.0.1:17907"
        self.config.update(config_path="test-config", network_file="test-network")
        rows = [
            {"type": "thread.started", "thread_id": self.branch},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "coverage plan"}},
            {"type": "turn.completed"},
        ]
        def run(argv, **kwargs):
            self.assertEqual(argv[argv.index("--model") + 1], "gpt-6.2-astra")
            self.assertIn('model_reasoning_effort="ultra"', argv)
            self.assertIn('forced_login_method="chatgpt"', argv)
            self.assertIn("--no-daemon", argv)
            self.assertIn("--ignore-user-config", argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
            self.assertNotIn(self.session, argv)
            self.assertEqual(kwargs["environment"]["HTTPS_PROXY"], endpoint)
            self.assertNotIn("ANTHROPIC_API_KEY", kwargs["environment"])
            (kwargs["output_dir"] / "stdout.log").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            return SimpleNamespace(exit_code=0, timed_out=False, output_limit_exceeded=False)
        with patch.object(flow.AgentProviders, "_check_codex_subscription") as preflight, \
             patch.object(flow, "proxy_environment", return_value={"HTTPS_PROXY": endpoint}) as route, \
             patch.object(flow, "run_process", side_effect=run):
            flow.invoke(Path("ignored"), request, "plan")
        route.assert_called_once_with(config_path="test-config", network_file="test-network", secrets_file=None)
        self.assertEqual(preflight.call_args.args[2]["HTTPS_PROXY"], endpoint)
        answer = read_json(request.parent / "native/plan/answer.json")
        self.assertEqual(answer["answer"], "coverage plan")
        self.assertFalse(answer["model_selection"]["model_verified"])
        self.assertEqual(answer["head_sha"], self.head)
        self.assertEqual((self.runtime / "state.json").read_bytes(), self.original_state)

    def test_test_executes_fixed_stages_before_model_verification(self):
        request = self.create()
        self.plan(request)
        order = []
        def stage(path, name):
            self.assertEqual(path, request)
            order.append(name)
            self.receipt(request, name, status="failed" if name == "visual" else "passed")
        def review(config, source, output, prompt, model, effort):
            self.assertEqual(order, list(flow.STAGES[:-1]))
            self.assertIn('"visual": "failed"', prompt)
            self.assertEqual((model, effort), ("gpt-6.2-astra", "high"))
            return self.answer()
        self.config["codex"]["reasoning_effort"] = "high"
        with patch.object(flow.full_flow_checks, "run_stage", side_effect=stage), \
             patch.object(flow, "_native_turn", side_effect=review):
            flow.invoke(Path("ignored"), request, "test")
        status = read_json(request.parent / "status.json")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["effort"], "high")
        self.assertEqual(read_json(request)["effort"], "ultra")

    def test_running_receipt_is_not_a_completed_stage(self):
        request = self.create()
        self.plan(request)
        for stage in flow.STAGES[:-1]:
            self.receipt(request, stage, status="running" if stage == "regression" else "passed")
        with patch.object(flow, "_native_turn", return_value=self.answer()), \
             patch.object(flow.full_flow_checks, "run_stage") as execute:
            flow.invoke(Path("ignored"), request, "analysis")
        execute.assert_not_called()
        status = read_json(request.parent / "status.json")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["missing_stages"], ["regression"])

    def test_analysis_continuation_preserves_old_logs(self):
        request = self.create()
        self.plan(request)
        with patch.object(flow, "_native_turn", return_value=self.answer()):
            flow.invoke(Path("ignored"), request, "analysis")
            original = (request.parent / "native/analysis/answer.json").read_bytes()
            flow.invoke(Path("ignored"), request, "analysis")
        self.assertEqual((request.parent / "native/analysis/answer.json").read_bytes(), original)
        self.assertEqual(read_json(request.parent / "status.json")["output_dir"],
                         str(request.parent / "native/analysis-2"))

    def test_copied_receipt_cannot_complete_test(self):
        request = self.create()
        self.plan(request)
        self.receipt(request, "visual", receipt_stage="build")
        with patch.object(flow, "_native_turn") as native:
            with self.assertRaisesRegex(ValueError, "another stage"):
                flow.invoke(Path("ignored"), request, "analysis")
        native.assert_not_called()
        self.assertEqual(read_json(request.parent / "status.json")["status"], "failed")


if __name__ == "__main__":
    unittest.main()
