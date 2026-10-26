# -*- coding: utf-8 -*-
"""NapCat 插件：内置 QQ 接入（自动解压/注入/扫码登录/自动配 OneBot）。"""
from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk

try:
    from src.plugins import PluginBase
except Exception:
    from src.plugins.api import PluginBase

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:
    Image = None
    ImageTk = None
    _HAS_PIL = False

_PHASE_TEXT = {
    "idle": "准备中…",
    "starting": "正在启动 NapCat…",
    "need_login": "请用手机 QQ 扫码登录",
    "configuring": "登录成功，正在配置…",
    "connected": "已登录，机器人就绪",
    "failed": "启动失败（请检查 QQ 是否安装）",
}


def _find_napcat_zip() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "napcat_bundle.zip")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "napcat_bundle.zip")


class Plugin(PluginBase):
    name = "napcat"
    title = "NapCat(QQ)"
    description = "内置 NapCat：自动解压、注入 QQ、扫码登录、自动配置 OneBot（QQ 接入）。"
    version = "1.0.0"
    api_defaults = {
        "api_key": "",
        "base_url": "http://127.0.0.1:6099",
        "model": "napcat-webui",
    }

    def __init__(self, context=None):
        super().__init__(context)
        self.runtime = None
        self._status_label = None
        self._qr_label = None
        self._token_label = None
        self._qr_photo = None
        self._started = False

    def _ensure_runtime(self):
        if self.runtime is not None:
            return self.runtime
        # 优先复用核心已创建的 NapCat 运行时（避免双份生命周期）
        if getattr(self.context, "napcat", None) is not None:
            self.runtime = self.context.napcat
            return self.runtime
        try:
            from src.napcat_runtime import NapCatRuntime
            self.runtime = NapCatRuntime(self.context.config, bundled_zip=_find_napcat_zip())
        except Exception:
            self.runtime = None
        return self.runtime

    def build_content(self, parent) -> None:
        self._ensure_runtime()
        box = ttk.LabelFrame(parent, text="QQ 登录（NapCat 后台常驻）")
        box.pack(fill="x", pady=(0, 8))
        self._status_label = ttk.Label(box, text="未启动", font=("", 11))
        self._status_label.pack(anchor="w", padx=8, pady=(8, 4))
        qr_row = ttk.Frame(box)
        qr_row.pack(padx=8, pady=4)
        self._qr_label = ttk.Label(qr_row, text="")
        self._qr_label.pack(side="left")
        self._qr_label.bind("<Button-1>", lambda e: self._refresh_qr())
        self._qr_label.configure(cursor="hand2")
        ttk.Button(qr_row, text="刷新二维码", command=self._refresh_qr).pack(
            side="left", padx=(12, 0), anchor="n")
        self._token_label = ttk.Label(box, text="", foreground="#888")
        self._token_label.pack(anchor="w", padx=8, pady=(0, 8))
        if self.runtime is not None and not self._started:
            self._started = True
            if getattr(self.runtime, 'phase', 'idle') == 'idle':
                threading.Thread(target=self.runtime.run_lifecycle, name='napcat-plugin', daemon=True).start()

    def poll(self) -> None:
        runtime = self._ensure_runtime()
        if runtime is None or self._status_label is None:
            return
        phase = runtime.phase
        try:
            self._status_label.config(text=_PHASE_TEXT.get(phase, phase))
        except Exception:
            return
        if phase == "need_login":
            if self._qr_photo is None:
                self._show_qr(runtime.qrcode_path())
        else:
            self._qr_photo = None
            try:
                self._qr_label.config(image="", text="")
            except Exception:
                pass
        try:
            token = runtime.webui_token()
            self._token_label.config(text=("登录密钥：" + token) if token else "")
        except Exception:
            pass

    def _show_qr(self, path: str) -> None:
        if not _HAS_PIL or not path or not os.path.isfile(path):
            return
        try:
            img = Image.open(path).convert("L")
            scale = max(2, 320 // max(img.width, img.height))
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
            self._qr_photo = ImageTk.PhotoImage(img)
            self._qr_label.config(image=self._qr_photo, text="")
        except Exception:
            self._qr_photo = None

    def _refresh_qr(self) -> None:
        runtime = self._ensure_runtime()
        if runtime is None:
            return
        try:
            self._status_label.config(text="正在刷新二维码…")
        except Exception:
            pass
        threading.Thread(target=self._do_refresh, daemon=True).start()

    def _do_refresh(self) -> None:
        try:
            self._ensure_runtime().refresh_login()
        except Exception:
            pass
        self._qr_photo = None

    def check(self):
        runtime = self._ensure_runtime()
        if runtime is None:
            return False, "NapCat 运行组件未加载"
        webui = runtime.is_webui_ready(0.5)
        onebot = runtime.is_onebot_ready(0.5)
        if onebot:
            return True, "NapCat 已连接，OneBot 3001 就绪"
        if webui:
            return True, "NapCat 运行中（WebUI 6099），等待扫码登录"
        return False, "NapCat 未运行（请等待启动或检查 QQ 是否安装）"