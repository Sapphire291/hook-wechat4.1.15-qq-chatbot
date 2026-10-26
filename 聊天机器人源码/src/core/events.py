# -*- coding: utf-8 -*-
"""事件基础设施：线程安全消息队列 + 轻量事件总线。"""
from __future__ import annotations

import queue
import threading
from typing import Callable, List

from .models import Message, ReplyRecord, SecurityEvent


class MessageQueue:
    """接入层 -> 处理层 之间的线程安全消息队列。"""

    def __init__(self):
        self._q: "queue.Queue[Message]" = queue.Queue()

    def put(self, msg: Message) -> None:
        self._q.put(msg)

    def get(self, timeout: float = 1.0):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def size(self) -> int:
        return self._q.qsize()


class EventBus:
    """轻量事件总线：UI 订阅安全告警 / 回复记录等事件。"""

    def __init__(self):
        self._alert_handlers: List[Callable[[SecurityEvent], None]] = []
        self._reply_handlers: List[Callable[[ReplyRecord], None]] = []
        self._lock = threading.RLock()

    def on_alert(self, fn: Callable[[SecurityEvent], None]) -> None:
        with self._lock:
            self._alert_handlers.append(fn)

    def on_reply(self, fn: Callable[[ReplyRecord], None]) -> None:
        with self._lock:
            self._reply_handlers.append(fn)

    def emit_alert(self, evt: SecurityEvent) -> None:
        with self._lock:
            handlers = list(self._alert_handlers)
        for h in handlers:
            try:
                h(evt)
            except Exception:
                pass

    def emit_reply(self, rec: ReplyRecord) -> None:
        with self._lock:
            handlers = list(self._reply_handlers)
        for h in handlers:
            try:
                h(rec)
            except Exception:
                pass
