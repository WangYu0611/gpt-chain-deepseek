"""Small Win32 input backend for the user's explicitly calibrated windows.

Importing this module never moves the mouse, changes focus, or types.  The only
read-only entry points used during setup are window enumeration and cursor reads.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Callable


class DesktopError(RuntimeError):
    pass


class WindowActivationError(DesktopError):
    """Target focus could not be established before any mouse/keyboard input."""


class DesktopUnavailableError(DesktopError):
    """The active input desktop is locked, secure, inaccessible or unknown."""


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process_name: str
    client_size: tuple[int, int]
    dpi: int


class _Api:
    def __init__(self) -> None:
        if os.name != "nt":
            raise DesktopError("窗口控制仅支持 Windows。")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.enum_proc, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        for name in ("IsWindow", "IsWindowVisible", "IsIconic", "SetForegroundWindow", "BringWindowToTop", "IsHungAppWindow"):
            function = getattr(self.user, name)
            function.argtypes = [wintypes.HWND]
            function.restype = wintypes.BOOL
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetUserObjectInformationW.restype = wintypes.BOOL
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        self.user.CloseDesktop.restype = wintypes.BOOL
        self.user.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        self.user.AttachThreadInput.restype = wintypes.BOOL
        self.user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user.PeekMessageW.restype = wintypes.BOOL
        self.kernel.GetCurrentThreadId.argtypes = []
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user.GetWindowTextLengthW.restype = ctypes.c_int
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowTextW.restype = ctypes.c_int
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetClientRect.restype = wintypes.BOOL
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user.GetCursorPos.restype = wintypes.BOOL
        self.user.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user.ScreenToClient.restype = wintypes.BOOL
        self.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user.ClientToScreen.restype = wintypes.BOOL
        self.user.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user.WindowFromPoint.restype = wintypes.HWND
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user.SetCursorPos.restype = wintypes.BOOL
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.ShowWindow.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        if hasattr(self.user, "GetDpiForWindow"):
            self.user.GetDpiForWindow.argtypes = [wintypes.HWND]
            self.user.GetDpiForWindow.restype = wintypes.UINT
        # Coordinates are physical client pixels, rather than DPI-virtualized.
        try:
            self.user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            self.user.SetProcessDPIAware()


_api: _Api | None = None


def _get_api() -> _Api:
    global _api
    if _api is None:
        _api = _Api()
    return _api


def _window_info(hwnd: int) -> WindowInfo:
    api = _get_api()
    if not api.user.IsWindow(hwnd) or not api.user.IsWindowVisible(hwnd):
        raise DesktopError("指定窗口已经关闭或不可见。")
    length = api.user.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    api.user.GetWindowTextW(hwnd, title, length + 1)
    pid = wintypes.DWORD()
    api.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = api.kernel.OpenProcess(0x1000, False, pid.value)
    if not process:
        raise DesktopError("无法读取窗口所属进程，请保持程序以普通用户身份运行。")
    try:
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        if not api.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
            raise DesktopError("无法核对窗口所属进程。")
    finally:
        api.kernel.CloseHandle(process)
    rect = wintypes.RECT()
    if not api.user.GetClientRect(hwnd, ctypes.byref(rect)):
        raise DesktopError("无法读取窗口大小。")
    dpi = int(api.user.GetDpiForWindow(hwnd)) if hasattr(api.user, "GetDpiForWindow") else 96
    return WindowInfo(int(hwnd), title.value, PureWindowsPath(path.value).name,
                      (rect.right - rect.left, rect.bottom - rect.top), dpi or 96)


def list_windows() -> list[WindowInfo]:
    api = _get_api()
    result: list[WindowInfo] = []

    @api.enum_proc
    def collect(hwnd, _):
        if api.user.IsWindowVisible(hwnd) and api.user.GetWindowTextLengthW(hwnd):
            try:
                result.append(_window_info(int(hwnd)))
            except DesktopError:
                pass  # Other processes may deny metadata access.
        return True

    if not api.user.EnumWindows(collect, 0):
        raise DesktopError("无法枚举当前窗口。")
    return result


def _matches(info: WindowInfo, spec: dict) -> bool:
    process = str(spec.get("process_name", "")).strip()
    title = str(spec.get("title_contains", "")).strip()
    if not process or not title:
        raise DesktopError("窗口配置必须同时填写 process_name 和 title_contains。")
    return info.process_name.casefold() == process.casefold() and title.casefold() in info.title.casefold()


def find_window(spec: dict) -> WindowInfo:
    handle = spec.get("hwnd")
    if handle:
        try:
            hwnd = int(handle, 0) if isinstance(handle, str) else int(handle)
        except (ValueError, TypeError) as error:
            raise DesktopError("hwnd 不是有效的窗口编号。") from error
        candidates = [_window_info(hwnd)]
    else:
        candidates = list_windows()
    found = [info for info in candidates if _matches(info, spec)]
    if len(found) != 1:
        raise DesktopError(f"窗口匹配到 {len(found)} 个；请打开目标标签页，或调整标题/窗口编号，使其唯一。")
    return found[0]


def cursor_client(hwnd: int) -> tuple[int, int]:
    api = _get_api()
    info = _window_info(hwnd)
    point = wintypes.POINT()
    if not api.user.GetCursorPos(ctypes.byref(point)) or not api.user.ScreenToClient(hwnd, ctypes.byref(point)):
        raise DesktopError("无法读取鼠标在窗口内的位置。")
    if not (0 <= point.x < info.client_size[0] and 0 <= point.y < info.client_size[1]):
        raise DesktopError("鼠标不在指定窗口的客户区内。")
    return int(point.x), int(point.y)


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("value", _InputUnion)]


def _key(vk: int, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(1, _InputUnion(ki=_KeyboardInput(vk, scan, flags, 0, 0)))


def _unicode_batches(text: str) -> list[list[_Input]]:
    """Printable UTF-16 packets plus explicit Shift+Enter line breaks."""
    if not isinstance(text, str) or not text.strip() or len(text) > 50000:
        raise DesktopError("消息必须非空，且不超过 50000 个字符。")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    if any(ord(c) < 32 and c != "\n" for c in text):
        raise DesktopError("消息含不允许的控制字符。")
    batches: list[list[_Input]] = []
    for line_index, line in enumerate(text.split("\n")):
        if line_index:
            batches.append([_key(0x10), _key(0x0D), _key(0x0D, flags=2), _key(0x10, flags=2)])
        encoded = line.encode("utf-16-le")
        units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
        for i in range(0, len(units), 32):
            packets = []
            for code in units[i:i + 32]:
                packets.extend([_key(0, code, 4), _key(0, code, 4 | 2)])
            batches.append(packets)
    return batches


class NativeDesktop:
    def __init__(self, abort: Callable[[], None] | None = None):
        self.api = _get_api()
        self.abort = abort
        self.specs: dict[int, dict] = {}
        self.api.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        self.api.user.SendInput.restype = wintypes.UINT

    def find_window(self, spec: dict) -> WindowInfo:
        info = find_window(spec)
        self.specs[info.hwnd] = dict(spec)
        return info

    def is_escape(self) -> bool:
        # Also catch a quick press released between two short action checks.
        return bool(self.api.user.GetAsyncKeyState(0x1B) & 0x8001)

    def is_desktop_available(self) -> bool:
        """Read only the active input desktop name; never switches or unlocks it."""
        try:
            handle = self.api.user.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        except (OSError, AttributeError):
            return False
        if not handle:
            return False
        available = False
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if self.api.user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):  # UOI_NAME
                available = needed.value <= ctypes.sizeof(name) and name.value.casefold() == "default"
        except (OSError, AttributeError, ValueError):
            available = False
        finally:
            try:
                if not self.api.user.CloseDesktop(handle):
                    available = False
            except (OSError, AttributeError):
                available = False
        return available

    def _check_identity(self, info: WindowInfo) -> None:
        if self.abort:
            self.abort()
        if not self.is_desktop_available():
            raise DesktopUnavailableError("交互桌面已锁定或不可访问；未执行窗口输入，请解锁后继续。")
        if self.is_escape():
            raise DesktopError("检测到 Esc，窗口操作已中断。")
        actual = _window_info(info.hwnd)
        if not _matches(actual, self.specs[info.hwnd]) or actual.process_name.casefold() != info.process_name.casefold():
            raise DesktopError("目标窗口身份或标题发生变化，已停止输入。")
        if actual.client_size != info.client_size or actual.dpi != info.dpi:
            raise DesktopError("窗口大小或 DPI 已变化，请重新校准。")

    def check(self, info: WindowInfo) -> None:
        self._check_identity(info)
        if int(self.api.user.GetForegroundWindow() or 0) != info.hwnd:
            raise DesktopError("前台窗口已改变，已停止输入。")

    def _wait_for_foreground(self, info: WindowInfo) -> bool:
        for attempt in range(4):
            self._check_identity(info)
            if int(self.api.user.GetForegroundWindow() or 0) == info.hwnd:
                return True
            if attempt < 3:
                time.sleep(0.05)
        return False

    def _activation_fallback(self, info: WindowInfo) -> None:
        """One temporary same-desktop input-queue attachment; never injects keys."""
        self._check_identity(info)
        foreground = int(self.api.user.GetForegroundWindow() or 0)
        if foreground == info.hwnd:
            return
        if self.api.user.IsHungAppWindow(info.hwnd) or (foreground and self.api.user.IsHungAppWindow(foreground)):
            raise WindowActivationError("目标或当前前台窗口无响应；尚未开始鼠标或文字输入。")
        caller_thread = int(self.api.kernel.GetCurrentThreadId())
        target_thread = int(self.api.user.GetWindowThreadProcessId(info.hwnd, None))
        foreground_thread = int(self.api.user.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        if not caller_thread or not target_thread or (foreground and not foreground_thread):
            raise WindowActivationError("无法核对激活所需线程；尚未开始鼠标或文字输入。")
        # USER creates a caller message queue on first use; PeekMessage makes that
        # prerequisite explicit without removing or dispatching any messages.
        message = wintypes.MSG()
        self.api.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        attached: list[tuple[int, int]] = []
        detach_failed = False
        try:
            for other_thread in dict.fromkeys((foreground_thread, target_thread)):
                if not other_thread or other_thread == caller_thread:
                    continue
                self._check_identity(info)
                if not self.api.user.AttachThreadInput(caller_thread, other_thread, True):
                    raise WindowActivationError("临时连接窗口输入队列失败；尚未开始鼠标或文字输入。")
                attached.append((caller_thread, other_thread))
            self._check_identity(info)
            self.api.user.BringWindowToTop(info.hwnd)
            self._check_identity(info)
            self.api.user.SetForegroundWindow(info.hwnd)
        finally:
            # Do not sleep or perform input while queues are attached. Detach
            # every successful pair even if a later attach/API/guard failed.
            for caller, other in reversed(attached):
                try:
                    if not self.api.user.AttachThreadInput(caller, other, False):
                        detach_failed = True
                except OSError:
                    detach_failed = True
        if detach_failed:
            raise WindowActivationError("窗口输入队列清理失败；未执行鼠标或文字输入，已停止。")

    def activate(self, info: WindowInfo) -> None:
        self._check_identity(info)
        if self.api.user.IsIconic(info.hwnd):
            self.api.user.ShowWindow(info.hwnd, 9)
            self._check_identity(info)
        self.api.user.SetForegroundWindow(info.hwnd)
        if not self._wait_for_foreground(info):
            try:
                self._activation_fallback(info)
            except OSError as error:
                raise WindowActivationError("窗口激活调用失败；尚未开始鼠标或文字输入。") from error
            if not self._wait_for_foreground(info):
                raise WindowActivationError("无法激活指定窗口；尚未开始鼠标或文字输入，请先手动切到目标窗口。")
        self.check(info)

    def _point(self, info: WindowInfo, point: tuple[int, int]) -> wintypes.POINT:
        self.check(info)
        x, y = point
        if not (0 <= x < info.client_size[0] and 0 <= y < info.client_size[1]):
            raise DesktopError("点击点不在校准的窗口客户区内。")
        screen = wintypes.POINT(x, y)
        if not self.api.user.ClientToScreen(info.hwnd, ctypes.byref(screen)):
            raise DesktopError("无法换算窗口坐标。")
        owner = self.api.user.WindowFromPoint(screen)
        root = self.api.user.GetAncestor(owner, 2) if owner else 0
        if int(root or 0) != info.hwnd:
            raise DesktopError("目标点被其他窗口或弹窗遮挡，已停止点击。")
        return screen

    def move(self, info: WindowInfo, point: tuple[int, int]) -> None:
        screen = self._point(info, point)
        if not self.api.user.SetCursorPos(screen.x, screen.y):
            raise DesktopError("无法移动鼠标。")

    def _send(self, info: WindowInfo, events: list[_Input]) -> None:
        self.check(info)
        values = (_Input * len(events))(*events)
        if self.api.user.SendInput(len(events), values, ctypes.sizeof(_Input)) != len(events):
            raise DesktopError("Windows 未完整接受输入；请核对界面，不能直接重发。")

    def click(self, info: WindowInfo, point: tuple[int, int]) -> None:
        self.move(info, point)
        self._point(info, point)
        self._send(info, [_Input(0, _InputUnion(mi=_MouseInput(0, 0, 0, 2, 0, 0))),
                          _Input(0, _InputUnion(mi=_MouseInput(0, 0, 0, 4, 0, 0)))])

    def type_unicode(self, info: WindowInfo, text: str) -> None:
        for batch in _unicode_batches(text):
            self._send(info, batch)
            time.sleep(0.005)

    def enter(self, info: WindowInfo) -> None:
        self._send(info, [_key(0x0D), _key(0x0D, flags=2)])

    def wait(self, info: WindowInfo, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check(info)
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
