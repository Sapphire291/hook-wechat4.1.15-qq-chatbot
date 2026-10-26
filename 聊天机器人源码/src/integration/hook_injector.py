# -*- coding: utf-8 -*-
"""微信 hook 自动注入（启动连接微信时调用）。

- 若 30001 已可用：跳过；
- 否则找 Weixin.exe 进程，向运行中的进程注入加载 wxhook415.dll；
- DLL 随 exe 打包在 hook/ 目录，运行时复制到 %LOCALAPPDATA%\\ChatBot\\hook 再注入。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HOOK_API = "http://127.0.0.1:30001"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.VirtualAllocEx.restype = ctypes.c_void_p
kernel32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
kernel32.WriteProcessMemory.restype = wt.BOOL
kernel32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.CreateRemoteThread.restype = wt.HANDLE
kernel32.CreateRemoteThread.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetProcAddress.restype = ctypes.c_void_p
kernel32.GetProcAddress.argtypes = [wt.HMODULE, wt.LPCSTR]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]


def _log(msg: str) -> None:
    try:
        from ..core.paths import data_dir
        p = os.path.join(data_dir(), "hook_inject.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def hook_alive(timeout: float = 3.0) -> bool:
    for path in ("/Version", "/GetChat"):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    HOOK_API + path, data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST"), timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


def _candidate_dll() -> str:
    names = ("wxhook415.dll", "wxhook415_exp67.dll")
    roots = []
    if getattr(sys, "_MEIPASS", ""):
        roots.append(os.path.join(sys._MEIPASS, "hook"))
    here = os.path.dirname(os.path.abspath(__file__))
    roots += [os.path.join(here, "..", "..", "hook"), os.path.join(here, "..", "..")]
    try:
        from ..core.paths import data_home
        roots += [os.path.join(data_home(), "hook"), data_home()]
    except Exception:
        pass
    for root in roots:
        for name in names:
            p = os.path.abspath(os.path.join(root, name))
            if os.path.isfile(p):
                return p
    return ""


def _runtime_dll() -> str:
    """把随包 DLL 复制到持久目录，返回可注入路径。"""
    src = _candidate_dll()
    if not src:
        return ""
    try:
        base = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ChatBot", "hook")
        os.makedirs(base, exist_ok=True)
        dst = os.path.join(base, "wxhook415.dll")
        if (not os.path.isfile(dst)) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copy2(src, dst)
        return dst
    except Exception as exc:
        _log("copy dll failed: %r" % (exc,))
        return src


def _weixin_pids():
    pids = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == "weixin.exe":
                    pids.append(int(p.info["pid"]))
            except Exception:
                pass
        if pids:
            return pids
    except Exception:
        pass
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        for line in out.splitlines()[1:]:
            parts = line.split('","')
            if len(parts) > 1:
                try:
                    pids.append(int(parts[1].strip('"')))
                except Exception:
                    pass
    except Exception:
        pass
    return pids


def _inject(pid: int, dll: str) -> bool:
    buf = (dll + "\x00").encode("utf-16-le")
    h = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        _log("OpenProcess pid=%d err=%d" % (pid, ctypes.get_last_error()))
        return False
    try:
        addr = kernel32.VirtualAllocEx(h, None, len(buf), MEM_COMMIT_RESERVE, PAGE_READWRITE)
        if not addr:
            _log("VirtualAllocEx err=%d" % ctypes.get_last_error())
            return False
        n = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(h, addr, buf, len(buf), ctypes.byref(n)):
            _log("WriteProcessMemory err=%d" % ctypes.get_last_error())
            return False
        k32 = kernel32.GetModuleHandleW("kernel32.dll")
        fn = kernel32.GetProcAddress(k32, b"LoadLibraryW")
        tid = wt.DWORD(0)
        th = kernel32.CreateRemoteThread(h, None, 0, fn, addr, 0, ctypes.byref(tid))
        if not th:
            _log("CreateRemoteThread err=%d" % ctypes.get_last_error())
            return False
        kernel32.WaitForSingleObject(th, 15000)
        code = wt.DWORD(0)
        kernel32.GetExitCodeThread(th, ctypes.byref(code))
        kernel32.CloseHandle(th)
        _log("inject pid=%d LoadLibraryW=%d dll=%s" % (pid, code.value, dll))
        return bool(code.value)
    finally:
        kernel32.CloseHandle(h)


def ensure_hook(force: bool = False) -> dict:
    """确保 hook 已注入；返回状态字典。"""
    if not force and hook_alive(2.0):
        return {"ok": True, "already": True, "msg": "hook 已在线"}
    dll = _runtime_dll()
    if not dll:
        msg = "未找到 wxhook415.dll（hook/ 目录或数据根目录）"
        _log(msg)
        return {"ok": False, "msg": msg}
    pids = _weixin_pids()
    if not pids:
        msg = "未发现 Weixin.exe；请先启动并登录微信"
        _log(msg)
        return {"ok": False, "msg": msg, "dll": dll}
    # 微信有多个子进程，只注入主进程（内存占用最大/有窗口的那个），避免污染 helper 进程
    if len(pids) > 1:
        try:
            import psutil
            main_pid = max(pids, key=lambda p: psutil.Process(p).memory_info().rss)
            pids = [main_pid]
        except Exception:
            pids = pids[:1]
    ok_any = False
    for pid in pids:
        try:
            if _inject(pid, dll):
                ok_any = True
        except Exception as exc:
            _log("inject exception pid=%d: %r" % (pid, exc))
    if ok_any:
        for _ in range(20):
            time.sleep(0.5)
            if hook_alive(1.0):
                return {"ok": True, "already": False, "pids": pids, "dll": dll}
    return {"ok": False, "pids": pids, "dll": dll, "msg": "注入后 30001 未就绪"}
