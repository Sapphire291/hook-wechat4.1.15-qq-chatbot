# -*- coding: utf-8 -*-
"""阻止微信抢走物理键鼠输入。

语义：
- 人正常在 Word/浏览器里打字时，完全不拦；
- 当微信窗口被自动回复流程抢到前台时：
  - 拦截原本会送进微信的物理键盘/鼠标事件；
  - 立即把前台还给最近一个非微信窗口（Word 等）；
  - 键盘事件会重放给还原后的窗口，尽量避免漏字。
- Ctrl+Alt+Shift+U 临时解锁 30 秒，方便手动操作微信；
- 设置里可以关闭。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_U = 0x55
LLKHF_INJECTED = 0x10
LLKHF_EXTENDED = 0x01
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1
SW_RESTORE = 9

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_busy_until = 0.0


def mark_busy(seconds: float = 8.0) -> None:
    """自动回复/发送期间置忙，输入守卫只在这段时间阻止微信抢键鼠。"""
    global _busy_until
    try:
        _busy_until = max(_busy_until, time.time() + float(seconds))
    except Exception:
        pass


def is_busy() -> bool:
    return time.time() < _busy_until


class InputGuard:
    def __init__(self, enabled_fn: Callable[[], bool], log_fn=None):
        self._enabled_fn = enabled_fn
        self._log_fn = log_fn
        self._thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._mouse_thread: Optional[threading.Thread] = None
        self._running = False
        self._thread_id = 0
        self._kb_hook = None
        self._mouse_hook = None
        self._kb_proc = None
        self._mouse_proc = None
        self._pause_until = 0.0
        self._last_pids = 0.0
        self._wechat_pids = set()
        self._wechat_hwnd = 0
        self._last_other_hwnd = 0
        self._my_pid = os.getpid()

    def log(self, msg: str) -> None:
        if self._log_fn:
            try:
                self._log_fn(msg)
            except Exception:
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="input-guard", daemon=True)
        self._thread.start()
        self._monitor_thread = threading.Thread(target=self._monitor, name="input-guard-monitor", daemon=True)
        self._monitor_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread_id:
            try:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    # ---------------- WeChat window ----------------
    def _refresh_wechat_pids(self) -> None:
        now = time.time()
        if now - self._last_pids < 2.0:
            return
        self._last_pids = now
        pids = set()
        try:
            import psutil
            for p in psutil.process_iter(["pid", "name"]):
                if (p.info.get("name") or "").lower() == "weixin.exe":
                    pids.add(int(p.info["pid"]))
        except Exception:
            pass
        self._wechat_pids = pids

    def _is_wechat_pid(self, pid: int) -> bool:
        return int(pid) in self._wechat_pids

    def _foreground_info(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0, 0
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(hwnd), int(pid.value)

    def _is_wechat_foreground(self) -> bool:
        _hwnd, pid = self._foreground_info()
        return bool(pid) and self._is_wechat_pid(pid)

    def _remember_or_restore(self) -> None:
        hwnd, pid = self._foreground_info()
        if not hwnd:
            return
        if pid and self._is_wechat_pid(pid):
            if is_busy():
                self._restore_last_other()
        else:
            self._last_other_hwnd = hwnd

    def _restore_last_other(self) -> bool:
        target = self._last_other_hwnd
        if not target or not user32.IsWindow(target):
            return False
        fg = user32.GetForegroundWindow()
        if fg == target:
            return True
        try:
            user32.ShowWindow(target, SW_RESTORE)
            cur_tid = int(kernel32.GetCurrentThreadId())
            fg_tid = int(user32.GetWindowThreadProcessId(fg, None) or 0) if fg else 0
            if fg_tid:
                user32.AttachThreadInput(fg_tid, cur_tid, True)
            ok = bool(user32.SetForegroundWindow(target))
            if fg_tid:
                user32.AttachThreadInput(fg_tid, cur_tid, False)
            return ok
        except Exception:
            return False

    def _emergency_combo(self) -> bool:
        try:
            return bool(
                (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                and (user32.GetAsyncKeyState(VK_MENU) & 0x8000)
                and (user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                and (user32.GetAsyncKeyState(VK_U) & 0x8000)
            )
        except Exception:
            return False

    def _should_block(self) -> bool:
        if not self._running:
            return False
        try:
            enabled = bool(self._enabled_fn())
        except Exception:
            enabled = False
        if not enabled:
            return False
        if time.time() < self._pause_until:
            return False
        if not is_busy():
            return False
        if not self._last_other_hwnd or not user32.IsWindow(self._last_other_hwnd):
            return False
        return self._is_wechat_foreground()

    # ---------------- hooks ----------------
    def _forward_key(self, kb: KBDLLHOOKSTRUCT) -> None:
        try:
            flags = 0
            if int(kb.flags) & LLKHF_EXTENDED:
                flags |= KEYEVENTF_EXTENDEDKEY
            events = (INPUT * 2)()
            for i, up in enumerate((False, True)):
                events[i].type = INPUT_KEYBOARD
                events[i].u.ki = KEYBDINPUT(
                    int(kb.vkCode), int(kb.scanCode),
                    flags | (KEYEVENTF_KEYUP if up else 0), 0, 0)
            user32.SendInput(2, events, ctypes.sizeof(INPUT))
        except Exception:
            pass

    def _keyboard_cb(self, n_code, w_param, l_param):
        if n_code >= 0 and self._should_block():
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if int(kb.flags) & LLKHF_INJECTED:
                return user32.CallNextHookEx(self._kb_hook, n_code, w_param, l_param)
            if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN) and self._emergency_combo():
                self._pause_until = time.time() + 30.0
                self.log("input guard: emergency unlock for 30s")
                return user32.CallNextHookEx(self._kb_hook, n_code, w_param, l_param)
            return 1
        return user32.CallNextHookEx(self._kb_hook, n_code, w_param, l_param)

    def _mouse_cb(self, n_code, w_param, l_param):
        if n_code >= 0 and self._should_block():
            self._restore_last_other()
            return 1
        return user32.CallNextHookEx(self._mouse_hook, n_code, w_param, l_param)

    def _monitor(self) -> None:
        while self._running:
            try:
                self._refresh_wechat_pids()
                self._remember_or_restore()
                if (is_busy() and self._is_wechat_foreground()
                        and (self._mouse_thread is None or not self._mouse_thread.is_alive())):
                    self._mouse_thread = threading.Thread(
                        target=self._run_mouse_hook, name="input-guard-mouse", daemon=True)
                    self._mouse_thread.start()
            except Exception:
                pass
            time.sleep(0.12)

    def _run_mouse_hook(self) -> None:
        """只在 busy 窗口内临时挂鼠标钩子，避免常驻钩子造成鼠标粘滞。"""
        hinst = kernel32.GetModuleHandleW(None)
        self._mouse_proc = HOOKPROC(self._mouse_cb)
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, hinst, 0)
        self._mouse_hook = hook
        try:
            while self._running and is_busy() and self._is_wechat_foreground():
                msg = wintypes.MSG()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.008)
        finally:
            if hook:
                user32.UnhookWindowsHookEx(hook)
            self._mouse_hook = None

    def _run(self) -> None:
        self._thread_id = int(kernel32.GetCurrentThreadId())
        hinst = kernel32.GetModuleHandleW(None)
        self._refresh_wechat_pids()
        self._kb_proc = HOOKPROC(self._keyboard_cb)
        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hinst, 0)
        self.log("input guard started keyboard=%r (mouse hook temporary during busy)"
                 % (bool(self._kb_hook),))
        msg = wintypes.MSG()
        try:
            while self._running and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._kb_hook:
                user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
            self.log("input guard stopped")
