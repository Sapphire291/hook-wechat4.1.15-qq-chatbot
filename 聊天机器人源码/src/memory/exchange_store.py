# -*- coding: utf-8 -*-
"""完整交流记录（只追加，单文件）：data/exchanges.jsonl

- 与「回复记录」(data/replies.jsonl) 同目录；回复记录 UI 仍只显示机器人回复。
- 需要完整交流记录时从这里调取（按 chat 过滤）。
"""
from __future__ import annotations

import json
import os
import threading
import time


def _date(ts=None):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts or time.time())))
    except Exception:
        return ""


class ExchangeStore:
    def __init__(self, path: str = None):
        if not path:
            from ..core.paths import data_dir
            path = os.path.join(data_dir(), "exchanges.jsonl")
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()

    def append(self, platform: str, chat: str, chat_name: str, role: str,
               sender: str, text: str, ts=None) -> int:
        text = str(text or "")
        if not text.strip():
            return 0
        rec = {
            "t": float(ts or time.time()),
            "d": _date(ts),
            "platform": str(platform or ""),
            "chat": str(chat or ""),
            "chat_name": str(chat_name or ""),
            "role": str(role or "user"),
            "sender": str(sender or ""),
            "text": text,
        }
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                return 0
            return 1

    def read(self, chat: str = None, limit: int = 100000):
        out = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if chat and str(rec.get("chat") or "") != str(chat):
                        continue
                    out.append(rec)
                    if len(out) >= limit:
                        break
        except Exception:
            pass
        return out

    def chats(self):
        """{chat: {'count': n, 'name': 显示名}}"""
        res = {}
        for rec in self.read(limit=10000000):
            c = str(rec.get("chat") or "")
            if not c:
                continue
            item = res.setdefault(c, {"count": 0, "name": rec.get("chat_name") or ""})
            item["count"] += 1
            if not item["name"] and rec.get("chat_name"):
                item["name"] = rec["chat_name"]
        return res

    def count(self) -> int:
        n = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        n += 1
        except Exception:
            pass
        return n
