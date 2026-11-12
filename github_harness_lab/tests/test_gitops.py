"""Controller Git guards exercised with disposable local repositories only."""
from pathlib import Path
import tempfile
import unittest

from lab_harness import gitops
from lab_harness.policy import PolicyError


class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        gitops.git(self.root, "init")
        (self.root / "src").mkdir()
        self.file = self.root / "src" / "value.py"
        self.file.write_text("value = 1\n", encoding="utf-8")
        gitops.git(self.root, "add", "--", "src/value.py")
        gitops.git(self.root, "commit", "-m", "baseline")
        self.base = gitops.git(self.root, "rev-parse", "HEAD")
        self.metadata = gitops.metadata_fingerprint(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_isolated_clone_has_no_origin_or_local_hardlinks(self):
        clone = Path(self.temp.name) / "clone"
        self.assertEqual(gitops.clone_local(self.root, clone, "agent/issue-1"), self.base)
        self.assertEqual(gitops.git(clone, "remote"), "")
        self.assertEqual(gitops.git(clone, "branch", "--show-current"), "agent/issue-1")
        self.assertEqual(gitops.git(clone, "rev-parse", "HEAD"), self.base)
        self.assertFalse((clone / ".git" / "objects" / "info" / "alternates").exists())

    def test_uncertain_existing_clone_is_not_deleted(self):
        clone = Path(self.temp.name) / "clone"
        clone.mkdir()
        marker = clone / "precious.txt"
        marker.write_text("keep")
        with self.assertRaises(PolicyError):
            gitops.clone_local(self.root, clone, "agent/issue-1")
        self.assertEqual(marker.read_text(), "keep")

    def test_clean_metadata_and_dirty_working_tree_are_permitted(self):
        self.file.write_text("value = 2\n", encoding="utf-8")
        gitops.verify_metadata(self.root, self.metadata, self.base)
        self.assertEqual(gitops.changed_files(self.root, self.base), ["src/value.py"])

    def test_assume_unchanged_flag_cannot_hide_forbidden_content(self):
        gitops.git(self.root, "update-index", "--assume-unchanged", "src/value.py")
        self.file.write_text("hidden change\n")
        with self.assertRaisesRegex(PolicyError, "index flags"):
            gitops.verify_metadata(self.root, self.metadata, self.base)

    def test_skip_worktree_flag_is_rejected(self):
        gitops.git(self.root, "update-index", "--skip-worktree", "src/value.py")
        with self.assertRaisesRegex(PolicyError, "index flags"):
            gitops.verify_metadata(self.root, self.metadata, self.base)

    def test_added_ref_cannot_rewrite_or_replace_controller_history(self):
        gitops.git(self.root, "update-ref", "refs/replace/" + self.base, self.base)
        with self.assertRaisesRegex(PolicyError, "metadata"):
            gitops.verify_metadata(self.root, self.metadata, self.base)

    def test_added_hook_is_rejected_before_controller_commit(self):
        (self.root / ".git" / "hooks" / "post-commit").write_text("unexpected hook\n")
        with self.assertRaisesRegex(PolicyError, "metadata"):
            gitops.verify_metadata(self.root, self.metadata, self.base)

    def test_corrupt_git_object_is_not_accepted_as_valid_evidence(self):
        blob = gitops.git(self.root, "rev-parse", "HEAD:src/value.py")
        path = self.root / ".git" / "objects" / blob[:2] / blob[2:]
        path.chmod(0o600)
        path.write_bytes(b"corrupt object fixture")
        with self.assertRaises(PolicyError):
            gitops.verify_metadata(self.root, self.metadata, self.base)

    def test_commit_revalidates_scope_even_if_caller_skipped_prior_check(self):
        (self.root / "outside.txt").write_text("outside")
        task = {"allowed_paths": ["src"], "forbidden_paths": []}
        with self.assertRaises(PolicyError):
            gitops.commit(self.root, ["outside.txt"], "TASK-1", task=task)
        self.assertEqual(gitops.git(self.root, "rev-parse", "HEAD"), self.base)

    def test_commit_requires_exact_change_set_and_verified_blob_bytes(self):
        self.file.write_text("value = 2\n", encoding="utf-8")
        task = {"allowed_paths": ["src"], "forbidden_paths": []}
        result = gitops.commit(self.root, ["src/value.py"], "TASK-1", task=task)
        self.assertNotEqual(result, self.base)
        self.assertEqual(gitops.git(self.root, "show", "HEAD:src/value.py"), "value = 2")
        self.assertEqual(gitops.git(self.root, "rev-parse", "HEAD^"), self.base)

    def test_git_z_listing_preserves_leading_whitespace_for_policy(self):
        (self.root / " src.py").write_text("fixture")
        self.assertIn(" src.py", gitops.changed_files(self.root, self.base))


if __name__ == "__main__":
    unittest.main()
