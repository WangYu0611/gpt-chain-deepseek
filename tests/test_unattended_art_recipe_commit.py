"""Real Git commits of exact art inputs, with bound evidence and cached recovery."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from unattended import art_pipeline, gitops
from unattended.engine import Engine
from unattended.storage import read_json, write_json
from tests import test_unattended_art_engine as fixtures
from tests.test_unattended_art_pipeline import digest


class ArtRecipeCommitTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ArtEngineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        patch.stopall()
        self.engine, self.work = self.f.engine, self.f.work
        self.recipe = self.work / "housing-recipe.json"
        self.document = {"version": 1, "assets": []}
        for asset in self.f.evidence.config["assets"]:
            entry = {key: deepcopy(asset[key]) for key in art_pipeline.RECIPE_FIELDS}
            entry["max_footprint_m"] = [12, 12]
            self.document["assets"].append(entry)
        write_json(self.recipe, self.document)
        (self.work / "brief.txt").write_text("trusted baseline brief", encoding="utf-8")
        (self.work / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        gitops.git(self.work, "init", "-b", self.f.config["branch"])
        gitops.git(self.work, "config", "user.name", "Offline Recipe Test")
        gitops.git(self.work, "config", "user.email", "offline@example.invalid")
        gitops.git(self.work, "add", ".")
        gitops.git(self.work, "commit", "-m", "BASE")
        self.base = gitops.head(self.work)
        self.cfg = {**self.f.evidence.config, "recipe_file": str(self.recipe)}
        write_json(self.f.config["art_pipeline"]["config_file"], self.cfg)
        patch.object(Engine, "_locks", side_effect=lambda: nullcontext()).start()
        patch.object(self.engine, "_workspace", return_value=self.work).start()
        self.addCleanup(patch.stopall)

    def state(self):
        return read_json(self.engine.state_path)

    def dispatch(self):
        self.engine.enqueue_art(fixtures.TASK)
        result = self.engine.step()
        self.assertEqual(result["phase"], "CODE", result)

    def enable_job(self):
        self.job = self.work / "housing-job.py"
        self.job.write_text("def run_job(context):\n    return {'instances': [], 'plots': [], 'report': {}}\n", encoding="utf-8")
        (self.work / "BLENDER_JOB_API.md").write_text("Trusted read-only API contract", encoding="utf-8")
        gitops.git(self.work, "add", "--", "housing-job.py", "BLENDER_JOB_API.md")
        gitops.git(self.work, "commit", "-m", "JOB BASE")
        self.base = gitops.head(self.work)
        self.cfg["blender_job_file"] = str(self.job)
        write_json(self.f.config["art_pipeline"]["config_file"], self.cfg)

    def edit_and_bind(self, *, edit_recipe=True, job_source=None):
        if edit_recipe:
            self.document["assets"][0].update(revision="v002", prompt="Approved bounded low house revision", target_height_m=3)
            write_json(self.recipe, self.document)
        if job_source is not None:
            self.job.write_text(job_source, encoding="utf-8")
        _, provenance, raw = art_pipeline.recipe_overlay(self.cfg)
        snapshot = Path(provenance["snapshot"])
        snapshot.parent.mkdir(exist_ok=True)
        snapshot.write_bytes(raw)
        public = self.f.evidence.public
        report = read_json(public / "review-package.json")
        report["recipe"] = provenance
        source = public / "production-input.json"
        inputs = {"recipe": provenance, "blender_provenance": report["blender_provenance"]}
        job_provenance, job_raw = art_pipeline.blender_job_snapshot(self.cfg)
        if job_provenance is not None:
            snapshot = Path(job_provenance["snapshot"])
            snapshot.parent.mkdir(exist_ok=True)
            snapshot.write_bytes(job_raw)
            inputs["blender_job"] = report["blender_job"] = job_provenance
        write_json(source, inputs)
        manifest = Path(report["manifest"])
        data = read_json(manifest)
        data["input_manifest_sha256"] = digest(source)
        if job_provenance is not None:
            data["blender_job"] = job_provenance
        write_json(manifest, data)
        imported = report["ue_import"]
        imported["manifest_sha256"] = digest(manifest)
        import_file = public / "receipts" / (imported["run"] + "-import.json")
        write_json(import_file, imported)
        report["capture"]["capture_id"] = art_pipeline.capture_fingerprint(
            imported["run"], import_file, art_pipeline.REPO / "art_tools/ue/capture_preview.py")
        write_json(public / "receipts" / (report["capture"]["capture_id"] + "-capture.json"), report["capture"])
        report["budget"]["budget"] = self.f.evidence.review_ledger(actual=260)
        write_json(public / "review-package.json", report)
        return deepcopy(self.f.providers.code.return_value)

    def test_controller_commits_only_bound_recipe_and_check_review_use_new_anchor(self):
        self.dispatch()
        self.f.providers.code.side_effect = lambda *args, **kwargs: self.edit_and_bind()
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_CHECK", result)
        current = gitops.head(self.work)
        state = self.state()
        self.assertNotEqual(current, self.base)
        self.assertEqual(state["task"]["base_sha"], self.base)
        self.assertEqual(state["expected_head"], current)
        self.assertEqual(state["task"]["art_workspace_head"], current)
        self.assertEqual(gitops.git(self.work, "diff", "--name-only", self.base, current), "housing-recipe.json")
        self.assertTrue(gitops.clean(self.work))
        self.assertEqual(self.engine.step()["phase"], "ART_REVIEW")
        self.assertEqual(self.state()["task"]["check_results"][0]["head_sha"], current)
        self.f.providers.review.return_value["head_sha"] = current
        self.assertEqual(self.engine.step()["phase"], "PLAN")
        self.assertEqual(self.state()["art_pending"][0]["head_sha"], current)
        self.assertEqual(self.engine.step()["phase"], "DONE")

    def test_blocked_cached_success_retry_never_reinvokes_model_or_regenerates(self):
        self.dispatch()
        state = self.state()
        op = self.engine._new_operation(state, "code")
        cached = self.edit_and_bind()
        state.update(phase="BLOCKED", blocked_from="CODE", failures=3, last_error="old clean-workspace fence")
        self.engine._save(state)
        self.f.providers.recover.return_value = cached
        self.assertEqual(self.engine.retry()["phase"], "CODE")
        self.assertEqual(self.state()["operation"]["output_dir"], op["output_dir"])
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_CHECK", result)
        self.f.providers.code.assert_not_called()
        self.assertEqual(len(self.state()["task"]["art_recipe_commits"]), 1)
        self.assertEqual(self.state()["task"]["session_id"], fixtures.SESSION)

    def test_other_tracked_untracked_and_ignored_files_are_rejected_before_model(self):
        self.dispatch()
        for name in ("brief.txt", "extra.txt", "ignored.txt"):
            file = self.work / name
            original = file.read_bytes() if file.exists() else None
            file.write_text("unexpected change", encoding="utf-8")
            with self.subTest(name=name):
                result = self.engine.step()
                self.assertIn("recipe_file", result["last_error"])
                self.f.providers.code.assert_not_called()
                self.assertEqual(gitops.head(self.work), self.base)
            if original is None:
                file.unlink()
            else:
                file.write_bytes(original)

    def test_report_not_bound_to_current_recipe_cannot_create_commit(self):
        self.dispatch()
        def stale(*args, **kwargs):
            self.document["assets"][0]["front_rotation_deg"] = 90
            write_json(self.recipe, self.document)
            return deepcopy(self.f.providers.code.return_value)
        self.f.providers.code.side_effect = stale
        result = self.engine.step()
        self.assertEqual(result["phase"], "CODE")
        self.assertIn("Independent asset check failed", result["last_error"])
        self.assertEqual(gitops.head(self.work), self.base)

    def test_crash_after_commit_recovers_exact_intent_and_cached_result_once(self):
        self.dispatch()
        self.f.config["max_failures"] = 1
        cached = deepcopy(self.f.providers.code.return_value)
        self.f.providers.code.side_effect = lambda *args, **kwargs: self.edit_and_bind()
        original = self.engine._recover_art_recipe_commit
        calls = []
        def interrupted(state, work):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("crash immediately after git commit")
            return original(state, work)
        with patch.object(self.engine, "_recover_art_recipe_commit", side_effect=interrupted):
            self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        committed = gitops.head(self.work)
        self.assertNotEqual(committed, self.base)
        self.assertIn("art_recipe_commit", self.state()["task"])
        self.f.providers.recover.return_value = cached
        self.assertEqual(self.engine.retry()["phase"], "CODE")
        self.assertEqual(self.engine.step()["phase"], "ART_CHECK")
        self.assertEqual(gitops.head(self.work), committed)
        self.assertEqual(gitops.git(self.work, "rev-list", "--count", self.base + "..HEAD"), "1")
        self.f.providers.code.assert_called_once()

    def test_job_only_commit_binds_both_inputs_but_commits_only_the_changed_file(self):
        self.enable_job()
        self.dispatch()
        recipe_sha = digest(self.recipe)
        self.f.providers.code.side_effect = lambda *a, **k: self.edit_and_bind(
            edit_recipe=False, job_source="def run_job(context):\n    return {'instances': [], 'plots': [], 'report': {'authored': True}}\n")
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_CHECK", result)
        record = self.state()["task"]["art_recipe_commits"][0]
        self.assertEqual(record["version"], 2)
        self.assertEqual(record["changed_paths"], ["housing-job.py"])
        self.assertEqual(record["files"], {"housing-recipe.json": recipe_sha, "housing-job.py": digest(self.job)})
        self.assertEqual(gitops.git(self.work, "diff", "--name-only", self.base, "HEAD"), "housing-job.py")
        self.assertTrue(gitops.clean(self.work))
        prompt = self.f.providers.code.call_args.args[0]
        self.assertIn("run_job(context)", prompt)
        self.assertIn("BLENDER_JOB_API.md", prompt)

    def test_two_file_crash_recovery_rejects_changed_file_and_package_then_recovers_once(self):
        self.enable_job()
        self.dispatch()
        self.f.config["max_failures"] = 1
        cached = deepcopy(self.f.providers.code.return_value)
        self.f.providers.code.side_effect = lambda *a, **k: self.edit_and_bind(
            job_source="def run_job(context):\n    return {'instances': [], 'plots': [], 'report': {'revision': 2}}\n")
        original, calls = self.engine._recover_art_recipe_commit, []
        def interrupted(state, work):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("crash after two-file commit")
            return original(state, work)
        with patch.object(self.engine, "_recover_art_recipe_commit", side_effect=interrupted):
            self.assertEqual(self.engine.step()["phase"], "BLOCKED")
        committed = gitops.head(self.work)
        intent = self.state()["task"]["art_recipe_commit"]
        self.assertEqual(intent["changed_paths"], ["housing-job.py", "housing-recipe.json"])
        self.assertEqual(set(intent["files"]), {"housing-job.py", "housing-recipe.json"})
        package = self.f.evidence.public / "review-package.json"
        for path in (self.job, self.recipe, package):
            raw = path.read_bytes()
            path.write_bytes(raw + b"\n ")
            with self.subTest(path=path.name), self.assertRaises(RuntimeError):
                self.engine.retry()
            path.write_bytes(raw)
        self.f.providers.recover.return_value = cached
        self.assertEqual(self.engine.retry()["phase"], "CODE")
        self.assertEqual(self.engine.step()["phase"], "ART_CHECK")
        self.assertEqual(gitops.head(self.work), committed)
        self.assertEqual(gitops.git(self.work, "rev-list", "--count", self.base + "..HEAD"), "1")
        self.assertEqual(len(self.state()["task"]["art_recipe_commits"]), 1)
        self.f.providers.code.assert_called_once()

    def test_legacy_recipe_only_pending_intent_still_commits_and_recovers(self):
        self.dispatch()
        state = self.state()
        op = self.engine._new_operation(state, "code")
        cached = self.edit_and_bind()
        state["task"]["art_recipe_commit"] = {
            "base_sha": self.base, "relative_path": "housing-recipe.json", "recipe_sha256": digest(self.recipe),
            "review_package_sha256": digest(self.f.evidence.public / "review-package.json"),
            "operation_output_dir": op["output_dir"]}
        self.engine._save(state)
        self.f.providers.recover.return_value = cached
        result = self.engine.step()
        self.assertEqual(result["phase"], "ART_CHECK", result)
        self.assertEqual(self.state()["task"]["art_recipe_commits"][0]["relative_path"], "housing-recipe.json")
        self.f.providers.code.assert_not_called()

    def test_job_report_mismatch_and_api_document_edits_cannot_create_commit(self):
        self.enable_job()
        self.dispatch()
        def stale(*args, **kwargs):
            self.job.write_text("def run_job(context):\n    return {}\n", encoding="utf-8")
            return deepcopy(self.f.providers.code.return_value)
        self.f.providers.code.side_effect = stale
        result = self.engine.step()
        self.assertIn("Independent asset check failed", result["last_error"])
        self.assertEqual(gitops.head(self.work), self.base)
        self.f.providers.code.reset_mock()
        (self.work / "BLENDER_JOB_API.md").write_text("unauthorized API edit", encoding="utf-8")
        result = self.engine.step()
        self.assertIn("blender_job_file", result["last_error"])
        self.f.providers.code.assert_not_called()


if __name__ == "__main__":
    unittest.main()
