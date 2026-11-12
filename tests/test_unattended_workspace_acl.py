"""Managed clone ACL scope, identity and working-window deadline regressions."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unattended import gitops


@unittest.skipUnless(os.name == "nt", "Windows workspace ACL support")
class WorkspaceAclTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.work = self.runtime / ("workspace.build-" + "a" * 32)
        (self.work / ".git").mkdir(parents=True)
        self.sid = "S-1-5-21-1001"
        self.error = self.root / "stderr.log"
        self.error.write_text("", encoding="utf-8")

    def fake_result(self, **updates):
        values = {"exit_code": 0, "timed_out": False, "output_limit_exceeded": False, "stderr_path": self.error}
        values.update(updates)
        return SimpleNamespace(**values)

    def test_grants_inheritable_full_control_only_to_current_owner_on_new_clone(self):
        native = self.root / "system32" / "icacls.exe"
        with patch.object(gitops, "_windows_user_sid", return_value=self.sid), \
             patch.object(gitops, "_windows_owner_sid", return_value=self.sid), \
             patch.object(gitops, "_windows_icacls", return_value=native), \
             patch.object(gitops, "run_process", return_value=self.fake_result()) as run:
            gitops._ensure_windows_workspace_access(self.work, self.runtime)
        argv = run.call_args.args[0]
        self.assertEqual(argv, [str(native), str(self.work), "/grant:r", "*" + self.sid + ":(OI)(CI)F", "/Q"])
        self.assertNotIn("/T", argv)
        self.assertEqual(run.call_args.kwargs["cwd"], self.work)
        self.assertLessEqual(run.call_args.kwargs["timeout_seconds"], 30)

    def test_rejects_source_parent_and_unmanaged_paths_without_running_icacls(self):
        source = self.root / "source"
        (source / ".git").mkdir(parents=True)
        for target in (source, self.runtime, self.runtime / "workspace", self.work / "nested"):
            with self.subTest(target=target), patch.object(gitops, "run_process") as run:
                with self.assertRaisesRegex(RuntimeError, "刚创建"):
                    gitops._ensure_windows_workspace_access(target, self.runtime)
                run.assert_not_called()

    def test_rejects_foreign_owner_instead_of_taking_ownership(self):
        with patch.object(gitops, "_windows_user_sid", return_value=self.sid), \
             patch.object(gitops, "_windows_owner_sid", return_value="S-1-5-21-9999"), \
             patch.object(gitops, "run_process") as run:
            with self.assertRaisesRegex(RuntimeError, "所有者不是当前用户"):
                gitops._ensure_windows_workspace_access(self.work, self.runtime)
        run.assert_not_called()

    def test_junction_is_rejected_before_any_permission_mutation(self):
        with patch.object(Path, "is_junction", return_value=True), patch.object(gitops, "run_process") as run:
            with self.assertRaisesRegex(RuntimeError, "刚创建"):
                gitops._ensure_windows_workspace_access(self.work, self.runtime)
        run.assert_not_called()

    def test_acl_deadline_stops_before_mutation_when_window_already_closed(self):
        with patch.object(gitops, "_windows_user_sid", return_value=self.sid), \
             patch.object(gitops, "_windows_owner_sid", return_value=self.sid), \
             patch.object(gitops, "run_process") as run:
            with gitops.deadline(0), self.assertRaises(gitops.WindowClosed):
                gitops._ensure_windows_workspace_access(self.work, self.runtime)
        run.assert_not_called()

    def test_acl_process_timeout_is_limited_to_work_window_and_preserves_window_status(self):
        with patch.object(gitops, "_windows_user_sid", return_value=self.sid), \
             patch.object(gitops, "_windows_owner_sid", return_value=self.sid), \
             patch.object(gitops, "_windows_icacls", return_value=self.root / "icacls.exe"), \
             patch.object(gitops, "run_process", return_value=self.fake_result(timed_out=True)) as run:
            with gitops.deadline(0.5), self.assertRaises(gitops.WindowClosed):
                gitops._ensure_windows_workspace_access(self.work, self.runtime)
        self.assertGreater(run.call_args.kwargs["timeout_seconds"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout_seconds"], 0.5)

    def test_actual_native_grant_adds_explicit_full_control_on_owned_temporary_clone(self):
        owner = gitops._windows_user_sid()
        self.assertEqual(gitops._windows_owner_sid(self.work), owner)
        gitops._ensure_windows_workspace_access(self.work, self.runtime)
        self.assertEqual(gitops._windows_owner_sid(self.work), owner)
        with tempfile.TemporaryDirectory(prefix="unattended-acl-inspect-") as scratch:
            result = gitops.run_process([str(gitops._windows_icacls()), str(self.work)], cwd=self.work,
                output_dir=Path(scratch), environment=dict(os.environ), timeout_seconds=10)
            self.assertEqual(result.exit_code, 0)
            output = result.stdout_path.read_text(encoding="utf-8", errors="replace")
        self.assertTrue(any("(OI)(CI)(F)" in line and "(I)" not in line for line in output.splitlines()), output)


if __name__ == "__main__":
    unittest.main()
