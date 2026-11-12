from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from windowrelay.windows import DesktopError, DesktopUnavailableError, NativeDesktop, WindowActivationError, WindowInfo


class FakeUser:
    def __init__(self):
        self.foreground = 99
        self.escape = False
        self.calls = []
        self.succeed_attempt = 2
        self.foreground_attempts = 0
        self.attach_failure = None
        self.on_attach = None
        self.detach_failure = None
        self.hung = False
        self.desktop_calls = []
        self.desktop_handle = 55
        self.desktop_name = "Default"
        self.desktop_info_success = True
        self.desktop_info_error = False
        self.desktop_close_success = True

    def OpenInputDesktop(self, flags, inherit, access):
        self.desktop_calls.append(("open", flags, inherit, access))
        return self.desktop_handle

    def GetUserObjectInformationW(self, handle, index, buffer, size, needed):
        self.desktop_calls.append(("info", handle, index))
        if self.desktop_info_error:
            raise OSError("access denied")
        if self.desktop_info_success:
            buffer.value = self.desktop_name
            needed._obj.value = (len(self.desktop_name) + 1) * 2
        return self.desktop_info_success

    def CloseDesktop(self, handle):
        self.desktop_calls.append(("close", handle))
        return self.desktop_close_success

    def GetAsyncKeyState(self, _): return 0x8000 if self.escape else 0
    def GetForegroundWindow(self): return self.foreground
    def IsIconic(self, _): return False
    def IsHungAppWindow(self, _): return self.hung
    def GetWindowThreadProcessId(self, hwnd, _): return {12: 300, 99: 200}[hwnd]
    def PeekMessageW(self, *_): self.calls.append(("peek",)); return False
    def ShowWindow(self, hwnd, mode): self.calls.append(("restore", hwnd, mode)); return True
    def BringWindowToTop(self, hwnd): self.calls.append(("bring", hwnd)); return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", hwnd))
        self.foreground_attempts += 1
        if self.foreground_attempts == self.succeed_attempt:
            self.foreground = hwnd
        return self.foreground == hwnd

    def AttachThreadInput(self, caller, other, attach):
        self.calls.append(("attach" if attach else "detach", caller, other))
        if attach and self.on_attach:
            self.on_attach(caller, other)
        return (caller, other) != (self.attach_failure if attach else self.detach_failure)


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(12, "DeepSeek Harness", "DeepSeek Harness.exe", (1000, 700), 96)
        self.user = FakeUser()
        self.desktop = object.__new__(NativeDesktop)
        self.desktop.api = SimpleNamespace(user=self.user, kernel=SimpleNamespace(GetCurrentThreadId=lambda: 100))
        self.desktop.abort = None
        self.desktop.specs = {12: {"process_name": "DeepSeek Harness.exe", "title_contains": "DeepSeek"}}
        metadata = patch("windowrelay.windows._window_info", return_value=self.window)
        self.metadata = metadata.start()
        self.addCleanup(metadata.stop)
        sleep = patch("windowrelay.windows.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_normal_foreground_success_never_attaches(self):
        self.user.succeed_attempt = 1
        self.desktop.activate(self.window)
        self.assertEqual(self.user.calls, [("foreground", 12)])

    def test_single_fallback_attaches_and_detaches_all_pairs_in_reverse_order(self):
        self.desktop.activate(self.window)
        self.assertEqual(self.user.calls, [
            ("foreground", 12), ("peek",), ("attach", 100, 200), ("attach", 100, 300),
            ("bring", 12), ("foreground", 12), ("detach", 100, 300), ("detach", 100, 200),
        ])
        self.desktop.check(self.window)  # Foreground protection remains active.

    def test_failure_of_second_attach_cleans_up_first_without_focus_retry(self):
        self.user.attach_failure = (100, 300)
        with self.assertRaises(WindowActivationError): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls[-1], ("detach", 100, 200))
        self.assertNotIn(("detach", 100, 300), self.user.calls)
        self.assertNotIn(("bring", 12), self.user.calls)
        self.assertEqual(self.user.foreground_attempts, 1)

    def test_failure_of_first_attach_has_nothing_to_detach(self):
        self.user.attach_failure = (100, 200)
        with self.assertRaises(WindowActivationError): self.desktop.activate(self.window)
        self.assertFalse(any(call[0] == "detach" for call in self.user.calls))
        self.assertNotIn(("bring", 12), self.user.calls)

    def test_failed_foreground_is_bounded_and_distinguishes_no_input_started(self):
        self.user.succeed_attempt = 0
        with self.assertRaises(WindowActivationError): self.desktop.activate(self.window)
        self.assertEqual(self.user.foreground_attempts, 2)
        self.assertEqual(sum(call[0] == "bring" for call in self.user.calls), 1)
        self.assertEqual(sum(call[0] == "detach" for call in self.user.calls), 2)

    def test_escape_before_activation_has_no_mutations(self):
        self.user.escape = True
        with self.assertRaisesRegex(DesktopError, "Esc"): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls, [])

    def test_escape_during_attach_detaches_before_returning(self):
        self.user.on_attach = lambda *_: setattr(self.user, "escape", True)
        with self.assertRaisesRegex(DesktopError, "Esc"): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls[-1], ("detach", 100, 200))
        self.assertNotIn(("bring", 12), self.user.calls)

    def test_identity_and_dpi_changes_prevent_first_mutation(self):
        for actual in (WindowInfo(12, "Other app", "other.exe", (1000, 700), 96),
                       WindowInfo(12, "DeepSeek Harness", "DeepSeek Harness.exe", (1000, 700), 120)):
            with self.subTest(actual=actual):
                self.metadata.return_value = actual
                with self.assertRaises(DesktopError): self.desktop.activate(self.window)
                self.assertEqual(self.user.calls, [])

    def test_geometry_change_after_attach_detaches_and_never_brings_or_retries(self):
        def change(*_):
            self.metadata.return_value = WindowInfo(12, "DeepSeek Harness", "DeepSeek Harness.exe", (900, 700), 96)
        self.user.on_attach = change
        with self.assertRaisesRegex(DesktopError, "DPI"): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls[-1], ("detach", 100, 200))
        self.assertNotIn(("bring", 12), self.user.calls)
        self.assertEqual(self.user.foreground_attempts, 1)

    def test_detach_failure_prevents_input_even_if_foreground_was_obtained(self):
        self.user.detach_failure = (100, 300)
        with self.assertRaises(WindowActivationError): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls[-2:], [("detach", 100, 300), ("detach", 100, 200)])

    def test_focus_api_exception_still_detaches_both_pairs(self):
        normal = self.user.SetForegroundWindow
        def fail_second_attempt(hwnd):
            if self.user.foreground_attempts == 1:
                self.user.calls.append(("foreground_error", hwnd))
                raise OSError("simulated Win32 failure")
            return normal(hwnd)
        self.user.SetForegroundWindow = fail_second_attempt
        with self.assertRaises(WindowActivationError): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls[-2:], [("detach", 100, 300), ("detach", 100, 200)])

    def test_shared_foreground_and_target_thread_is_attached_only_once(self):
        self.user.GetWindowThreadProcessId = lambda *_: 300
        self.desktop.activate(self.window)
        self.assertEqual([call for call in self.user.calls if call[0] == "attach"], [("attach", 100, 300)])
        self.assertEqual([call for call in self.user.calls if call[0] == "detach"], [("detach", 100, 300)])

    def test_foreground_check_is_not_disabled_after_success(self):
        self.desktop.activate(self.window)
        self.user.foreground = 99
        with self.assertRaisesRegex(DesktopError, "前台窗口"): self.desktop.check(self.window)

    def test_hung_window_is_not_attached(self):
        self.user.hung = True
        with self.assertRaises(WindowActivationError): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls, [("foreground", 12)])

    def test_pause_during_activation_stops_without_attachments(self):
        self.desktop.abort = lambda: (_ for _ in ()).throw(DesktopError("暂停"))
        with self.assertRaisesRegex(DesktopError, "暂停"): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls, [])

    def test_default_desktop_is_available_and_handle_is_released(self):
        self.assertTrue(self.desktop.is_desktop_available())
        self.assertEqual(self.user.desktop_calls, [("open", 0, False, 1), ("info", 55, 2), ("close", 55)])

    def test_access_denied_open_is_unavailable_without_handle_to_close(self):
        self.user.desktop_handle = 0
        self.assertFalse(self.desktop.is_desktop_available())
        self.assertEqual(self.user.desktop_calls, [("open", 0, False, 1)])

    def test_winlogon_and_unknown_desktops_are_unavailable_and_closed(self):
        for name in ("Winlogon", "Screen-saver", "", "OtherDesktop"):
            with self.subTest(name=name):
                self.user.desktop_name = name
                self.assertFalse(self.desktop.is_desktop_available())
                self.assertEqual(self.user.desktop_calls[-1], ("close", 55))

    def test_failed_information_read_still_releases_handle(self):
        self.user.desktop_info_success = False
        self.assertFalse(self.desktop.is_desktop_available())
        self.assertEqual(self.user.desktop_calls[-1], ("close", 55))
        self.user.desktop_info_error = True
        self.assertFalse(self.desktop.is_desktop_available())
        self.assertEqual(self.user.desktop_calls[-1], ("close", 55))

    def test_handle_release_failure_does_not_allow_input(self):
        self.user.desktop_close_success = False
        self.assertFalse(self.desktop.is_desktop_available())

    def test_locked_desktop_prevents_activation_and_even_window_metadata_read(self):
        self.user.desktop_name = "Winlogon"
        with self.assertRaises(DesktopUnavailableError): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls, [])
        self.metadata.assert_not_called()

    def test_locked_desktop_prevents_check_before_window_metadata_read(self):
        self.user.desktop_handle = 0
        with self.assertRaises(DesktopUnavailableError): self.desktop.check(self.window)
        self.assertEqual(self.user.calls, [])
        self.metadata.assert_not_called()

    def test_desktop_lock_mid_attach_stops_and_detaches_successful_pair(self):
        self.user.on_attach = lambda *_: setattr(self.user, "desktop_name", "Winlogon")
        with self.assertRaises(DesktopUnavailableError): self.desktop.activate(self.window)
        self.assertEqual(self.user.calls[-1], ("detach", 100, 200))
        self.assertNotIn(("bring", 12), self.user.calls)


if __name__ == "__main__":
    unittest.main()
