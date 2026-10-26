# -*- coding: utf-8 -*-
"""独立预警界面：统一展示「提示词注入」与「隐私信息刺探」两类告警。"""
from __future__ import annotations

import datetime
import tkinter as tk
from tkinter import ttk
from typing import List

from ..core.models import SecurityEvent, SecurityEventType

_TYPE_LABEL = {SecurityEventType.INJECTION: "提示词注入", SecurityEventType.PRIVACY: "隐私刺探"}


class AlertWindow(tk.Toplevel):
    """预警窗口（独立于主界面），展示告警列表。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("预警事件")
        self.geometry("760x360")
        self._build()

    def _build(self) -> None:
        cols = ("time", "type", "sender", "chat", "content")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, title, w, anchor in [
            ("time", "发生时间", 150, "w"),
            ("type", "类型", 90, "w"),
            ("sender", "发起人", 120, "w"),
            ("chat", "会话", 140, "w"),
            ("content", "内容", 360, "w"),
        ]:
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "content"))
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

    def add_alert(self, evt: SecurityEvent) -> None:
        self.tree.insert("", 0, values=(
            datetime.datetime.fromtimestamp(evt.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
            _TYPE_LABEL.get(evt.event_type, evt.event_type.value),
            evt.sender_name or evt.sender_id,
            evt.chat_name or evt.chat_id,
            evt.content,
        ))

    def refresh(self, events: List[SecurityEvent]) -> None:
        """幂等刷新：清空后按新到旧重载。"""
        for i in self.tree.get_children():
            self.tree.delete(i)
        for evt in reversed(events):
            self.add_alert(evt)
