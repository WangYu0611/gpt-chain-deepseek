"""Exercise Codex resume recovery against native receipts and a local Git checkout."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

from tests import test_unattended_recovery as recovery
from unattended import gitops
from unattended.providers import ProviderError
from unattended.storage import write_json


OLD_SESSION = "550e8400-e29b-41d4-a716-446655440000"
NEW_SESSION = "550e8400-e29b-41d4-a716-446655440001"
COMPACTION_ERROR = "Error running remote compact task: Connection failed: error sending request"
WORK_IN_PROGRESS = "Do not discard unfinished work\n"


class CodexCompactionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery.RecoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config.update(coding_provider="codex", headless=True, codex={"model": "gpt-6.2-astra"})
        self.f.engine = self.f.restart()
        planned = self.f.plan()
        self.f.config["codex_only"] = True
        self.task_id = planned["task"]["id"]
        self.head = planned["task"]["code_base"]
        self.feature = self.f.work / "feature.txt"

    def _failure(self, cwd, out, session_id, *, message=COMPACTION_ERROR, receipt_task_id=None, activity=None):
        if len(self.f.providers.code_calls) == 1:
            self.feature.write_text(WORK_IN_PROGRESS, encoding="utf-8")
        session = session_id or (NEW_SESSION if len(self.f.providers.code_calls) > 2 else OLD_SESSION)
        out.mkdir(parents=True)
        events = [
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "error", "message": "Model refresh timed out"}},
            {"type": "turn.started"},
            {"type": "error", "message": "Reconnecting... 2/2 (Connection failed: error sending request)"},
            {"type": "item.completed", "item": activity or {"type": "error", "message": COMPACTION_ERROR}},
            {"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}},
        ]
        (out / "stdout.log").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        (out / "stderr.log").write_text("", encoding="utf-8")
        write_json(out / "process.json", {
            "kind": "code", "exit_code": 1, "timed_out": False, "output_limit_exceeded": False,
            "coding_provider": "codex", "coding_protocol": "codex_exec_v1", "coding_sandbox": "workspace-write",
            "codex_auth_mode": "chatgpt", "coding_model": "gpt-6.2-sol", "coding_reasoning_effort": "high",
            "requested_session": session_id, "session_id": session, "transient": True, "retry_reason": "transport",
            "workspace": str(cwd), "output_dir": str(out), "task_id": receipt_task_id or self.task_id,
        })
        raise ProviderError("Temporary provider failure", session_id=session, transient=True, retry_reason="transport")

    def _run_failed_attempts(self, times, **receipt_overrides):
        self.f.providers.code_callback = lambda prompt, cwd, out, timeout, session_id: self._failure(
            cwd, out, session_id, **receipt_overrides)
        for _ in range(times):
            self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
            state = self.f.load()
            self.f.clock[0] = datetime.fromisoformat(state["provider_retry"]["next_retry_at"])
        return state

    def _finish(self, expected_session):
        def code(prompt, cwd, out, timeout, session_id):
            self.assertEqual(session_id, expected_session)
            self.assertEqual(gitops.head(cwd), self.head)
            self.assertEqual(self.feature.read_text(encoding="utf-8"), WORK_IN_PROGRESS)
            self.assertEqual(self.f.load()["operation"]["requested_session_id"], expected_session)
            self.assertEqual(self.f.load()["task"]["code_base"], self.head)
            self.f.providers.commit(cwd, "recovered work\n")
            return {"completed": True, "session_id": session_id or NEW_SESSION, "text": "Committed."}

        self.f.providers.code_callback = code
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        return self.f.load()

    def test_restarts_same_task_and_preserves_work_after_failed_resume_compaction(self):
        retry = self._run_failed_attempts(2)
        source = Path(retry["operation"]["output_dir"])
        receipts = {name: (source / name).read_bytes() for name in ("process.json", "stdout.log", "stderr.log")}
        self.f.clock[0] -= timedelta(seconds=1)
        done = self._finish(expected_session=None)

        self.assertNotEqual(self.f.providers.code_calls[-1]["out"], source)
        self.assertEqual([call["session_id"] for call in self.f.providers.code_calls], [None, OLD_SESSION, None])
        self.assertEqual(done["task"]["session_id"], NEW_SESSION)
        self.assertEqual(done["task"]["reworks"], retry["task"]["reworks"])
        archived = done["task"]["session_history"][-1]
        self.assertEqual(archived["session_id"], OLD_SESSION)
        self.assertEqual(archived["reason"], "codex_remote_compaction")
        self.assertEqual(Path(archived["source_output_dir"]), source)
        self.assertEqual({name: (source / name).read_bytes() for name in receipts}, receipts)
        self.assertNotIn("provider_retry", done)

    def test_ordinary_transport_keeps_the_native_session(self):
        self._run_failed_attempts(2, message="Connection failed: error sending request")
        done = self._finish(expected_session=OLD_SESSION)
        self.assertFalse(done["task"].get("session_history"))

    def test_receipt_for_another_task_cannot_retire_the_current_session(self):
        self._run_failed_attempts(2, receipt_task_id="TASK-AUTO-9999")
        done = self._finish(expected_session=OLD_SESSION)
        self.assertFalse(done["task"].get("session_history"))

    def test_compaction_error_after_model_activity_keeps_the_native_session(self):
        self._run_failed_attempts(2, activity={"type": "agent_message", "text": "Made progress; continuing."})
        done = self._finish(expected_session=OLD_SESSION)
        self.assertFalse(done["task"].get("session_history"))

    def test_repeated_compaction_errors_do_not_replace_another_session_for_same_work(self):
        self._run_failed_attempts(4)
        done = self._finish(expected_session=NEW_SESSION)
        self.assertEqual([call["session_id"] for call in self.f.providers.code_calls],
                         [None, OLD_SESSION, None, NEW_SESSION, NEW_SESSION])
        self.assertEqual([item["session_id"] for item in done["task"]["session_history"]], [OLD_SESSION])

    def test_committed_candidate_goes_to_check_without_another_coding_call(self):
        self._run_failed_attempts(1)

        def commit_before_disconnect(prompt, cwd, out, timeout, session_id):
            self.f.providers.commit(cwd, "committed before disconnect\n")
            self._failure(cwd, out, session_id)

        self.f.providers.code_callback = commit_before_disconnect
        self.assertEqual(self.f.restart().step()["status"], "waiting_provider")
        waiting = self.f.load()
        self.f.clock[0] = datetime.fromisoformat(waiting["provider_retry"]["next_retry_at"])
        self.assertEqual(self.f.restart().step()["phase"], "CHECK")
        self.assertEqual(len(self.f.providers.code_calls), 2)
        task = self.f.load()["task"]
        self.assertEqual(task["candidate"]["head_sha"], gitops.head(self.f.work))
        self.assertEqual(task["session_id"], OLD_SESSION)
        self.assertFalse(task.get("session_history"))
