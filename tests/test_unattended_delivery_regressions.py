"""Real Git regressions for completed turns without a code commit."""
from __future__ import annotations

from pathlib import Path
import unittest

from tests import test_unattended_recovery as recovery
from unattended import gitops
from unattended.storage import read_json


class CompletedWithoutCommitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = recovery.RecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_completed_no_commit_retires_receipt_and_resumes_same_session(self):
        fixture = self.fixture
        fixture.plan()
        completed = {"completed": True, "session_id": "session-acl-failure", "text": "grantWrite failed; no commit."}

        def no_commit(prompt, cwd, out, timeout, session_id):
            (cwd / "feature.txt").write_text("unfinished work to preserve\n", encoding="utf-8")
            fixture.providers.recovered[("code", str(out))] = completed
            return completed
        fixture.providers.code_callback = no_commit
        first = fixture.engine.step()
        self.assertEqual(first["phase"], "CODE")
        self.assertEqual(first["failures"], 1)
        state = fixture.load()
        self.assertIsNone(state["operation"])
        self.assertTrue(state["task"]["code_resume"])
        self.assertEqual(state["task"]["session_id"], "session-acl-failure")
        evidence = read_json(Path(state["task"]["code_failures"][0]))
        self.assertEqual(evidence["kind"], "completed_without_commit")
        self.assertEqual(evidence["provider_result"], completed)
        self.assertEqual(evidence["head_sha"], state["task"]["code_base"])
        self.assertEqual((fixture.work / "feature.txt").read_text(), "unfinished work to preserve\n")

        def finish(prompt, cwd, out, timeout, session_id):
            self.assertEqual(session_id, "session-acl-failure")
            self.assertEqual((cwd / "feature.txt").read_text(), "unfinished work to preserve\n")
            fixture.providers.commit(cwd, "finished after recovering the same session\n")
            return {"completed": True, "session_id": session_id, "text": "Committed after resuming."}
        fixture.providers.code_callback = finish
        self.assertEqual(fixture.restart().step()["phase"], "CHECK")
        self.assertEqual(len(fixture.providers.code_calls), 2)
        self.assertNotEqual(fixture.providers.code_calls[0]["out"], fixture.providers.code_calls[1]["out"])
        self.assertEqual(len(fixture.load()["task"]["candidate"]["commits"]), 1)
        self.assertEqual((fixture.project / "feature.txt").read_text(), "original\n")

    def test_cached_completed_no_commit_is_consumed_once_then_new_attempt_runs(self):
        fixture = self.fixture
        state = fixture.plan()
        operation = fixture.partial_code(state)
        fixture.providers.recovered[("code", operation["output_dir"])] = {
            "completed": True, "session_id": "session-cached", "text": "Protocol completed, Git unchanged."}
        first = fixture.restart().step()
        self.assertEqual(first["phase"], "CODE")
        self.assertEqual(first["failures"], 1)
        self.assertFalse(fixture.providers.code_calls)
        self.assertIsNone(fixture.load()["operation"])
        self.assertEqual(fixture.restart().step()["phase"], "CHECK")
        self.assertEqual(len(fixture.providers.code_calls), 1)
        self.assertEqual(fixture.providers.code_calls[0]["session_id"], "session-cached")
        recovered_code = [call for call in fixture.providers.recover_calls if call[0] == "code"]
        self.assertEqual(len(recovered_code), 1)

    def test_empty_delivery_next_call_receives_feedback_and_finishes_original_dirty_work(self):
        fixture = self.fixture
        state = fixture.plan()
        original_feedback = "R01: Complete the original requested behavior and preserve its regression."
        state["task"]["feedback"] = original_feedback
        fixture.save(state)
        base = state["task"]["code_base"]

        def only_plan(prompt, cwd, out, timeout, session_id):
            self.assertNotIn("执行器交付检查反馈", prompt)
            (cwd / "feature.txt").write_text("partial implementation\n", encoding="utf-8")
            return {"completed": True, "session_id": "session-empty-delivery", "text": "I will finish it."}

        fixture.providers.code_callback = only_plan
        failed = fixture.engine.step()
        self.assertEqual((failed["phase"], failed["failures"]), ("CODE", 1))
        self.assertIsNone(fixture.load().get("provider_retry"))

        def complete(prompt, cwd, out, timeout, session_id):
            self.assertEqual(session_id, "session-empty-delivery")
            self.assertEqual((cwd / "feature.txt").read_text(), "partial implementation\n")
            self.assertEqual(fixture.load()["failures"], 1)
            self.assertEqual(fixture.load()["task"]["feedback"], original_feedback)
            self.assertIn(original_feedback, prompt)
            self.assertIn("上次 DSH 协议轮已结束", prompt)
            self.assertIn("code_base=" + base, prompt)
            self.assertIn("不能仅回复计划", prompt)
            self.assertIn("不得为满足提交条件制造无意义改动", prompt)
            self.assertIn("任务编号为唯一 commit 标题", prompt)
            self.assertIn("最终完整 HEAD", prompt)
            fixture.providers.commit(cwd, "completed original implementation\n")
            return {"completed": True, "session_id": session_id, "text": "Implemented and committed."}

        fixture.providers.code_callback = complete
        self.assertEqual(fixture.restart().step()["phase"], "CHECK")
        saved = fixture.load()
        self.assertEqual(saved["task"]["feedback"], original_feedback)
        self.assertEqual(saved["task"]["session_id"], "session-empty-delivery")
        self.assertEqual(len(saved["task"]["code_failures"]), 1)
        self.assertEqual(len(saved["task"]["candidate"]["commits"]), 1)
        self.assertNotEqual(saved["task"]["candidate"]["head_sha"], base)
        self.assertEqual(len(fixture.providers.code_calls), 2)

    def test_repeated_no_commit_is_bounded_and_retry_retains_original_task(self):
        fixture = self.fixture
        initial = fixture.plan()

        def no_commit(prompt, cwd, out, timeout, session_id):
            result = {"completed": True, "session_id": session_id or "session-no-commit", "text": "No commit."}
            fixture.providers.recovered[("code", str(out))] = result
            return result
        fixture.providers.code_callback = no_commit
        for expected in range(1, fixture.config["max_failures"] + 1):
            result = fixture.restart().step()
            self.assertEqual(result["failures"], expected)
        self.assertEqual(result["phase"], "BLOCKED")
        attempts = fixture.providers.code_calls
        self.assertEqual(len(attempts), fixture.config["max_failures"])
        self.assertEqual(len({str(call["out"]) for call in attempts}), len(attempts))
        state = fixture.load()
        self.assertIsNone(state["operation"])
        self.assertEqual(len(state["task"]["code_failures"]), len(attempts))
        self.assertEqual(gitops.head(fixture.work), initial["task"]["code_base"])
        bounded_count = len(attempts)
        fixture.restart().step()
        self.assertEqual(len(fixture.providers.code_calls), bounded_count)
        fixture.engine.retry()
        fixture.providers.code_callback = None
        self.assertEqual(fixture.restart().step()["phase"], "CHECK")
        self.assertEqual(fixture.load()["task"]["id"], initial["task"]["id"])
        self.assertEqual(fixture.providers.code_calls[-1]["session_id"], "session-no-commit")


if __name__ == "__main__":
    unittest.main()
