"""Task-scoped independent checks stay bound across planning and recovery."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_unattended_user_repair as fixtures
from unattended import ue_checks, user_repair
from unattended.storage import read_json, write_json, write_text


class DynamicCheckReplanningTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.UserRepairTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.engine, self.runtime, self.work = self.f.engine, self.f.runtime, self.f.work
        self.providers = self.f.f.providers
        self.f.config["checks"].extend({"id": f"fixed-{i}", "argv": ["fixture-check", str(i)], "timeout_seconds": 30} for i in range(3))
        state = self.engine._load() if self.engine._binding() == self.f.before["binding"] else deepcopy(self.f.before)
        state["binding"] = self.engine._binding(); self.engine._save(state)
        self.f.request.update(source_state_sha256=user_repair.file_sha(self.engine.state_path),
            source_scope_sha256=ue_checks.task_validation_scope_sha256(state["task"], self.f.config["checks"]),
            max_cycles=1, rework_window=2)
        write_json(self.f.path, self.f.request)
        self.f.activate()
        state = self.engine._load(); task = state["task"]
        state.update(phase="REVIEW", operation=None)
        task["reworks"] = 11
        for row in task["replanning_history"]:
            row["result"] = deepcopy(self.f.f.plan)
        self.checks = self.engine._task_checks(task)
        self.assertEqual(len(self.f.config["checks"]), 5)
        self.assertEqual(len(self.checks), 6)
        rows = []
        for index, check in enumerate(self.checks):
            out = self.runtime / "runs" / f"check-dynamic-{index}"
            write_text(out / "stdout.log", f"Independent {check['id']} completed\n")
            write_text(out / "stderr.log", "")
            result = {"id": check["id"], "head_sha": task["candidate"]["head_sha"], "passed": True,
                      "stdout_path": str(out / "stdout.log"), "stderr_path": str(out / "stderr.log")}
            write_json(out / "check.json", result); rows.append(result)
        task["check_results"] = rows
        self.folder = self.runtime / "tasks" / task["id"]
        write_json(self.folder / "review-11.json", self.f.f.review)
        write_text(self.folder / "delivery-11.md", "Coder delivery with residual issues for independent review.")
        self.engine._save(state)
        self.original = deepcopy(state)

    def begin(self):
        state = self.engine._load()
        self.assertTrue(self.engine._begin_task_replanning(state, self.f.f.review["feedback"], trigger="review_limit"))
        self.assertEqual(state["phase"], "REPLAN")
        self.engine._save(state)
        return state

    def test_six_check_source_binds_dynamic_definition_and_all_six_raw_check_receipts(self):
        task = self.original["task"]
        source = self.engine._replanning_source(task, self.work)
        scope = {key: deepcopy(task.get(key)) for key in
                 ("id", "title", "body", "acceptance_criteria", "core_tests", "base_sha", "coding_provider")}
        scope["checks"] = self.checks
        self.assertEqual(source["scope_sha256"], self.engine._replanning_digest(scope))
        self.assertEqual(source["check_results_sha256"], self.engine._replanning_digest(task["check_results"]))
        for row in task["check_results"]:
            for path in (Path(row["stdout_path"]), Path(row["stderr_path"]), Path(row["stdout_path"]).parent / "check.json"):
                self.assertIn(str(path), source["evidence_files"])

    def test_replanning_source_is_identical_across_cli_and_background_interpreters(self):
        task = self.original["task"]
        with patch("sys.executable", str(self.runtime / "cli/python.exe")):
            cli = self.engine._replanning_source(task, self.work)
        with patch("sys.executable", str(self.runtime / "host/pythonw.exe")):
            background = self.engine._replanning_source(task, self.work)
        self.assertEqual(cli, background)

    def test_missing_reordered_or_wrong_head_dynamic_check_still_refuses_planning(self):
        variants = []
        task = deepcopy(self.original["task"]); task["check_results"].pop(); variants.append(task)
        task = deepcopy(self.original["task"]); task["check_results"][-2:] = reversed(task["check_results"][-2:]); variants.append(task)
        task = deepcopy(self.original["task"]); task["check_results"][-1]["head_sha"] = "9" * 40; variants.append(task)
        for task in variants:
            with self.assertRaisesRegex(ValueError, "完整独立检查记录"):
                self.engine._replanning_source(task, self.work)
        self.providers.plan.assert_not_called()

    def test_same_six_checks_resume_original_session_with_one_existing_epoch_window(self):
        self.begin()
        result = self.engine.step()
        self.assertEqual(result["phase"], "CODE")
        task = self.engine._load()["task"]
        self.assertEqual(self.engine._task_replanning_policy(task), {"enabled": True, "max_cycles": 1, "rework_window": 2})
        self.assertEqual(task["reworks"], 12)
        self.assertEqual(len(self.engine._epoch_replanning_history(task)), 1)
        for field in ("body", "acceptance_criteria", "core_tests", "base_sha", "session_id", "user_repair"):
            self.assertEqual(task[field], self.original["task"][field], field)
        self.assertEqual(task["replanning_history"][:-1], self.original["task"]["replanning_history"])
        state = self.engine._load(); state.update(phase="REVIEW", operation=None)
        self.engine._begin_task_replanning(state, "same authorization already used", trigger="review_limit")
        self.assertEqual(state["phase"], "BLOCKED")
        self.assertIn("窗口已用完", state["last_error"])

    def test_dynamic_definition_or_sixth_raw_log_change_invalidates_saved_source(self):
        self.begin()
        real = self.engine._task_checks
        def changed(task):
            values = real(task); values[-1]["timeout_seconds"] += 1; return values
        with patch.object(self.engine, "_task_checks", side_effect=changed):
            result = self.engine.step()
        self.assertIn("来源已改变", result["last_error"])
        self.providers.plan.assert_not_called()
        write_text(Path(self.original["task"]["check_results"][-1]["stdout_path"]), "changed full-flow evidence")
        result = self.engine.step()
        self.assertIn("来源已改变", result["last_error"])
        self.providers.plan.assert_not_called()

    def test_completed_plan_receipt_recovers_once_with_six_check_source(self):
        self.begin()
        def interrupted(*args, **kwargs):
            self.providers.recover.return_value = deepcopy(self.f.f.plan)
            raise SystemExit("fixture after native planning completion")
        self.providers.plan.side_effect = interrupted
        with self.assertRaises(SystemExit): self.engine.step()
        self.assertEqual(self.engine.step()["phase"], "CODE")
        self.assertEqual(self.providers.plan.call_count, 1)
        self.assertEqual(len(self.engine._epoch_replanning_history(self.engine._load()["task"])), 1)

    def test_code_timeout_source_uses_same_dynamic_checks_without_changing_windows(self):
        task = deepcopy(self.original["task"]); task["reworks"] = 12
        out = self.runtime / "runs" / ("code-" + "4" * 32)
        for name in ("process.json", "stdout.log", "stderr.log"):
            write_text(out / name, "fixture native timeout proof checked by separate contract tests")
        op = {"kind": "code", "output_dir": str(out), "task_id": task["id"]}
        with patch.object(self.engine, "_code_timeout_receipt", return_value=out), \
                patch.object(self.engine, "_code_wip_snapshot", return_value={"files": [], "index_sha256": "i", "worktree_sha256": "w"}):
            source = self.engine._replanning_source(task, self.work, source_phase="CODE", source_operation=op)
        self.assertEqual(source["source_phase"], "CODE")
        self.assertEqual(source["review_round"], 11)
        self.assertIn(str(Path(task["check_results"][-1]["stdout_path"])), source["evidence_files"])
        self.assertEqual(task["user_repair"]["max_cycles"], 1)
        self.assertEqual(task["user_repair"]["rework_window"], 2)
