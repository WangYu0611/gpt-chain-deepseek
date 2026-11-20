"""Codex art production and diagnosis are bounded to the existing art lane."""
from datetime import datetime
import hashlib
import json
import unittest
from unittest.mock import patch

from github_harness_lab.lab_harness.process import ProcessResult
from tests import test_unattended_art_engine as engine_fixture
from tests import test_unattended_art_integration as provider_fixture
from tests.test_unattended_art_diagnose_provider import BRANCH, HEAD, RESULT, SOURCE
from tests.test_unattended_providers import write_events
from unattended.calendar import BEIJING
from unattended.providers import AgentProviders, ProviderError
from unattended.storage import read_json, write_json


class CodexArtProviderTests(unittest.TestCase):
    def setUp(self):
        f = self.f = provider_fixture.ArtIntegrationTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.config.update(coding_provider="codex", planning_provider="codex", codex_only=True)
        f.config["codex"].update(coding_model="gpt-6.2-sol", coding_reasoning_effort="ultra")
        self.recipe = f.workspace / "art-jobs" / "recipe.json"
        self.recipe.parent.mkdir()
        write_json(self.recipe, {"version": 1, "assets": []})
        write_json(f.batch, {**f.batch_config, "recipe_file": str(self.recipe)})
        f.state["task"].update(kind="art", coding_provider="codex", session_id=None,
            art_request={"config_file": str(f.batch), "artifacts_dir": str(f.artifacts),
                         "config_sha256": hashlib.sha256(f.batch.read_bytes()).hexdigest()})
        f.state["operation"].update(coding_provider="codex", coding_model="gpt-6.2-sol",
                                    coding_reasoning_effort="ultra", requested_session_id=None, head_sha=HEAD)
        f.save_state()
        self.provider = AgentProviders(f.config)

    @staticmethod
    def capture(out, final, *, thread=BRANCH):
        write_events(out / "stdout.log", [{"type": "thread.started", "thread_id": thread},
                     {"type": "turn.started"}, {"type": "item.completed",
                     "item": {"type": "agent_message", "text": final}}, {"type": "turn.completed"}])
        (out / "stderr.log").write_text("", encoding="utf-8")
        return ProcessResult(0, False, False, out / "stdout.log", out / "stderr.log")

    def test_art_coding_edit_scope_leaves_git_and_paid_preview_to_controller(self):
        def launch(argv, **kwargs):
            self.assertIn("gpt-6.2-sol", argv)
            self.assertIn('model_reasoning_effort="ultra"', argv)
            filesystem = next(option for option in argv if ".filesystem=" in option)
            self.assertIn('":workspace_roots"="read"', filesystem)
            self.assertIn(json.dumps(self.recipe.parent.as_posix()) + '="write"', filesystem)
            self.assertNotIn(".git", filesystem)
            self.assertTrue(any(option.endswith(".network.enabled=false") for option in argv))
            return self.capture(kwargs["output_dir"], "Recipe edited")

        with patch.object(self.provider, "_check_codex_subscription"), patch("unattended.providers.run_process", side_effect=launch):
            result = self.provider.code("Prepare art.", self.f.workspace, self.f.out, timeout=60)
        self.assertEqual(result["text"], "Recipe edited")
        self.assertEqual(read_json(self.f.out / "process.json")["coding_writable_git_roots"], [])

    def test_diagnosis_uses_astra_in_a_new_read_only_thread(self):
        f = self.f
        f.state["phase"] = "ART_DIAGNOSE"
        f.state["task"].update(session_id=SOURCE, session_owner_task_id=f.state["task"]["id"])
        f.state["operation"].update(kind="art_diagnose", diagnosis_provider="codex",
                                    diagnosis_model="gpt-6.2-astra", diagnosis_reasoning_effort="high")
        task_documents = f.runtime / "tasks" / f.state["task"]["id"]
        task_documents.mkdir(parents=True)
        f.save_state()

        def launch(argv, **kwargs):
            self.assertIn("gpt-6.2-astra", argv)
            self.assertIn('model_reasoning_effort="high"', argv)
            self.assertNotIn("resume", argv)
            filesystem = next(option for option in argv if ".filesystem=" in option)
            self.assertNotIn('="write"', filesystem)
            return self.capture(kwargs["output_dir"], json.dumps(RESULT))

        with patch.object(self.provider, "_check_codex_subscription"), patch("unattended.providers.run_process", side_effect=launch):
            result = self.provider.art_diagnose("Diagnose art.", f.workspace, f.out, 60, session_id=SOURCE)
        self.assertEqual(result, RESULT)
        self.assertEqual(self.provider.recover("art_diagnose", f.out), RESULT)
        f.state["operation"]["diagnosis_reasoning_effort"] = "ultra"
        f.save_state()
        with self.assertRaisesRegex(ProviderError, "differs from its operation"):
            self.provider.recover("art_diagnose", f.out)


class CodexArtEngineTests(unittest.TestCase):
    def test_art_coding_crosses_cutover_and_runs_preview_before_independent_check(self):
        f = engine_fixture.ArtEngineTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.config.update(coding_provider="codex", planning_provider="codex", codex_only=True)
        f.config["codex"].update(coding_model="gpt-6.2-sol", coding_reasoning_effort="ultra",
                                 effort_change_at="2026-11-22T00:00:00+08:00", effort_after_change="high")
        f.config["schedule"]["always_on"] = True
        f.now = datetime(2026, 11, 21, 23, 59, tzinfo=BEIJING)
        f.engine = f.restart()
        f.dispatch()
        task = f.state()["task"]
        self.assertEqual((task["coding_provider"], task["coding_model"], task["coding_reasoning_effort"]),
                         ("codex", "gpt-6.2-sol", "ultra"))
        f.now = datetime(2026, 11, 22, 0, 0, tzinfo=BEIJING)
        with patch.object(f.engine, "_run_codex_art_preview", return_value=None) as preview:
            result = f.engine.step()
        self.assertEqual(result["phase"], "ART_CHECK", result)
        preview.assert_called_once()
        self.assertEqual(f.state()["task"]["coding_reasoning_effort"], "high")
        f.providers.review.assert_not_called()
