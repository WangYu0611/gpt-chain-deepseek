"""Real local-Git and OS-lock checks; these never contact a model or network."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unattended import gitops
from unattended.storage import lock, read_json, write_json


class LocalGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "source"
        self.project.mkdir()
        gitops.git(self.project, "init", "--initial-branch=main")
        gitops.git(self.project, "config", "user.name", "Offline Test")
        gitops.git(self.project, "config", "user.email", "offline@example.invalid")
        self.commit(self.project, "README.txt", "base\n", "Initial")
        self.base = gitops.head(self.project)

    def tearDown(self):
        # Git on Windows marks object files read-only; reset only this owned
        # temporary directory's file attributes before TemporaryDirectory cleanup.
        if os.name == "nt":
            for path in self.root.rglob("*"):
                if path.is_file():
                    path.chmod(0o666)
        self.temporary.cleanup()

    @staticmethod
    def commit(project, path, content, title):
        target = project / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        gitops.git(project, "add", "--", path)
        gitops.git(project, "commit", "-m", title)
        return gitops.head(project)

    def test_candidate_requires_real_clean_commits_bound_to_task(self):
        with self.assertRaisesRegex(RuntimeError, "尚未产生"):
            gitops.verify_candidate(self.project, self.base, "TASK-0001")
        candidate = self.commit(self.project, "code.txt", "implemented\n", "TASK-0001")
        evidence = gitops.verify_candidate(self.project, self.base, "TASK-0001")
        self.assertEqual(evidence["head_sha"], candidate)
        self.assertEqual(evidence["base_sha"], self.base)
        self.assertEqual(evidence["commits"], [candidate])
        self.assertEqual(evidence["files"], ["code.txt"])
        (self.project / "extra.txt").write_text("not committed", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "未提交"):
            gitops.verify_candidate(self.project, self.base, "TASK-0001")

    def test_candidate_rejects_a_commit_for_another_task(self):
        self.commit(self.project, "code.txt", "implemented", "TASK-0002")
        with self.assertRaisesRegex(RuntimeError, "标题"):
            gitops.verify_candidate(self.project, self.base, "TASK-0001")

    def test_candidate_rejects_merge_commits(self):
        gitops.git(self.project, "checkout", "-b", "side")
        self.commit(self.project, "side.txt", "side", "TASK-0001")
        gitops.git(self.project, "checkout", "main")
        self.commit(self.project, "main.txt", "main", "TASK-0001")
        gitops.git(self.project, "merge", "--no-ff", "side", "-m", "TASK-0001")
        with self.assertRaisesRegex(RuntimeError, "合并"):
            gitops.verify_candidate(self.project, self.base, "TASK-0001")

    def test_candidate_rejects_unrelated_history(self):
        gitops.git(self.project, "checkout", "--orphan", "unrelated")
        self.commit(self.project, "new.txt", "new root", "TASK-0001")
        with self.assertRaises(RuntimeError):
            gitops.verify_candidate(self.project, self.base, "TASK-0001")

    def test_publish_reconciles_an_already_pushed_head(self):
        remote = self.root / "remote.git"
        remote.mkdir()
        gitops.git(remote, "init", "--bare")
        gitops.git(self.project, "remote", "add", "origin", str(remote))
        candidate = self.commit(self.project, "code.txt", "implemented", "TASK-0001")
        config = {"branch": "agent/serial", "remote": "origin"}
        first = gitops.publish(self.project, config, candidate)
        self.assertFalse(first["already_published"])
        again = gitops.publish(self.project, config, candidate)
        self.assertTrue(again["already_published"])
        self.assertEqual(gitops.git(remote, "rev-parse", "refs/heads/agent/serial"), candidate)

    def test_publish_never_force_overwrites_remote_work(self):
        remote = self.root / "remote.git"
        remote.mkdir()
        gitops.git(remote, "init", "--bare")
        gitops.git(self.project, "remote", "add", "origin", str(remote))
        first = self.commit(self.project, "code.txt", "first", "TASK-0001")
        gitops.publish(self.project, {"branch": "agent/serial"}, first)
        gitops.git(self.project, "checkout", "-b", "divergent", self.base)
        second = self.commit(self.project, "other.txt", "other", "TASK-0002")
        with self.assertRaises(RuntimeError):
            gitops.publish(self.project, {"branch": "agent/serial"}, second)
        self.assertEqual(gitops.git(remote, "rev-parse", "refs/heads/agent/serial"), first)

    def test_clone_preserves_source_edits_and_resumes_its_own_progress(self):
        (self.project / "README.txt").write_text("user's unfinished edit\n", encoding="utf-8")
        (self.project / "untracked.txt").write_text("user's untracked file\n", encoding="utf-8")
        config = {"project_path": str(self.project), "runtime_dir": str(self.root / "runtime"),
                  "branch": "unattended/offline", "base_ref": "HEAD"}
        work = gitops.ensure_workspace(config)
        self.assertTrue((work / ".git").is_dir())
        self.assertEqual((work / "README.txt").read_text(encoding="utf-8"), "base\n")
        self.assertFalse((work / "untracked.txt").exists())
        self.assertEqual((self.project / "README.txt").read_text(encoding="utf-8"), "user's unfinished edit\n")
        self.assertEqual(gitops.git(self.project, "symbolic-ref", "--short", "HEAD"), "main")
        candidate = self.commit(work, "code.txt", "implemented", "TASK-0001")
        self.assertEqual(gitops.ensure_workspace(config), work)
        self.assertEqual(gitops.head(work), candidate)
        self.assertEqual(gitops.head(self.project), self.base)

    def test_clone_copies_configured_remote_and_local_commit_identity(self):
        remote = self.root / "remote.git"
        remote.mkdir()
        gitops.git(remote, "init", "--bare")
        gitops.git(self.project, "remote", "add", "upstream", str(remote))
        config = {"project_path": str(self.project), "runtime_dir": str(self.root / "runtime"),
                  "branch": "unattended/offline", "base_ref": "HEAD", "remote": "upstream"}
        work = gitops.ensure_workspace(config)
        self.assertEqual(gitops.git(work, "remote").splitlines(), ["upstream"])
        self.assertEqual(gitops.git(work, "remote", "get-url", "upstream"), str(remote))
        self.assertEqual(gitops.git(work, "config", "--get", "user.name"), "Offline Test")
        self.assertEqual(gitops.git(work, "config", "--get", "user.email"), "offline@example.invalid")

    def test_resume_rejects_external_branch_changes(self):
        config = {"project_path": str(self.project), "runtime_dir": str(self.root / "runtime"),
                  "branch": "unattended/offline", "base_ref": "HEAD"}
        work = gitops.ensure_workspace(config)
        gitops.git(work, "checkout", "-b", "external")
        with self.assertRaisesRegex(RuntimeError, "工作分支"):
            gitops.ensure_workspace(config)


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_failed_atomic_replace_preserves_the_previous_checkpoint(self):
        path = self.root / "state.json"
        write_json(path, {"phase": "coding", "task": "TASK-0001"})
        with patch("unattended.storage.os.replace", side_effect=OSError("simulated interrupted replacement")):
            with self.assertRaises(OSError):
                write_json(path, {"phase": "review", "task": "TASK-0001"})
        self.assertEqual(read_json(path), {"phase": "coding", "task": "TASK-0001"})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_project_lock_blocks_another_process_and_releases_after_crash(self):
        path = self.root / "project.lock"
        code = (
            "from pathlib import Path\n"
            "import sys\n"
            "from unattended.storage import lock\n"
            "with lock(Path(sys.argv[1])):\n"
            " print('locked', flush=True)\n"
            " sys.stdin.read(1)\n"
        )
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(path)],
                                   cwd=str(Path(__file__).resolve().parents[1]),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises((RuntimeError, OSError)):
                with lock(path):
                    self.fail("A second process acquired the owned project lock")
        finally:
            process.terminate()
            process.communicate(timeout=10)
        with lock(path):
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
