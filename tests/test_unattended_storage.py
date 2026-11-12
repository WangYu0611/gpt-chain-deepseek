"""Atomic checkpoint replacement survives short Windows sharing conflicts."""
import errno
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from unattended import storage


def windows_error(code):
    error = PermissionError(errno.EACCES, "simulated Windows sharing conflict")
    error.winerror = code
    return error


class AtomicStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "state.json"
        self.target.write_bytes(b'{"phase":"old"}\n')

    def test_known_windows_conflicts_reuse_one_flushed_file_and_keep_old_content_until_success(self):
        real_replace = storage.os.replace
        sources, bytes_seen, target_seen = [], [], []
        failures = [windows_error(code) for code in (5, 32, 33)]
        def replace(source, destination):
            sources.append(source)
            bytes_seen.append(Path(source).read_bytes())
            target_seen.append(self.target.read_bytes())
            self.assertEqual(Path(destination), self.target)
            if failures:
                raise failures.pop(0)
            return real_replace(source, destination)
        with patch.object(storage.os, "replace", side_effect=replace) as replacement, \
             patch.object(storage.time, "sleep") as sleep, \
             patch.object(storage.os, "fsync", wraps=storage.os.fsync) as fsync:
            storage.write_json(self.target, {"phase": "new", "name": "断点"})
        self.assertEqual(replacement.call_count, 4)
        self.assertEqual(len(set(sources)), 1)
        self.assertEqual(len(set(bytes_seen)), 1)
        self.assertEqual(target_seen, [b'{"phase":"old"}\n'] * 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.02, 0.05, 0.1])
        fsync.assert_called_once()
        self.assertEqual(storage.read_json(self.target), {"phase": "new", "name": "断点"})
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ["state.json"])

    def test_lasting_windows_access_denial_is_bounded_preserves_target_and_cleans_temporary(self):
        error = windows_error(5)
        with patch.object(storage.os, "replace", side_effect=error) as replacement, \
             patch.object(storage.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as caught:
                storage.write_text(self.target, "new checkpoint")
        self.assertIs(caught.exception, error)
        self.assertEqual(replacement.call_count, 8)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0])
        self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 3)
        self.assertEqual(self.target.read_bytes(), b'{"phase":"old"}\n')
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ["state.json"])

    def test_unrelated_windows_and_non_windows_errors_are_not_retried(self):
        for error in (windows_error(2), PermissionError(errno.EACCES, "POSIX permission denied"),
                      OSError(errno.ENOSPC, "out of space")):
            with self.subTest(winerror=getattr(error, "winerror", None), errno=error.errno), \
                 patch.object(storage.os, "replace", side_effect=error) as replacement, \
                 patch.object(storage.time, "sleep") as sleep:
                with self.assertRaises(OSError) as caught:
                    storage.write_text(self.target, "new checkpoint")
                self.assertIs(caught.exception, error)
                replacement.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(self.target.read_bytes(), b'{"phase":"old"}\n')
                self.assertEqual(sorted(item.name for item in self.root.iterdir()), ["state.json"])

    def test_non_os_error_cannot_enable_retry_by_carrying_winerror(self):
        error = RuntimeError("not a Windows IO failure")
        error.winerror = 5
        self.assertFalse(storage.is_windows_sharing_error(error))
        error = windows_error(True)
        self.assertFalse(storage.is_windows_sharing_error(error))

    @unittest.skipUnless(os.name == "nt", "Exercises Windows delete-sharing semantics")
    def test_real_windows_reader_without_delete_sharing_releases_before_atomic_commit(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        # An ordinary reader may permit more reads/writes but omit FILE_SHARE_DELETE.
        handle = kernel.CreateFileW(str(self.target), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            self.fail("Could not open actual Windows reader fixture: " + str(ctypes.get_last_error()))
        observed = threading.Event()
        failures = []
        real_replace = storage.os.replace
        def replace(source, destination):
            try:
                return real_replace(source, destination)
            except OSError as error:
                if storage.is_windows_sharing_error(error):
                    observed.set()
                raise
        def writer():
            try:
                storage.write_json(self.target, {"phase": "committed after reader closed"})
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=writer)
        try:
            with patch.object(storage.os, "replace", side_effect=replace):
                thread.start()
                self.assertTrue(observed.wait(2), "Expected a real Windows sharing conflict")
                self.assertEqual(self.target.read_bytes(), b'{"phase":"old"}\n')
                self.assertTrue(kernel.CloseHandle(handle))
                handle = None
                thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertFalse(failures)
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
            thread.join(4)
        self.assertEqual(storage.read_json(self.target), {"phase": "committed after reader closed"})
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ["state.json"])


if __name__ == "__main__":
    unittest.main()
