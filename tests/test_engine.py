"""Real local-Git loop tests. No user repositories or online services are used."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from relay.engine import Relay, RelayPaused
from relay.git_repository import GitError, GitRepository
from relay.protocol import ProtocolError
from relay.storage import read_json, write_text


PNG = b"\x89PNG\r\n\x1a\nfixture-image"


class FakeBrowser:
    def __init__(self, fixture, mode="execute"):
        self.fixture = fixture
        self.mode = mode
        self.calls = []
        self.sent = []
        self.interrupted = False

    def exchange(self, prompt, attachments, relay_id, pending=False):
        self.calls.append({"relay_id": relay_id, "pending": pending, "attachments": list(attachments), "prompt": prompt})
        if not pending:
            self.sent.append(relay_id)
        if self.mode == "interrupted" and not self.interrupted:
            self.interrupted = True
            raise RuntimeError("Fixture: browser disconnected after sending")
        if self.mode == "never":
            raise AssertionError("A cached reply should not reopen the browser")
        if self.mode in ("done", "pause"):
            result = {"relay_id": relay_id, "reviewed_sha": self.fixture.delivery_sha,
                      "action": self.mode, "reason": "fixture decision"}
        else:
            changed = {"Docs/Tasks/NEXT.md": "TASK-002\nImplement the next small feature.\n"}
            if self.mode == "unchanged_task":
                changed = {"unrelated.txt": "A commit without a task update"}
            published = (
                self.fixture.preexisting_task_sha if self.mode == "preexisting_task"
                else self.fixture.publish(changed, push=self.mode != "unpublished_task")
            )
            result = {"relay_id": relay_id, "reviewed_sha": self.fixture.delivery_sha,
                      "action": "execute", "task_id": "TASK-002",
                      "task_path": "Docs/Tasks/MISSING.md" if self.mode == "missing_task" else "Docs/Tasks/NEXT.md",
                      "published_sha": published}
        return "Independent review.\n【交接】" + json.dumps(result) + "【交接结束】"


class FakeRunner:
    def __init__(self, fixture, mode="ok", after_result=None):
        self.fixture = fixture
        self.mode = mode
        self.after_result = after_result
        self.calls = []

    def run(self, prompt, cwd, run_id, session_id=None):
        self.calls.append({"prompt": prompt, "cwd": cwd, "run_id": run_id, "session_id": session_id})
        if self.mode == "interrupted":
            raise RuntimeError("Fixture: uncertain DSH process after launch")
        self.fixture.put(cwd, "app.txt", "implemented feature two\n")
        self.fixture.put(cwd, "Docs/Reports/TASK-002.md", "Build/test fixture passed.\n")
        self.fixture.put(cwd, "Docs/Evidence/TASK-002.png", PNG)
        sha = self.fixture.commit(cwd, "Complete task 002", push=self.mode != "unpublished")
        manifest = {"task_id": "TASK-002", "commit_sha": sha,
                    "report_paths": ["Docs/Reports/TASK-002.md"],
                    "screenshot_paths": ["Docs/Evidence/TASK-002.png"]}
        if self.mode == "wrong_task":
            manifest["task_id"] = "OTHER-TASK"
        if self.after_result:
            self.after_result()
        return SimpleNamespace(session_id="session-fixture", exit_code=0,
                               events_path=self.fixture.runtime / "fake-events.jsonl",
                               answer="Done.\n【交付】" + json.dumps(manifest) + "【交付结束】")


class RelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay engine 中文 ")
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.publisher = self.root / "publisher"
        self.worker = self.root / "worker"
        self.runtime = self.root / "runtime"
        self.git("init", "--bare", "--initial-branch=main", str(self.remote), cwd=self.root)
        self.git("init", "--initial-branch=main", str(self.publisher), cwd=self.root)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.publisher)
        self.put(self.publisher, "app.txt", "base\n")
        self.put(self.publisher, "Docs/Tasks/NEXT.md", "Existing task, must be updated after review.\n")
        self.base_sha = self.commit(self.publisher, "Initial baseline", push=True)
        self.delivery_sha = self.publish({"app.txt":"feature one\n",
                                         "Docs/Reports/TASK-001.md":"Initial delivery report.\n",
                                         "Docs/Evidence/TASK-001.png":PNG})
        self.config = {
            "version":1, "runtime_dir":str(self.runtime),
            "repository":{"url":str(self.remote), "workdir":str(self.worker), "branch":"main", "allow_local_remote":True},
            "browser":{"conversation_url":"https://chatgpt.com/c/fixture", "profile_dir":str(self.root / "profile")},
            "dsh":{"executable":"fake-dsh", "home":str(self.root / "dsh-home")},
            "limits":{"max_cycles":5, "max_revisions":3},
            "evidence":{"upload_attachments":True, "require_reports":True, "require_screenshots":True, "max_total_mb":2, "max_files":10},
        }
        self.delivery = {"task_id":"TASK-001", "commit_sha":self.delivery_sha,
                         "base_sha":self.base_sha, "report_paths":["Docs/Reports/TASK-001.md"],
                         "screenshot_paths":["Docs/Evidence/TASK-001.png"]}

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, cwd):
        environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Relay fixture",
                           GIT_AUTHOR_EMAIL="relay@example.invalid", GIT_COMMITTER_NAME="Relay fixture",
                           GIT_COMMITTER_EMAIL="relay@example.invalid")
        result = subprocess.run(["git", "-c", "protocol.ext.allow=never", *args], cwd=cwd,
                                env=environment, capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout.decode("utf-8").strip()

    def put(self, directory, relative, content):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    def commit(self, directory, message, push=False):
        self.git("add", "--all", cwd=directory)
        self.git("commit", "-m", message, cwd=directory)
        sha = self.git("rev-parse", "HEAD", cwd=directory)
        if push:
            self.git("push", "origin", "HEAD:main", cwd=directory)
        return sha

    def publish(self, changes, push=True):
        self.git("fetch", "origin", "main", cwd=self.publisher)
        self.git("merge", "--ff-only", "origin/main", cwd=self.publisher)
        for relative, content in changes.items():
            self.put(self.publisher, relative, content)
        return self.commit(self.publisher, "Publish fixture changes", push=push)

    def relay(self, browser=None, runner=None, repository=None):
        return Relay(self.config, browser=browser or FakeBrowser(self), runner=runner or FakeRunner(self),
                     repository=repository)

    def seeded(self, browser=None, runner=None, repository=None):
        relay = self.relay(browser, runner, repository)
        relay.seed(self.delivery)
        return relay

    def test_full_review_execute_publish_cycle_stops_before_next_review(self):
        browser, runner = FakeBrowser(self), FakeRunner(self)
        relay = self.seeded(browser, runner)
        relay.run(once=True)
        saved = read_json(self.runtime / "state.json")
        self.assertEqual(saved["phase"], "review_ready")
        self.assertEqual(saved["completed_cycles"], 1)
        self.assertEqual(saved["delivery"]["task_id"], "TASK-002")
        self.assertEqual(saved["session_id"], "session-fixture")
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["cwd"], self.worker)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.worker), saved["delivery"]["commit_sha"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), saved["delivery"]["commit_sha"])
        attachments = browser.calls[0]["attachments"]
        self.assertEqual(len(attachments), 3)
        self.assertEqual(attachments[0].read_text(), "Initial delivery report.\n")
        self.assertEqual(attachments[1].read_bytes(), PNG)
        self.assertIn(b"feature one", attachments[2].read_bytes())
        self.assertNotIn("review", saved)
        self.assertNotIn("task", saved)

    def test_default_github_notification_cycle_does_not_export_or_upload_files(self):
        # Omit the option entirely: the product default must remain notification
        # only, with GPT reading the fixed commit using its own GitHub access.
        self.config["evidence"].pop("upload_attachments")
        browser, runner = FakeBrowser(self), FakeRunner(self)
        relay = self.seeded(browser, runner)
        with patch.object(relay, "prepare_attachments", side_effect=AssertionError("Do not export attachments")) as export:
            relay.run(once=True)
        export.assert_not_called()
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(browser.calls[0]["attachments"], [])
        prompt = browser.calls[0]["prompt"]
        self.assertIn("已连接的 GitHub 工具", prompt)
        self.assertIn("本消息不上传文件", prompt)
        self.assertIn(self.delivery_sha, prompt)
        self.assertIn(str(self.remote), prompt)
        self.assertNotIn("附件包含", prompt)
        self.assertEqual(len(runner.calls), 1)
        review_dir = self.runtime / "turns" / browser.calls[0]["relay_id"]
        self.assertEqual({path.name for path in review_dir.iterdir()}, {"review-prompt.txt", "gpt-answer.txt"})
        saved = read_json(self.runtime / "state.json")
        self.assertEqual(saved["phase"], "review_ready")
        self.assertEqual(saved["completed_cycles"], 1)

    def test_review_pending_resume_uses_same_id_without_resending(self):
        browser, runner = FakeBrowser(self, "interrupted"), FakeRunner(self)
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.seeded(browser, runner).run(once=True)
        pending = read_json(self.runtime / "state.json")
        self.assertEqual(pending["phase"], "review_pending")
        self.relay(browser, runner).run(once=True)
        self.assertEqual(len(browser.calls), 2)
        self.assertEqual(len(browser.sent), 1)
        self.assertFalse(browser.calls[0]["pending"])
        self.assertTrue(browser.calls[1]["pending"])
        self.assertEqual(browser.calls[0]["relay_id"], browser.calls[1]["relay_id"])
        self.assertEqual(len(runner.calls), 1)

    def test_dsh_pending_without_result_never_dispatches_again(self):
        browser, runner = FakeBrowser(self), FakeRunner(self, "interrupted")
        with self.assertRaisesRegex(RuntimeError, "uncertain DSH"):
            self.seeded(browser, runner).run(once=True)
        self.assertEqual(read_json(self.runtime / "state.json")["phase"], "dsh_pending")
        with self.assertRaisesRegex(RelayPaused, "不能自动再次派发"):
            self.relay(browser, runner).run(once=True)
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(len(runner.calls), 1)

    def test_saved_dsh_result_recovers_without_rerunning_worker(self):
        repo = GitRepository(self.config["repository"])
        real_verify = repo.verify_origin
        broken = [False]

        def verify_once():
            if broken[0]:
                broken[0] = False
                raise GitError("Fixture interruption after result persistence")
            return real_verify()

        repo.verify_origin = verify_once
        runner = FakeRunner(self, after_result=lambda: broken.__setitem__(0, True))
        browser = FakeBrowser(self)
        with self.assertRaisesRegex(GitError, "after result"):
            self.seeded(browser, runner, repo).run(once=True)
        pending = read_json(self.runtime / "state.json")
        self.assertTrue((self.runtime / "turns" / pending["task"]["run_id"] / "result.json").is_file())
        self.relay(browser, runner).run(once=True)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(browser.calls), 1)
        self.assertEqual(read_json(self.runtime / "state.json")["completed_cycles"], 1)

    def test_cached_gpt_answer_recovers_without_browser(self):
        repo = GitRepository(self.config["repository"])
        real_verify = repo.verify_task
        calls = [0]

        def verify_once(task, delivery, review_base=None):
            calls[0] += 1
            if calls[0] == 1:
                raise GitError("Fixture interruption after reply persistence")
            return real_verify(task, delivery, review_base=review_base)

        repo.verify_task = verify_once
        relay = self.seeded(FakeBrowser(self), FakeRunner(self), repo)
        with self.assertRaisesRegex(GitError, "after reply"):
            relay.run(once=True)
        pending = read_json(self.runtime / "state.json")
        self.assertTrue((Path(pending["review"]["turn_dir"]) / "gpt-answer.txt").is_file())
        browser = FakeBrowser(self, "never")
        runner = FakeRunner(self)
        self.relay(browser, runner).run(once=True)
        self.assertEqual(browser.calls, [])
        self.assertEqual(len(runner.calls), 1)

    def test_unpushed_dsh_commit_never_becomes_new_review(self):
        browser, runner = FakeBrowser(self), FakeRunner(self, "unpublished")
        with self.assertRaises(GitError):
            self.seeded(browser, runner).run(once=True)
        saved = read_json(self.runtime / "state.json")
        self.assertEqual(saved["phase"], "dsh_pending")
        self.assertEqual(saved["completed_cycles"], 0)
        self.assertEqual(len(browser.calls), 1)

    def test_unpublished_task_does_not_dispatch_dsh(self):
        browser, runner = FakeBrowser(self, "unpublished_task"), FakeRunner(self)
        with self.assertRaises(GitError):
            self.seeded(browser, runner).run(once=True)
        self.assertEqual(runner.calls, [])

    def test_task_file_must_be_updated_and_exist(self):
        for mode in ("unchanged_task", "missing_task"):
            with self.subTest(mode=mode):
                # Each subcase needs an independent run state, but can share the
                # throwaway remote; both published changes remain harmless.
                self.config["runtime_dir"] = str(self.root / ("runtime-" + mode))
                browser, runner = FakeBrowser(self, mode), FakeRunner(self)
                with self.assertRaises(GitError):
                    self.seeded(browser, runner).run(once=True)
                self.assertEqual(runner.calls, [])

    def test_initial_missing_report_or_image_stops_before_web_send(self):
        for name, mutation in (
            ("missing-report", {"report_paths":["Docs/Reports/MISSING.md"]}),
            ("empty-reports", {"report_paths":[]}),
            ("empty-images", {"screenshot_paths":[]}),
            ("not-image", {"screenshot_paths":["Docs/Reports/TASK-001.md"]}),
        ):
            with self.subTest(name=name):
                self.config["runtime_dir"] = str(self.root / ("runtime-" + name))
                browser, runner = FakeBrowser(self), FakeRunner(self)
                relay = self.relay(browser, runner)
                manifest = dict(self.delivery, **mutation)
                relay.seed(manifest)
                with self.assertRaises((RelayPaused, GitError)):
                    relay.run(once=True)
                self.assertEqual(browser.calls, [])
                self.assertEqual(runner.calls, [])

    def test_project_and_conversation_binding_prevents_cross_project_resume(self):
        self.seeded()
        for section, field, value in (("repository", "url", str(self.root / "other.git")),
                                      ("repository", "branch", "other"),
                                      ("browser", "conversation_url", "https://chatgpt.com/c/other"),
                                      ("dsh", "home", str(self.root / "other-home"))):
            with self.subTest(field=field):
                config = deepcopy(self.config)
                config[section][field] = value
                with self.assertRaises(RelayPaused):
                    Relay(config)

    def test_dirty_worker_directory_is_not_overwritten_or_dispatched(self):
        runner = FakeRunner(self)
        relay = self.seeded(runner=runner)
        self.put(self.worker, "private-uncommitted.txt", "keep me")
        with self.assertRaisesRegex(GitError, "未提交"):
            relay.run(once=True)
        self.assertEqual((self.worker / "private-uncommitted.txt").read_text(), "keep me")
        self.assertEqual(runner.calls, [])

    def test_done_and_pause_do_not_start_dsh(self):
        for mode, phase in (("done", "done"), ("pause", "waiting_user")):
            with self.subTest(mode=mode):
                self.config["runtime_dir"] = str(self.root / ("runtime-" + mode))
                runner = FakeRunner(self)
                relay = self.seeded(FakeBrowser(self, mode), runner)
                relay.run(once=True)
                self.assertEqual(relay.state["phase"], phase)
                self.assertEqual(runner.calls, [])

    def test_task_changed_after_publication_is_rejected(self):
        relay = self.seeded()
        published = self.publish({"Docs/Tasks/NEXT.md":"TASK-002 reviewed instructions\n"})
        self.publish({"Docs/Tasks/NEXT.md":"TASK-002 changed after review\n"})
        task = {"published_sha":published, "task_path":"Docs/Tasks/NEXT.md"}
        with self.assertRaises(GitError):
            relay.repo.verify_task(task, self.delivery)

    def test_push_destination_mismatch_is_rejected(self):
        relay = self.seeded()
        other = self.root / "other.git"
        self.git("init", "--bare", str(other), cwd=self.root)
        self.git("remote", "set-url", "--push", "origin", str(other), cwd=self.worker)
        with self.assertRaises(GitError):
            relay.repo.verify_origin()

    def test_task_changed_during_sync_is_not_executed(self):
        repo = GitRepository(self.config["repository"])
        original_sync = repo.sync

        def change_before_sync(expected):
            self.publish({"Docs/Tasks/NEXT.md":"TASK-002 modified during sync\n"})
            return original_sync(expected)

        repo.sync = change_before_sync
        runner = FakeRunner(self)
        relay = self.seeded(runner=runner, repository=repo)
        with self.assertRaises((GitError, RelayPaused)):
            relay.run(once=True)
        self.assertEqual(runner.calls, [])

    def test_lfs_cache_requires_matching_hash_and_size(self):
        repo = self.seeded().repo
        digest = hashlib.sha256(PNG).hexdigest()
        pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(PNG)}\n"
        relative = "Docs/Evidence/LFS.png"
        sha = self.publish({relative:pointer})
        repo.require_published(sha)
        cache = self.worker / ".git" / "lfs" / "objects" / digest[:2] / digest[2:4] / digest
        cache.parent.mkdir(parents=True)
        cache.write_bytes(PNG)
        self.assertEqual(repo.read_file(sha, relative), PNG)
        cache.write_bytes(PNG[:-1] + b"X")
        with self.assertRaisesRegex(GitError, "LFS"):
            repo.read_file(sha, relative)
        cache.write_bytes(PNG + b"extra")
        with self.assertRaisesRegex(GitError, "LFS"):
            repo.read_file(sha, relative)

    def test_lfs_worktree_fallback_requires_matching_hash(self):
        repo = self.seeded().repo
        digest = hashlib.sha256(PNG).hexdigest()
        pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {len(PNG)}\n"
        relative = "Docs/Evidence/LFS-WORKTREE.png"
        sha = self.publish({relative:pointer})
        repo.require_published(sha)
        self.put(self.worker, relative, PNG)
        self.assertEqual(repo.read_file(sha, relative), PNG)
        self.put(self.worker, relative, PNG[:-1] + b"Y")
        with self.assertRaisesRegex(GitError, "LFS"):
            repo.read_file(sha, relative)

    def test_preexisting_task_update_is_not_a_new_publication(self):
        browser, runner = FakeBrowser(self, "preexisting_task"), FakeRunner(self)
        relay = self.seeded(browser, runner)
        self.preexisting_task_sha = self.publish({"Docs/Tasks/NEXT.md":"TASK-OLD published before notification\n"})
        with self.assertRaises(GitError):
            relay.run(once=True)
        saved = read_json(self.runtime / "state.json")
        self.assertEqual(saved["review"]["baseline_sha"], self.preexisting_task_sha)
        self.assertEqual(runner.calls, [])

        # An independent fresh review can publish a genuine newer task. This
        # demonstrates that the baseline gate rejects stale tasks, not all tasks.
        self.config["runtime_dir"] = str(self.root / "runtime-genuine-new-task")
        browser, runner = FakeBrowser(self), FakeRunner(self)
        self.seeded(browser, runner).run(once=True)
        self.assertEqual(len(runner.calls), 1)
        saved = read_json(Path(self.config["runtime_dir"]) / "state.json")
        self.assertEqual(saved["completed_cycles"], 1)

    def test_reply_cache_write_failure_resumes_same_review_without_resend(self):
        browser, runner = FakeBrowser(self), FakeRunner(self)
        exchange = browser.exchange
        replies = {}

        def cached_exchange(prompt, attachments, relay_id, pending=False):
            if pending:
                browser.calls.append({"relay_id":relay_id, "pending":True, "attachments":list(attachments)})
                return replies[relay_id]
            answer = exchange(prompt, attachments, relay_id, pending=False)
            replies[relay_id] = answer
            return answer

        browser.exchange = cached_exchange
        failed = [False]

        def fail_answer_once(path, text):
            if Path(path).name == "gpt-answer.txt" and not failed[0]:
                failed[0] = True
                raise OSError("Fixture answer-cache write interrupted")
            return write_text(path, text)

        relay = self.seeded(browser, runner)
        with patch("relay.engine.write_text", side_effect=fail_answer_once):
            with self.assertRaisesRegex(OSError, "answer-cache"):
                relay.run(once=True)
        saved = read_json(self.runtime / "state.json")
        relay_id = saved["review"]["relay_id"]
        self.assertEqual(saved["phase"], "review_pending")
        self.assertFalse((Path(saved["review"]["turn_dir"]) / "gpt-answer.txt").exists())
        self.assertEqual(runner.calls, [])
        self.relay(browser, runner).run(once=True)
        self.assertEqual(len(browser.calls), 2)
        self.assertEqual(len(browser.sent), 1)
        self.assertEqual([call["relay_id"] for call in browser.calls], [relay_id, relay_id])
        self.assertTrue(browser.calls[-1]["pending"])
        self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
