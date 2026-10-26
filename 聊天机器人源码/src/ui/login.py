# -*- coding: utf-8 -*-
"""NapCat 扫码登录窗口：在 exe 内直接显示二维码，无需开网页、无需手填 token。"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

_PHASE_TEXT = {
    "idle": "准备中…",
    "starting": "正在启动 NapCat…",
    "need_login": "请用手机 QQ 扫描下方二维码登录",
    "configuring": "登录成功，正在配置…",
    "connected": "登录成功，机器人已就绪",
    "failed": "启动失败（请检查 QQ 是否已安装）",
}


class NapCatLoginWindow(tk.Toplevel):
    """扫码登录窗口（可选关闭，稍后再登录）。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("QQ 登录")
        self.geometry("360x460")
        self.resizable(False, False)
        self._photo = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build(self) -> None:
        self.status_label = ttk.Label(self, text="正在启动 NapCat…", font=("", 12), wraplength=320)
        self.status_label.pack(pady=(18, 8))
        self.qr_label = ttk.Label(self, text="(等待二维码…)")
        self.qr_label.pack(pady=8)
        self.token_label = ttk.Label(self, text="", foreground="#888")
        self.token_label.pack(pady=(0, 4))
        ttk.Button(self, text="稍后再说", command=self.destroy).pack(pady=10)

    def update_state(self, phase: str, qrcode_path: str = "", token: str = "") -> None:
        try:
            self.status_label.config(text=_PHASE_TEXT.get(phase, phase))
        except Exception:
            pass
        if phase == "need_login":
            self._show_qr(qrcode_path)
        else:
            self._show_qr("")
        if token:
            try:
                self.token_label.config(text=f"登录密钥：{token}")
            except Exception:
                pass

    def _show_qr(self, path: str) -> None:
        if not _HAS_PIL or not path or not os.path.isfile(path):
            self.qr_label.config(image="", text="(等待二维码…)" if not path else "")
            self._photo = None
            return
        try:
            img = Image.open(path).resize((240, 240))
            self._photo = ImageTk.PhotoImage(img)
            self.qr_label.config(image=self._photo, text="")
        except Exception:
            self.qr_label.config(image="", text="")
            self._photo = None
