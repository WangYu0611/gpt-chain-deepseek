"""Art execution pins the native model and grants reviewers read-only evidence."""
import copy
import hashlib
import json
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from tests import test_unattended_art_integration as fixtures
from tests.test_unattended_providers import CODEX_EVENTS, REVIEW, write_events
from unattended.config import (ART_CODING_MODEL, ART_CODING_REASONING_EFFORT,
                               load_config, validate_art_worker_settings)
from unattended.providers import AgentProviders, ProviderError, _codex_art_review_directory

SESSION = "550e8400-e29b-41d4-a716-446655440000"


class ArtProviderTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config["claude"].update(model="claude-sonnet-5-5", reasoning_effort="medium")
        self.f.state["task"].update(kind="art", coding_model=ART_CODING_MODEL,
                                   coding_reasoning_effort=ART_CODING_REASONING_EFFORT,
                                   art_request={"config_file": str(self.f.batch),
                                                "config_sha256": hashlib.sha256(self.f.batch.read_bytes()).hexdigest(),
                                                "artifacts_dir": str(self.f.artifacts)})
        self.f.state["operation"].update(coding_model=ART_CODING_MODEL,
                                        coding_reasoning_effort=ART_CODING_REASONING_EFFORT)
        self.f.save_state()

    def rows(self, model=ART_CODING_MODEL, *, usage=True):
        return [{"type": "system", "subtype": "init", "session_id": SESSION, "model": model},
                {"type": "result", "subtype": "success", "session_id": SESSION, "is_error": False,
                 "result": "Local art work ready for independent review.",
                 **({"modelUsage": {model: {"inputTokens": 100, "outputTokens": 10}}} if usage else {})}]

    def capture_claude(self, rows=None):
        def launch(argv, **kwargs):
            out = kwargs["output_dir"]
            write_events(out / "stdout.log", rows if rows is not None else self.rows())
            (out / "stderr.log").write_text("", encoding="utf-8")
            return ProcessResult(0, False, False, out / "stdout.log", out / "stderr.log")
        return launch

    def review_state(self):
        self.f.state["phase"] = "ART_REVIEW"
        self.f.state["operation"]["kind"] = "review"
        self.f.save_state()

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_art_uses_opus_high_and_records_native_evidence_without_mutating_general_config(self, launch, auth):
        before = copy.deepcopy(self.f.config)
        launch.side_effect = self.capture_claude()
        provider = AgentProviders(self.f.config)
        result = provider.code("Produce art.", self.f.workspace, self.f.out, 120)
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], ART_CODING_MODEL)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(self.f.config, before)
        selection = result["model_selection"]
        self.assertTrue(selection["model_verified"])
        self.assertEqual(selection["observed_model"], ART_CODING_MODEL)
        self.assertEqual(selection["model_usage_models"], [ART_CODING_MODEL])
        self.assertEqual(selection["requested_reasoning_effort"], "high")
        self.assertEqual(selection["reasoning_effort_source"], "cli_argument")
        receipt = json.loads((self.f.out / "process.json").read_text())
        self.assertTrue(receipt["strict_model_selection"])
        self.assertEqual(receipt["model_selection"], selection)
        self.assertEqual(provider.recover("code", self.f.out), result)
        self.assertEqual(launch.call_count, 1)

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_art_rejects_wrong_actual_model_or_missing_usage_evidence(self, launch, auth):
        for index, rows in enumerate((self.rows("claude-sonnet-5-5"), self.rows(usage=False))):
            self.f.out = self.f.runtime / "runs" / f"code-art-{index}"
            self.f.state["operation"]["output_dir"] = str(self.f.out)
            self.f.save_state()
            launch.side_effect = self.capture_claude(rows)
            with self.subTest(index=index), self.assertRaisesRegex(ProviderError, "Opus"):
                AgentProviders(self.f.config).code("Produce art.", self.f.workspace, self.f.out, 120)
            receipt = json.loads((self.f.out / "process.json").read_text())
            self.assertFalse(receipt["model_selection"]["model_verified"])
            with self.assertRaisesRegex(ProviderError, "required model"):
                AgentProviders(self.f.config).recover("code", self.f.out)

    @patch("unattended.providers.run_process")
    def test_art_operation_cannot_change_the_pinned_model_or_effort(self, launch):
        for field, wrong in (("coding_model", "claude-sonnet-5-5"), ("coding_reasoning_effort", "low")):
            original = self.f.state["operation"][field]
            self.f.state["operation"][field] = wrong
            self.f.save_state()
            with self.subTest(field=field), self.assertRaises(ProviderError):
                AgentProviders(self.f.config).code("Produce art.", self.f.workspace, self.f.out, 120)
            self.f.state["operation"][field] = original
        launch.assert_not_called()

    @patch("unattended.providers.claude_subscription_ready", return_value=True)
    @patch("unattended.providers.run_process")
    def test_ordinary_code_keeps_its_existing_sonnet_selection(self, launch, auth):
        self.f.state["task"]["kind"] = "code"
        self.f.state["task"]["id"] = self.f.state["operation"]["task_id"] = "TASK-CODE-0001"
        self.f.save_state()
        launch.side_effect = self.capture_claude(self.rows("claude-sonnet-5-5"))
        result = AgentProviders(self.f.config).code("Normal code.", self.f.workspace, self.f.out, 120)
        argv = launch.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertTrue(result["model_selection"]["model_verified"])

    @patch("unattended.providers.run_process")
    def test_codex_review_has_a_read_only_profile_for_exact_public_evidence(self, launch):
        self.review_state()
        def capture(argv, **kwargs):
            out = kwargs["output_dir"]
            write_events(out / "stdout.log", CODEX_EVENTS)
            (out / "stderr.log").write_text("", encoding="utf-8")
            Path(argv[argv.index("-o") + 1]).write_text(json.dumps(REVIEW), encoding="utf-8")
            return ProcessResult(0, False, False, out / "stdout.log", out / "stderr.log")
        launch.side_effect = capture
        self.assertEqual(AgentProviders(self.f.config).review("Review art.", self.f.workspace, self.f.out, 120), REVIEW)
        argv = launch.call_args.args[0]
        self.assertNotIn("--add-dir", argv)
        self.assertNotIn("--sandbox", argv)  # An explicit legacy mode would override the profile.
        profile = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("default_permissions="))
        self.assertTrue(json.loads(profile).startswith("art-review-"))
        filesystem = next(arg.split("=", 1)[1] for arg in argv if ".filesystem=" in arg)
        policy = tomllib.loads("value=" + filesystem)["value"]
        self.assertEqual(policy, {":root": "read", ":workspace_roots": "read", self.f.artifacts.as_posix(): "read"})
        self.assertTrue(any(arg.endswith(".network.enabled=false") for arg in argv))
        self.assertEqual(argv[argv.index("-a") + 1], "never")
        prompt = launch.call_args.kwargs["stdin"].decode()
        self.assertIn(self.f.artifacts.as_posix(), prompt)
        receipt = json.loads((self.f.out / "process.json").read_text())
        self.assertEqual(receipt["review_access"], {"mode": "read-only",
                         "artifact_directories": [str(self.f.artifacts)], "command_network": False})

    def test_review_rejects_changed_config_private_overlap_or_linked_evidence(self):
        self.review_state()
        self.assertEqual(_codex_art_review_directory(self.f.config, self.f.workspace, self.f.out), self.f.artifacts)
        self.f.batch.write_text(json.dumps({**self.f.batch_config, "changed": True}), encoding="utf-8")
        with self.assertRaisesRegex(ProviderError, "queued version"):
            _codex_art_review_directory(self.f.config, self.f.workspace, self.f.out)
        self.f.batch.write_text(json.dumps(self.f.batch_config), encoding="utf-8")
        self.f.state["task"]["art_request"]["config_sha256"] = hashlib.sha256(self.f.batch.read_bytes()).hexdigest()
        self.f.save_state()
        leaf = self.f.artifacts / "linked"
        leaf.write_text("fixture")
        with patch("unattended.providers._reparse", side_effect=lambda path: path == leaf):
            with self.assertRaisesRegex(ProviderError, "linked artifact"):
                _codex_art_review_directory(self.f.config, self.f.workspace, self.f.out)
        self.f.batch.write_text(json.dumps({**self.f.batch_config, "private_state_dir": str(self.f.artifacts / "private")}), encoding="utf-8")
        self.f.state["task"]["art_request"]["config_sha256"] = hashlib.sha256(self.f.batch.read_bytes()).hexdigest()
        self.f.save_state()
        with self.assertRaisesRegex(ProviderError, "overlaps"):
            _codex_art_review_directory(self.f.config, self.f.workspace, self.f.out)

    def test_ordinary_review_does_not_receive_art_access(self):
        self.f.state["task"]["kind"] = "code"
        self.f.state["phase"] = "REVIEW"
        self.f.state["operation"]["kind"] = "review"
        self.f.save_state()
        self.assertIsNone(_codex_art_review_directory(self.f.config, self.f.workspace, self.f.out))

    def test_worker_route_is_separate_and_loading_it_does_not_read_or_mutate_worker_config(self):
        worker = self.f.root / "worker" / "worker.json"
        worker.parent.mkdir()
        worker.write_text("unreadable-as-JSON sentinel", encoding="utf-8")
        config = copy.deepcopy(self.f.config)
        config.pop("art_pipeline")
        config["art_worker"] = {"config_file": str(worker)}
        path = self.f.root / "main.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        loaded = load_config(path)
        self.assertEqual(loaded["art_worker"], {"config_file": str(worker)})
        self.assertNotIn("art_pipeline", loaded)
        self.assertEqual(worker.read_text(), "unreadable-as-JSON sentinel")
        for invalid in ({"config_file": str(self.f.workspace / "worker.json")},
                        {"config_file": str(self.f.runtime / "worker.json")},
                        {"config_file": str(self.f.project / "worker.json")},
                        {"config_file": "relative.json"}, {"config_file": str(worker), "command": ["anything"]},
                        {"config_file": str(worker.parent / "*.json")}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_art_worker_settings(invalid, project=self.f.project, runtime=self.f.runtime)


if __name__ == "__main__":
    unittest.main()
