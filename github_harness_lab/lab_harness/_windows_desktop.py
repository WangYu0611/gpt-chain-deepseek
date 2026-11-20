"""Private desktop launcher for a Windows subprocess already contained in a Job."""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import subprocess
import sys
import uuid


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def _run(argv: list[str]) -> int:
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, dword = wintypes.HANDLE, wintypes.DWORD
    user.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                   dword, dword, ctypes.c_void_p]
    user.CreateDesktopW.restype = handle
    user.CloseDesktop.argtypes = [handle]
    kernel.GetCurrentProcess.restype = handle
    kernel.GetStdHandle.argtypes = [dword]
    kernel.GetStdHandle.restype = handle
    kernel.DuplicateHandle.argtypes = [handle, handle, handle, ctypes.POINTER(handle),
                                      dword, wintypes.BOOL, dword]
    kernel.CloseHandle.argtypes = [handle]
    kernel.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, dword, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInformation),
    ]
    kernel.WaitForSingleObject.argtypes = [handle, dword]
    kernel.WaitForSingleObject.restype = dword
    kernel.GetExitCodeProcess.argtypes = [handle, ctypes.POINTER(dword)]

    name = "CodexUnattended-" + uuid.uuid4().hex
    # Create the desktop in the current window station without switching the input desktop.
    access = 0x0001 | 0x0002 | 0x0040 | 0x0080  # READOBJECTS, CREATEWINDOW, ENUMERATE, WRITEOBJECTS
    desktop = user.CreateDesktopW(name, None, None, 0, access, None)
    if not desktop:
        raise OSError(ctypes.get_last_error(), "CreateDesktopW failed")
    with ExitStack() as lifetime:
        lifetime.callback(user.CloseDesktop, desktop)
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = name
        startup.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        process = _ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        with ExitStack() as inherited:
            current = kernel.GetCurrentProcess()
            for field, source in (("hStdInput", -10), ("hStdOutput", -11), ("hStdError", -12)):
                duplicate = handle()
                original = kernel.GetStdHandle(source)
                if not kernel.DuplicateHandle(current, original, current, ctypes.byref(duplicate),
                                              0, True, 0x00000002):  # DUPLICATE_SAME_ACCESS
                    raise OSError(ctypes.get_last_error(), "Could not inherit subprocess pipes")
                setattr(startup, field, duplicate.value)
                inherited.callback(kernel.CloseHandle, duplicate)
            if not kernel.CreateProcessW(None, command, None, None, True, subprocess.CREATE_NO_WINDOW,
                                         None, None, ctypes.byref(startup), ctypes.byref(process)):
                raise OSError(ctypes.get_last_error(), "Could not launch on the private desktop")
            lifetime.callback(kernel.CloseHandle, process.hProcess)
            lifetime.callback(kernel.CloseHandle, process.hThread)
        if kernel.WaitForSingleObject(process.hProcess, 0xFFFFFFFF) != 0:  # INFINITE
            raise OSError(ctypes.get_last_error(), "Private desktop process wait failed")
        code = dword()
        if not kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), "Could not read private desktop process result")
        return ctypes.c_int32(code.value).value


if __name__ == "__main__":
    try:
        status = _run(sys.argv[1:])
    except OSError as error:
        print(f"Private Windows desktop launch failed: {error}", file=sys.stderr)
        status = 1
    raise SystemExit(status)
