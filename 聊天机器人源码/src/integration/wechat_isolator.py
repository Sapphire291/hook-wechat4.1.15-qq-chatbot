# -*- coding: utf-8 -*-
"""微信最小化时隔离微信窗口的用户输入。

- 微信最小化：EnableWindow(FALSE)，键盘/鼠标不会进入微信；
- 其他程序（Word 等）的前台/焦点完全不受影响；
- exe 发送前调用 allow_for_send()，临时 EnableWindow(TRUE)，让 hook 能发送；
- 发送完成后自动重新隔离；
- Ctrl+Alt+Shift+U 临时解禁 30 秒，方便手动打开微信。
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import urllib.request
from ctypes import wintypes
from typing import Callable, Dict, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.restype = wintypes.BOOL
user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.EnableWindow.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
user32.GetGUIThreadInfo.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_SHIFT = 0x10
VK_U = 0x55

_busy_until = 0.0
_unlock_until = 0.0
_active: "Optional[WeChatInputIsolator]" = None


def allow_for_send(seconds: float = 12.0) -> None:
    """exe 发消息前调用：立刻解禁微信，让 hook 能操作。"""
    global _busy_until
    _busy_until = max(_busy_until, time.time() + float(seconds))
    try:
        if _active is not None:
            _active._set_all_enabled(True)
    except Exception:
        pass


def begin_send(seconds: float = 12.0) -> None:
    """发送期间只标记忙碌，不解禁窗口；隔离器暂停清焦点，避免打断发送。"""
    global _busy_until
    _busy_until = max(_busy_until, time.time() + float(seconds))


def end_send() -> None:
    """发送完成：立即恢复隔离（若微信仍最小化）。"""
    global _busy_until
    _busy_until = 0.0
    try:
        if _active is not None:
            _active.enforce_isolation()
    except Exception:
        pass


def is_unlocked() -> bool:
    return time.time() < _unlock_until


def clear_wechat_focus() -> None:
    try:
        if _active is not None:
            _active._clear_hidden_focus()
    except Exception:
        pass


def is_wechat_minimized() -> bool:
    try:
        if _active is not None:
            return any(user32.IsIconic(h) for h in _active._windows())
    except Exception:
        pass
    return False


class WeChatInputIsolator:
    def __init__(self, enabled_fn: Callable[[], bool], log_fn=None):
        self._enabled_fn = enabled_fn
        self._log_fn = log_fn
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pids: set = set()
        self._pid_ts = 0.0
        self._disabled: Dict[int, bool] = {}
        self._last_unlock = 0.0

    def log(self, msg: str) -> None:
        if self._log_fn:
            try:
                self._log_fn(msg)
            except Exception:
                pass

    def start(self) -> None:
        global _active
        if self._thread and self._thread.is_alive():
            return
        _active = self
        self._running = True
        self._thread = threading.Thread(target=self._run, name="wechat-isolator", daemon=True)
        self._thread.start()

    def enforce_isolation(self) -> None:
        try:
            if not bool(self._enabled_fn()) or is_unlocked():
                return
        except Exception:
            return
        for hwnd in self._windows():
            try:
                if user32.IsIconic(hwnd) and user32.IsWindowEnabled(hwnd):
                    user32.EnableWindow(hwnd, False)
            except Exception:
                pass

    def stop(self) -> None:
        global _active
        self._running = False
        self._set_all_enabled(True)
        if _active is self:
            _active = None

    def _refresh_pids(self) -> None:
        now = time.time()
        if now - self._pid_ts < 2.0:
            return
        self._pid_ts = now
        pids = set()
        try:
            import psutil
            for p in psutil.process_iter(["pid", "name"]):
                if (p.info.get("name") or "").lower() == "weixin.exe":
                    pids.add(int(p.info["pid"]))
        except Exception:
            pass
        self._pids = pids

    def _windows(self):
        self._refresh_pids()
        if not self._pids:
            return []
        out = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _l):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) in self._pids:
                out.append(int(hwnd))
            return True

        try:
            user32.EnumWindows(cb, 0)
        except Exception:
            pass
        return out

    def _set_all_enabled(self, enabled: bool) -> None:
        for hwnd in self._windows():
            try:
                if bool(user32.IsWindowEnabled(hwnd)) != enabled:
                    user32.EnableWindow(hwnd, enabled)
                    self._disabled[hwnd] = not enabled
            except Exception:
                pass

    def _current_chat(self) -> str:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:30001/GetChat", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return str(json.loads(resp.read().decode("utf-8")).get("chat") or "")
        except Exception:
            return ""

    def _ensure_donor(self) -> bool:
        donor = "文件传输助手"
        chat = self._current_chat()
        if chat in ("文件传输助手", "filehelper"):
            return True
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:30001/SendTextTo",
                data=json.dumps({"chat": donor, "text": " "}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            if out.get("ret") == 0:
                return True
        except Exception as exc:
            self.log("ensure donor failed %r" % (exc,))
        return False

    def _check_unlock_hotkey(self) -> None:
        try:
            pressed = bool(
                (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                and (user32.GetAsyncKeyState(VK_MENU) & 0x8000)
                and (user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
                and (user32.GetAsyncKeyState(VK_U) & 0x8000)
            )
        except Exception:
            pressed = False
        if pressed and time.time() - self._last_unlock > 1.5:
            global _unlock_until
            _unlock_until = time.time() + 30.0
            self._last_unlock = time.time()
            self.log("wechat isolator: unlock 30s")

    def _clear_hidden_focus(self) -> None:
        """清掉微信 GUI 线程的隐藏焦点，避免键盘被后台窗口截走。"""
        cur = int(kernel32.GetCurrentThreadId())
        for hwnd in self._windows():
            tid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(tid))
            if not tid.value:
                continue
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            try:
                if not user32.GetGUIThreadInfo(tid.value, ctypes.byref(info)):
                    continue
            except Exception:
                continue
            if not info.hwndFocus:
                continue
            try:
                user32.AttachThreadInput(tid.value, cur, True)
                user32.SetFocus(None)
                user32.AttachThreadInput(tid.value, cur, False)
            except Exception:
                pass

    def _run(self) -> None:
        self.log("wechat isolator started")
        while self._running:
            try:
                self._check_unlock_hotkey()
                enabled = bool(self._enabled_fn())
                busy = time.time() < _busy_until
                unlocked = is_unlocked()
                windows = self._windows()
                for hwnd in windows:
                    iconic = bool(user32.IsIconic(hwnd))
                    # 最小化就隔离；不在这里切会话，否则会把微信重新激活
                    should_disable = bool(enabled and iconic and not busy and not unlocked)
                    try:
                        if should_disable and user32.IsWindowEnabled(hwnd):
                            user32.EnableWindow(hwnd, False)
                            self._disabled[hwnd] = True
                        elif (not should_disable) and (not user32.IsWindowEnabled(hwnd)):
                            user32.EnableWindow(hwnd, True)
                            self._disabled[hwnd] = False
                    except Exception:
                        pass
                if enabled and not busy and any(user32.IsIconic(h) for h in windows):
                    self._clear_hidden_focus()
            except Exception as exc:
                self.log("wechat isolator error %r" % (exc,))
            time.sleep(0.25)

    def stop_and_restore(self) -> None:
        self._running = False
        self._set_all_enabled(True)
