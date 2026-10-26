# -*- coding: utf-8 -*-
"""微信接入插件：数据库读取 + hook 优先 / 持久 UIA-OCR 兜底发送。

性能要点：复用同一个 WeChatGUI 实例，命中 wechatauto 的「快速路径」
（跳过重复窗口探测/会话重检），把单次发送从 ~130s 降到数秒。
"""
from __future__ import annotations

import json
import subprocess
import threading
import tkinter as tk
import urllib.request
from tkinter import ttk

try:
    from src.plugins import PluginBase
except Exception:
    from src.plugins.api import PluginBase

HOOK_API = "http://127.0.0.1:30001"

_GUI = None
_GUI_LOCK = threading.Lock()


def _get_gui():
    global _GUI
    if _GUI is not None:
        return _GUI
    with _GUI_LOCK:
        if _GUI is None:
            from wechatauto.guia import WeChatGUI
            _GUI = WeChatGUI()
    return _GUI


def _wechat_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe"],
                             capture_output=True, text=True, timeout=10).stdout or ""
        return "Weixin.exe" in out
    except Exception:
        return False


def _hook_available(timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(HOOK_API + "/QueryDB/status", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _connect_db():
    try:
        from wechatauto import WeChatDB
        db = WeChatDB()
        info = db.get_self_info() or {}
        return db, info, ""
    except Exception as exc:
        return None, {}, str(exc)


def _hook_send(wxid: str, text: str) -> bool:
    try:
        data = json.dumps({"wxidorgid": wxid, "msg": text}).encode("utf-8")
        req = urllib.request.Request(HOOK_API + "/SendTextMsg", data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            out = json.loads(r.read().decode("utf-8"))
            return bool(out and out.get("ret") == 0)
    except Exception:
        return False


def _screen_send_text(who: str, text: str) -> bool:
    try:
        resp = _get_gui().send_msg(str(text), str(who), verify=False)
        return getattr(resp, "status", None) is not False
    except Exception:
        return False


def _screen_send_file(who: str, path: str) -> bool:
    try:
        resp = _get_gui().send_file(str(path), str(who), verify=False)
        return getattr(resp, "status", None) is not False
    except Exception:
        return False


class Plugin(PluginBase):
    name = "wechat_hook"
    title = "微信接入"
    description = "数据库读消息 + hook 优先 / 持久 UIA-OCR 兜底发送（含表情包/图片）。"
    version = "4.1.0"
    api_defaults = {"api_key": "", "base_url": HOOK_API, "model": ""}

    def __init__(self, context=None):
        super().__init__(context)
        self._parent = None
        self._status_label = None
        self._log_label = None
        self._db = None
        self._cached_running = False
        self._cached_connected = False
        self._cached_hook = False
        self._cached_msg = ""

    def build_content(self, parent) -> None:
        self._parent = parent
        box = ttk.LabelFrame(parent, text="微信接入")
        box.pack(fill="both", expand=True, pady=(0, 8))
        self._status_label = ttk.Label(box, text="检测中…", font=("", 11))
        self._status_label.pack(anchor="w", padx=8, pady=(8, 4))
        row = ttk.Frame(box)
        row.pack(anchor="w", padx=8, pady=4)
        ttk.Button(row, text="重新检测", command=self._detect_async).pack(side="left")
        ttk.Button(row, text="连接微信", command=self._attach_async).pack(side="left", padx=8)
        ttk.Button(row, text="断开", command=self._detach).pack(side="left")
        self._log_label = ttk.Label(box, text="", foreground="#666", wraplength=640, justify="left")
        self._log_label.pack(anchor="w", padx=8, pady=(4, 8))
        try:
            parent.after(250, self._detect_async)
        except Exception:
            pass

    def _ui(self, fn) -> None:
        try:
            if self._parent is not None:
                self._parent.after(0, fn)
            else:
                fn()
        except Exception:
            pass

    def _set_status(self, text: str, ok: bool = True) -> None:
        try:
            self._status_label.config(text=text, foreground="#2a7d2a" if ok else "#b33")
        except Exception:
            pass

    def _append_log(self, text: str) -> None:
        try:
            old = self._log_label.cget("text")
            self._log_label.config(text=(old + "\n" + text).strip())
        except Exception:
            pass

    def _detect_async(self) -> None:
        self._set_status("检测中…")
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self) -> None:
        running = _wechat_running()
        db, info, err = (None, {}, "")
        if running:
            db, info, err = _connect_db()
        hook_ok = _hook_available() if running else False
        self._cached_running = bool(running)
        self._cached_connected = db is not None
        self._cached_hook = bool(hook_ok)
        if db is not None:
            self._db = db
        self._ui(lambda: self._apply(running, db, info, err, hook_ok))

    def _apply(self, running, db, info, err, hook_ok) -> None:
        lines = ["微信客户端：" + ("运行中" if running else "未运行（请先打开并登录电脑端微信）")]
        if db is not None:
            nick = str(info.get("nick_name") or "")
            wxid = str(info.get("username") or "")
            lines.append("读消息：已连接（%s / %s）" % (nick or "?", wxid or "?"))
        elif running and err:
            lines.append("读消息：连接失败（%s）" % err)
        lines.append("发消息：" + ("hook 协议（不碰屏幕）" if hook_ok
                                 else "UIA/OCR（复用持久实例，已加速）"))
        if db is not None:
            self._set_status("已连接：可读可发（%s）" % ("hook" if hook_ok else "UIA/OCR"), ok=True)
        else:
            self._set_status("未连接", ok=False)
        try:
            self._log_label.config(text="\n".join(lines))
        except Exception:
            pass

    def _attach_async(self) -> None:
        self._set_status("正在连接微信…")
        threading.Thread(target=self._attach_worker, daemon=True).start()

    def _attach_worker(self) -> None:
        if not _wechat_running():
            self._ui(lambda: self._set_status("连接失败：微信未运行", ok=False))
            return
        db, info, err = _connect_db()
        if db is None:
            self._ui(lambda: self._set_status("连接失败：%s" % err, ok=False))
            return
        self._db = db
        self._cached_connected = True
        self._cached_running = True
        hook_ok = _hook_available()
        self._cached_hook = hook_ok
        nick = str(info.get("nick_name") or "")
        msg = "已连接：%s（%s）" % (nick or "微信", "hook" if hook_ok else "UIA/OCR 发送")
        self._cached_msg = msg
        self._ui(lambda: self._set_status(msg, ok=True))
        # 预热 GUI 实例，避免首次发送再花 100+ 秒
        try:
            _get_gui()
        except Exception:
            pass

    def _detach(self) -> None:
        self._db = None
        self._cached_connected = False
        self._set_status("已断开", ok=True)

    def send_text(self, wxid: str, text: str) -> bool:
        if not text:
            return False
        if self._cached_hook and _hook_send(str(wxid), str(text)):
            return True
        return _screen_send_text(str(wxid), str(text))

    def send_image(self, chat_id: str, image_path: str) -> bool:
        return _screen_send_file(str(chat_id), str(image_path))

    def check(self):
        if not self._cached_running:
            return False, "微信客户端未运行"
        if self._cached_connected:
            return True, self._cached_msg or "已连接"
        return False, self._cached_msg or "未连接"