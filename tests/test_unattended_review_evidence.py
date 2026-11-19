"""Read-only API evidence boundaries, commit binding and redaction regressions."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from unattended.review_evidence import ReviewEvidence
from unattended.secrets import secret_values


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8j8AAAAASUVORK5CYII=")


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.out = self.runtime / "runs/review-current"
        self.out.mkdir(parents=True)
        self.shared = self.root / "secrets.local.json"
        self.shared.write_text(json.dumps({"CODEX_API_KEY": "synthetic-primary-secret",
                                           "CODEX_API_KEY_BACKUPS": ["synthetic-backup-secret"],
                                           "TRIPO_API_KEY": "synthetic-tripo-secret",
                                           "FEISHU_WEBHOOK_URL": "https://bot.example/private-hook",
                                           "CODEX_BASE_URL": "https://api.example.test/v1"}), encoding="utf-8")
        self.config = {"runtime_dir": str(self.runtime), "secrets_file": str(self.shared)}
        self.git("init", "--quiet")
        self.git("config", "user.email", "evidence@example.invalid")
        self.git("config", "user.name", "Evidence tests")
        self.write_repo("Source/Game/Private/Foo.cpp", "int value = 1;\n")
        self.write_repo(".env", "SENSITIVE=synthetic-secret\n")
        self.write_repo("gpt_api.txt", "synthetic-secret\n")
        self.write_repo("docs/evidence/example.png", PNG)
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.write_repo("Source/Game/Private/Foo.cpp", "int value = 2;\n")
        self.git("commit", "--quiet", "-am", "candidate")
        self.head = self.git("rev-parse", "HEAD")
        self.state = {"phase": "REVIEW", "task": {"base_sha": self.base, "candidate": {"head_sha": self.head}}}
        self.write_state()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, check=True)
        return result.stdout.decode("utf-8").strip()

    def write_repo(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        return target

    def write_state(self):
        (self.runtime / "state.json").write_text(json.dumps(self.state), encoding="utf-8")

    def read(self, evidence, scope, path, offset=0, limit=32000):
        return evidence.run("read_file", {"scope": scope, "path": str(path), "offset": offset, "limit": limit})

    def test_pinned_repo_blob_and_standard_ue_private_sources_are_readable(self):
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        path = self.repo / "Source/Game/Private/Foo.cpp"
        self.assertEqual(self.read(evidence, "repo", path)["content"], "int value = 2;\n")
        path.write_text("uncommitted replacement", encoding="utf-8")
        self.assertEqual(self.read(evidence, "repo", "Source/Game/Private/Foo.cpp")["content"], "int value = 2;\n")
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            evidence.verify()

    def test_repository_secrets_untracked_files_and_path_escapes_are_unavailable(self):
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        for path in (".env", "gpt_api.txt", ".git/config", "../secrets.local.json", "untracked.txt"):
            with self.subTest(path=path):
                result = self.read(evidence, "repo", path)
                self.assertIn("error", result)
                self.assertNotIn("synthetic-secret", json.dumps(result))
        listed = evidence.run("list_files", {"scope": "repo", "path": "", "offset": 0, "limit": 200})
        self.assertNotIn(".env", listed["files"])
        self.assertNotIn("gpt_api.txt", listed["files"])

    def test_runtime_evidence_is_pinned_when_consumed_and_state_heartbeats_can_change(self):
        path = self.runtime / "checks/unit/stdout.log"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"Test failed\n")
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        self.assertEqual(self.read(evidence, "runtime", path)["content"], "Test failed\n")
        self.state["heartbeat"] = "later"
        self.write_state()
        evidence.verify()
        path.write_text("Test passed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            evidence.verify()

    def test_runtime_scope_excludes_configs_private_storage_and_current_tool_output(self):
        for name in ("config.json", "private/token.txt", "runs/review-current/transcript.json",
                     "checks/private/captured.txt", "runs/old/workspace/source.cpp"):
            target = self.runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("private-value", encoding="utf-8")
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        for name in ("config.json", "private/token.txt", "runs/review-current/transcript.json",
                     "checks/private/captured.txt", "runs/old/workspace/source.cpp"):
            with self.subTest(name=name):
                self.assertIn("error", self.read(evidence, "runtime", name))
        listed = evidence.run("list_files", {"scope": "runtime", "path": "", "offset": 0, "limit": 200})
        self.assertEqual(listed["files"], [])

    def test_known_archived_and_non_codex_keys_are_redacted_before_pagination(self):
        path = self.runtime / "tasks/task-1/report.log"
        path.parent.mkdir(parents=True)
        text = "synthetic-backup-secret synthetic-tripo-secret https://bot.example/private-hook https://api.example.test/v1"
        path.write_text(text, encoding="utf-8")
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        pieces = []
        offset = 0
        while True:
            page = self.read(evidence, "runtime", path, offset=offset, limit=7)
            pieces.append(page["content"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        redacted = "".join(pieces)
        self.assertNotIn("synthetic-backup-secret", redacted)
        self.assertNotIn("synthetic-tripo-secret", redacted)
        self.assertNotIn("private-hook", redacted)
        self.assertIn("https://api.example.test/v1", redacted)
        self.assertIn("synthetic-backup-secret", secret_values(secrets_file=self.shared))
        self.assertNotIn("https://api.example.test/v1", secret_values(secrets_file=self.shared))

    def test_diff_uses_controller_base_across_multiple_commits_and_ignores_external_diff(self):
        self.write_repo("later.txt", "later change\n")
        self.git("add", "later.txt")
        self.git("commit", "--quiet", "-m", "later")
        self.state["task"]["candidate"]["head_sha"] = self.git("rev-parse", "HEAD")
        self.write_state()
        self.git("config", "diff.external", "nonexistent-external-diff-helper")
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        result = evidence.run("git_diff", {"path": "", "offset": 0, "limit": 32000})
        self.assertEqual(result["base_sha"], self.base)
        self.assertIn("Source/Game/Private/Foo.cpp", result["files"])
        self.assertIn("+int value = 2;", result["content"])
        self.assertIn("+later change", result["content"])

    def test_first_commit_diff_needs_no_empty_tree_object_or_repository_write(self):
        self.git("checkout", "--quiet", "--detach", self.base)
        (self.runtime / "state.json").unlink()
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        result = evidence.run("git_diff", {"path": "Source/Game/Private/Foo.cpp", "offset": 0, "limit": 32000})
        self.assertIn("+int value = 1;", result["content"])
        evidence.verify()

    def test_controller_candidate_and_later_head_changes_fail_binding(self):
        self.state["task"]["candidate"]["head_sha"] = self.base
        self.write_state()
        with self.assertRaisesRegex(ValueError, "candidate"):
            ReviewEvidence(self.config, self.repo, self.out)
        self.state["task"]["candidate"]["head_sha"] = self.head
        self.write_state()
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        self.git("checkout", "--quiet", "--detach", self.base)
        with self.assertRaisesRegex(ValueError, "HEAD changed"):
            evidence.verify()

    def test_evidence_images_require_approved_scope_and_preserve_native_image_data(self):
        task = self.runtime / "tasks/task-1/screenshot.png"
        task.parent.mkdir(parents=True)
        task.write_bytes(PNG)
        other = self.runtime / "runs/old/screenshot.png"
        other.parent.mkdir(parents=True)
        other.write_bytes(PNG)
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        for scope, path in (("runtime", str(task)), ("repo", "docs/evidence/example.png")):
            image = evidence.run("read_image", {"scope": scope, "path": path})
            self.assertEqual(base64.b64decode(image["image_url"].split(",", 1)[1]), PNG)
        self.assertIn("error", evidence.run("read_image", {"scope": "runtime", "path": str(other)}))

    def test_linked_runtime_evidence_is_denied(self):
        target = self.runtime / "tasks/task-1/linked.txt"
        target.parent.mkdir(parents=True)
        try:
            os.symlink(self.shared, target)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic link creation is unavailable")
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        self.assertIn("error", self.read(evidence, "runtime", target))

    def test_tool_schemas_and_pagination_are_enforced(self):
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        for tool in evidence.tools:
            self.assertTrue(tool["strict"])
            self.assertFalse(tool["parameters"]["additionalProperties"])
            self.assertEqual(set(tool["parameters"]["properties"]), set(tool["parameters"]["required"]))
        self.assertIn("error", evidence.run("read_file", {"scope": "repo", "path": "later.txt", "offset": True, "limit": 10}))
        self.assertIn("error", evidence.run("shell", {"command": "write"}))
        page = evidence.run("git_diff", {"path": "Source/Game/Private/Foo.cpp", "offset": 0, "limit": 10})
        self.assertTrue(page["truncated"])
        self.assertEqual(page["next_offset"], 10)

    def test_literal_search_reports_line_numbers_and_excludes_private_content(self):
        evidence = ReviewEvidence(self.config, self.repo, self.out)
        result = evidence.run("search_files", {"scope": "repo", "path": "", "query": "int value", "case_sensitive": True,
                                                "offset": 0, "limit": 10})
        self.assertEqual(result["matches"], [{"path": "Source/Game/Private/Foo.cpp", "line": 1,
                                              "text": "int value = 2;", "line_truncated": False}])


if __name__ == "__main__":
    unittest.main()
