# -*- coding: utf-8 -*-
"""长期记忆与对话历史存储。

- MemoryStore：按会话保存「关键信息」条目（JSON 文件），可被 AI 长期读取。
- HistoryStore：按会话保存原始对话历史（jsonl 文件）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List


class MemoryStore:
    """长期记忆：{chat_key: [{"time": float, "text": str}, ...]}"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, List[dict]] = {}
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            if os.path.isfile(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._data = {
                        str(k): [e for e in v if isinstance(e, dict)]
                        for k, v in data.items() if isinstance(v, list)
                    }
        except Exception:
            self._data = {}

    def reload(self) -> None:
        with self._lock:
            self._load()

    def _save_locked(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 查询 ----
    def chats(self) -> List[str]:
        with self._lock:
            return list(self._data.keys())

    def entries(self, chat_key: str) -> List[dict]:
        with self._lock:
            return [dict(e) for e in self._data.get(chat_key, [])]

    def render(self, chat_key: str) -> str:
        """渲染成给 AI 的长期记忆文本。"""
        with self._lock:
            items = list(self._data.get(chat_key, []))
        lines = [f"- {e.get('text', '')}" for e in items if e.get("text")]
        return "\n".join(lines)

    def count(self, chat_key: str) -> int:
        with self._lock:
            return len(self._data.get(chat_key, []))

    # ---- 修改 ----
    def add(self, chat_key: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._data.setdefault(chat_key, []).append({"time": time.time(), "text": text})
            self._save_locked()

    def update(self, chat_key: str, index: int, text: str) -> bool:
        with self._lock:
            items = self._data.get(chat_key, [])
            if 0 <= index < len(items):
                items[index]["text"] = text
                self._save_locked()
                return True
            return False

    def delete(self, chat_key: str, index: int) -> bool:
        with self._lock:
            items = self._data.get(chat_key, [])
            if 0 <= index < len(items):
                del items[index]
                self._save_locked()
                return True
            return False

    def clear(self, chat_key: str) -> None:
        with self._lock:
            self._data.pop(chat_key, None)
            self._save_locked()


def _safe_name(chat_key: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in chat_key)
    return safe[:120] or "chat"


class HistoryStore:
    """对话历史：每个会话一个 jsonl 文件。"""

    def __init__(self, directory: str):
        self.directory = directory

    def _path(self, chat_key: str) -> str:
        os.makedirs(self.directory, exist_ok=True)
        return os.path.join(self.directory, _safe_name(chat_key) + ".jsonl")

    def append(self, chat_key: str, role: str, content: str, ts: float = None) -> None:
        try:
            path = self._path(chat_key)
            item = {"role": role, "content": content,
                    "ts": float(ts if ts is not None else time.time())}
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _read(self, chat_key: str, limit: int = None) -> List[dict]:
        try:
            path = self._path(chat_key)
            if not os.path.isfile(path):
                return []
            out: List[dict] = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(item, dict) and "role" in item and "content" in item:
                        if "ts" not in item:
                            item["ts"] = 0.0
                        out.append(item)
            return out[-limit:] if limit else out
        except Exception:
            return []

    def load(self, chat_key: str, limit: int = 400) -> List[dict]:
        return self._read(chat_key, limit)

    def load_all(self, chat_key: str, max_items: int = 5000) -> List[dict]:
        """加载全部历史（用于启动时恢复短期记忆）。"""
        return self._read(chat_key, max_items)
