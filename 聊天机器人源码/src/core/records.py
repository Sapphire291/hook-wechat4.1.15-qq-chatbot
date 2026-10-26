# -*- coding: utf-8 -*-
"""回复记录与安全告警的持久化（JSON Lines，追加式，崩溃安全）。"""
from __future__ import annotations

import json
import os
import threading
from typing import List, Optional

from .models import ReplyRecord, SecurityEvent
from .paths import data_dir


class RecordsStore:
    """追加式记录：已发送回复 + 注入/隐私告警。"""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or data_dir()
        os.makedirs(self.base_dir, exist_ok=True)
        self.replies_path = os.path.join(self.base_dir, "replies.jsonl")
        self.alerts_path = os.path.join(self.base_dir, "alerts.jsonl")
        self._lock = threading.RLock()

    # ---- 写 ----
    def add_reply(self, rec: ReplyRecord) -> None:
        self._append(self.replies_path, rec.to_dict())

    def add_alert(self, evt: SecurityEvent) -> None:
        self._append(self.alerts_path, evt.to_dict())

    # ---- 读（返回最近 N 条，供 UI 展示） ----
    def recent_replies(self, n: int = 100) -> List[ReplyRecord]:
        return self._read(self.replies_path, ReplyRecord.from_dict, n)

    def recent_alerts(self, n: int = 100) -> List[SecurityEvent]:
        return self._read(self.alerts_path, SecurityEvent.from_dict, n)

    # ---- 内部 ----
    def _append(self, path: str, obj: dict) -> None:
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _read(self, path: str, factory, n: int) -> list:
        if not os.path.exists(path):
            return []
        items = []
        with self._lock:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(factory(json.loads(line)))
                    except Exception:
                        continue
        return items[-n:]
