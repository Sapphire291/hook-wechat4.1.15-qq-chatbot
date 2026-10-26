# -*- coding: utf-8 -*-
"""入站消息序列（按人/群、按秒排序）+ 刷屏抑制。

需求：
- 机器人回复期间收到的消息不丢，按到达秒进入该会话序列，按顺序回复
- 同一会话在短时间内连续发送完全相同的内容 >= 4 次：第 4 次起不进入序列/不回复，
  由主 AI 回复并警告刷屏（每次刷屏只警告一次）
- 出现不同内容后恢复正常；之后 10 条内若再出现刚才的刷屏内容，也不进入序列且不回复
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple


class MessageSequencer:
    def __init__(self, now=None, spam_repeat: int = 4, spam_window: float = 120.0,
                 followup_window: int = 10):
        self._now = now or time.time
        self.spam_repeat = max(2, int(spam_repeat))
        self.spam_window = float(spam_window)
        self.followup_window = max(0, int(followup_window))
        self._pending: Dict[str, List[Tuple[float, int, object]]] = {}
        self._arrival = 0
        self._state: Dict[str, dict] = {}

    # ---------------- 状态 ----------------
    def _st(self, key: str) -> dict:
        st = self._state.get(key)
        if st is None:
            st = {"last_text": "", "count": 0, "last_t": 0.0,
                  "spam_active": False, "spam_text": "",
                  "followup": 0}
            self._state[key] = st
        return st

    @staticmethod
    def key_of(msg) -> str:
        return "%s:%s" % (msg.platform.value, msg.chat_id)

    def offer(self, msg) -> str:
        """返回: queue / spam_warn / suppress"""
        key = self.key_of(msg)
        text = str(getattr(msg, "content", "") or "").strip()
        now = self._now()
        st = self._st(key)

        if st["last_text"] == text:
            if (now - float(st["last_t"] or 0)) <= self.spam_window:
                st["count"] = int(st["count"]) + 1
            else:
                st["count"] = 1
        else:
            # 内容变了：如果之前在刷屏，进入观察窗口（保留原刷屏内容用于比对）
            had_spam = bool(st.get("spam_active")) or bool(st.get("spam_text"))
            if st.get("spam_active"):
                st["followup"] = self.followup_window
                st["spam_active"] = False
            st["last_text"] = text
            st["count"] = 1
            if not had_spam:
                st["spam_text"] = text
        st["last_t"] = now

        # 观察窗口：再次出现此前的刷屏内容 → 抑制且不回复
        if st.get("followup", 0) > 0:
            st["followup"] = int(st["followup"]) - 1
            if st.get("spam_text") and text == st["spam_text"]:
                return "suppress"

        if int(st["count"]) >= self.spam_repeat:
            warn = not bool(st["spam_active"])
            st["spam_active"] = True
            st["spam_text"] = text
            st["followup"] = self.followup_window
            return "spam_warn" if warn else "suppress"

        self._arrival += 1
        self._pending.setdefault(key, []).append(
            (float(getattr(msg, "timestamp", 0) or now), self._arrival, msg))
        self._pending[key].sort(key=lambda x: (int(x[0]), x[1]))
        return "queue"

    def pop_ready(self, key: Optional[str] = None) -> Optional[object]:
        """取该会话最早的一条（按秒序 + 到达序）。"""
        if key is None:
            if not self._pending:
                return None
            key = min(self._pending, key=lambda k: self._pending[k][0][0] if self._pending[k] else 0)
        lst = self._pending.get(key)
        if not lst:
            return None
        _, _, msg = lst.pop(0)
        if not lst:
            self._pending.pop(key, None)
        return msg

    def has_pending(self, key: str) -> bool:
        return bool(self._pending.get(key))

    def pending_count(self, key: Optional[str] = None) -> int:
        if key is None:
            return sum(len(v) for v in self._pending.values())
        return len(self._pending.get(key) or [])
