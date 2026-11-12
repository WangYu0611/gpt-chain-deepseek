import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lab_harness import process as process_module
from lab_harness.process import run_process, safe_environment


class ProcessTests(unittest.TestCase):
    def run_code(self, code, **kwargs):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        return run_process([sys.executable, "-c", code], cwd=directory,
                           output_dir=directory / "logs", timeout_seconds=kwargs.pop("timeout_seconds", 10),
                           environment=safe_environment(), **kwargs)

    def test_environment_never_inherits_publisher_keys(self):
        env = safe_environment({"PATH": "example", "GITHUB_TOKEN": "secret", "GH_TOKEN": "secret", "AWS_SECRET_ACCESS_KEY": "secret"})
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["PATH"], "example")
        with self.assertRaises(ValueError):
            safe_environment(extra={"GITHUB_TOKEN": "secret"})

    def test_stdin_and_exit_are_real(self):
        result = self.run_code("import sys; print(sys.stdin.read()); sys.exit(7)", stdin=b"safe input")
        self.assertEqual(result.exit_code, 7)
        self.assertIn("safe input", result.stdout_path.read_text())

    def test_secret_redaction(self):
        result = self.run_code("import sys; sys.stdout.write('top-'+'secret-value'); sys.stderr.write('top-secret-value')",
                               secrets=["top-secret-value"])
        self.assertEqual(result.stdout_path.read_text(), "[REDACTED]")
        self.assertEqual(result.stderr_path.read_text(), "[REDACTED]")

    def test_secret_split_between_pipe_reads_is_redacted(self):
        result = self.run_code("import sys,time; sys.stdout.write('x'*65530+'top-'); sys.stdout.flush(); time.sleep(.05); sys.stdout.write('secret-value'); sys.stdout.flush()",
                               secrets=["top-secret-value"])
        self.assertEqual(result.stdout_path.read_text(), "x" * 65530 + "[REDACTED]")

    def test_timeout(self):
        result = self.run_code("import time; time.sleep(10)", timeout_seconds=0.15)
        self.assertTrue(result.timed_out)
        # Windows Job close can report code 0 even though the process was killed.
        # The outer worker must independently reject the timeout flag.

    def test_timeout_terminates_descendants(self):
        import time
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',\"import time; time.sleep(0.8); open('child-marker','w').write('bad')\"]); time.sleep(10)"
        result = self.run_code(code, timeout_seconds=0.3)
        self.assertTrue(result.timed_out)
        time.sleep(0.9)
        self.assertFalse((Path(self.temp.name) / "child-marker").exists())

    def test_timeout_unblocks_a_large_pending_stdin_write(self):
        # A child that never reads stdin must not keep the writer thread alive
        # after the supervisor cancels its process tree.
        result = self.run_code("import time; time.sleep(10)", timeout_seconds=0.15,
                               stdin=b"x" * 1_000_000)
        self.assertTrue(result.timed_out)

    def test_output_limit(self):
        result = self.run_code("import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(5)",
                               max_output_bytes=2048)
        self.assertTrue(result.output_limit_exceeded)
        self.assertLessEqual(result.stdout_path.stat().st_size + result.stderr_path.stat().st_size, 2048)

    def test_no_cmd_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "cmd/bat"):
                run_process(["dsh.cmd"], cwd=Path(directory), output_dir=Path(directory),
                            timeout_seconds=1, environment={})


@unittest.skipUnless(os.name == "nt", "Windows Job containment")
class WindowsLaunchTests(unittest.TestCase):
    run_code = ProcessTests.run_code

    def thread_kernel(self, owners, *, actual_owner=123, suspended=1):
        entries = iter(enumerate(owners, 200))

        def next_entry(snapshot, pointer):
            try:
                thread_id, owner = next(entries)
            except StopIteration:
                return False
            pointer._obj.th32ThreadID = thread_id
            pointer._obj.th32OwnerProcessID = owner
            return True

        return SimpleNamespace(
            CreateToolhelp32Snapshot=Mock(return_value=100),
            Thread32First=Mock(side_effect=next_entry), Thread32Next=Mock(side_effect=next_entry),
            OpenThread=Mock(return_value=101), GetProcessIdOfThread=Mock(return_value=actual_owner),
            ResumeThread=Mock(return_value=suspended), CloseHandle=Mock(return_value=True))

    def test_thread_scan_rejects_missing_or_ambiguous_initial_thread(self):
        for owners in ([456], [123, 123]):
            with self.subTest(owners=owners):
                kernel = self.thread_kernel(owners)
                with patch("ctypes.WinDLL", return_value=kernel), patch("ctypes.get_last_error", return_value=18):
                    with self.assertRaisesRegex(RuntimeError, "exactly one"):
                        process_module._resume_windows_process(SimpleNamespace(pid=123))
                kernel.CloseHandle.assert_called_once_with(100)
                kernel.OpenThread.assert_not_called()
                kernel.ResumeThread.assert_not_called()

    def test_thread_handle_owner_is_checked_before_resuming(self):
        kernel = self.thread_kernel([123], actual_owner=456)
        with patch("ctypes.WinDLL", return_value=kernel), patch("ctypes.get_last_error", return_value=18):
            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                process_module._resume_windows_process(SimpleNamespace(pid=123))
        self.assertEqual([call.args[0] for call in kernel.CloseHandle.call_args_list], [100, 101])
        kernel.ResumeThread.assert_not_called()

    def test_resume_requires_exactly_one_prior_suspend_and_closes_handles(self):
        for count in (0, 2, 0xFFFFFFFF):
            with self.subTest(suspended=count):
                kernel = self.thread_kernel([456, 123], suspended=count)
                with patch("ctypes.WinDLL", return_value=kernel), patch("ctypes.get_last_error", return_value=18):
                    with self.assertRaises((RuntimeError, OSError)):
                        process_module._resume_windows_process(SimpleNamespace(pid=123))
                self.assertEqual([call.args[0] for call in kernel.CloseHandle.call_args_list], [100, 101])
                kernel.OpenThread.assert_called_once_with(0x0802, False, 201)
                kernel.ResumeThread.assert_called_once_with(101)

    def test_delayed_assignment_cannot_run_or_exit_the_root(self):
        assign = process_module._WindowsJob.assign

        def delayed(job, process):
            time.sleep(0.15)
            self.assertIsNone(process.poll())
            self.assertFalse((Path(self.temp.name) / "entered").exists())
            assign(job, process)

        with patch.object(process_module._WindowsJob, "assign", delayed):
            result = self.run_code("from pathlib import Path; Path('entered').write_text('started')")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual((Path(self.temp.name) / "entered").read_text(), "started")

    def test_assignment_failure_never_runs_the_root_and_reaps_it(self):
        created = []
        popen = process_module.subprocess.Popen

        def capture(*args, **kwargs):
            child = popen(*args, **kwargs)
            created.append(child)
            return child

        with patch.object(process_module.subprocess, "Popen", capture), \
             patch.object(process_module._WindowsJob, "assign", side_effect=OSError(5, "assignment denied")):
            with self.assertRaisesRegex(OSError, "assignment denied"):
                self.run_code("from pathlib import Path; Path('escaped').write_text('bad')")
        self.assertFalse((Path(self.temp.name) / "escaped").exists())
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)
        self.assertTrue(all(stream.closed for stream in (created[0].stdin, created[0].stdout, created[0].stderr)))

    def test_resume_failure_closes_job_and_reaps_suspended_root(self):
        created = []
        assign = process_module._WindowsJob.assign

        def capture(job, child):
            assign(job, child)
            created.append((job, child))

        with patch.object(process_module._WindowsJob, "assign", capture), \
             patch.object(process_module, "_resume_windows_process", side_effect=OSError(5, "resume denied")):
            with self.assertRaisesRegex(OSError, "resume denied"):
                self.run_code("from pathlib import Path; Path('escaped').write_text('bad')")
        self.assertFalse((Path(self.temp.name) / "escaped").exists())
        self.assertEqual(len(created), 1)
        job, child = created[0]
        self.assertIsNone(job.handle)
        self.assertIsNotNone(child.returncode)

    def test_fast_git_commands_remain_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(50):
                result = run_process(["git", "--version"], cwd=root, output_dir=root / str(index),
                                     timeout_seconds=10, environment=safe_environment())
                self.assertEqual(result.exit_code, 0)
                self.assertTrue(result.stdout_path.read_text().startswith("git version "))
                self.assertFalse(result.timed_out)

    def test_fast_root_exit_still_terminates_its_descendant(self):
        child = "import time; time.sleep(.7); open('orphan-marker','w').write('bad')"
        code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
        result = self.run_code(code)
        self.assertEqual(result.exit_code, 0)
        time.sleep(0.9)
        self.assertFalse((Path(self.temp.name) / "orphan-marker").exists())

    def test_worker_inside_a_job_can_launch_a_nested_contained_process(self):
        package_root = str(Path(process_module.__file__).resolve().parent.parent)
        code = (
            "import sys; from pathlib import Path; sys.path.insert(0," + repr(package_root) + "); "
            "from lab_harness.process import run_process,safe_environment; "
            "r=run_process([sys.executable,'-c',\"print('nested execution')\"],cwd=Path.cwd(),"
            "output_dir=Path('inner'),timeout_seconds=10,environment=safe_environment()); "
            "assert r.exit_code==0; print(r.stdout_path.read_text())"
        )
        result = self.run_code(code)
        self.assertEqual(result.exit_code, 0, result.stderr_path.read_text())
        self.assertIn("nested execution", result.stdout_path.read_text())


if __name__ == "__main__":
    unittest.main()
