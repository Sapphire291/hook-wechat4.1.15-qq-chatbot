# -*- coding: utf-8 -*-
"""回复频率策略：决定机器人「是否回复」某条消息，按会话分别维护状态。

支持三种可切换、可配参的策略（见 :class:`FrequencyStrategy`）：

- ``INTERVAL``（按时间间隔）：同一会话距上次回复满 ``frequency_value`` 秒才允许再次回复。
- ``COUNT``（按消息条数）：同一会话每累计收到 ``frequency_value`` 条消息回复一次，回复后计数清零。
- ``MENTION_ONLY``（仅被 @ 时）：仅当 ``message.mentions_me`` 为 ``True`` 才回复。

所有会话状态按 ``(platform, chat_id)`` 键独立维护，并用一把 ``threading.Lock``
保护共享状态，保证多线程下的一致性与线程安全；``reset()`` 清空全部状态。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

from ..core.interfaces import FrequencyPolicy
from ..core.models import AppConfig, FrequencyStrategy, Message


class FrequencyPolicyImpl(FrequencyPolicy):
    """基于 ``AppConfig`` 的回复频率策略实现。

    参数 ``clock`` 用于注入时间源（便于测试模拟时间推进），默认使用 ``time.time``。
    """

    def __init__(self, config: AppConfig, clock: Optional[Callable[[], float]] = None):
        self.config = config
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._lock = threading.Lock()
        # 会话键 -> 上次回复时间（Unix 秒），用于 INTERVAL
        self._last_reply_times: Dict[str, float] = {}
        # 会话键 -> 已累计收到的消息条数，用于 COUNT
        self._counts: Dict[str, int] = {}

    @staticmethod
    def _chat_key(message: Message) -> str:
        """生成会话唯一键：platform + chat_id。"""
        return f"{message.platform.value}:{message.chat_id}"

    def should_reply(self, message: Message) -> bool:
        """判断是否允许对该消息回复。"""
        strategy = self.config.frequency_strategy

        if strategy == FrequencyStrategy.INTERVAL:
            return self._should_reply_interval(message)

        if strategy == FrequencyStrategy.COUNT:
            return self._should_reply_count(message)

        # MENTION_ONLY：无跨消息状态，直接依据是否 @ 机器人判断。
        return bool(message.mentions_me)

    def _should_reply_interval(self, message: Message) -> bool:
        """INTERVAL：距上次回复 >= frequency_value 秒才允许（最小 0.1 秒）。"""
        try:
            interval = max(0.1, float(self.config.frequency_value or 0.1))
        except (TypeError, ValueError):
            interval = 10.0
        key = self._chat_key(message)
        now = self._clock()
        with self._lock:
            last = self._last_reply_times.get(key)
            if last is None:
                # 该会话尚未回复过，允许首次回复。
                return True
            return (now - last) >= interval

    def _should_reply_count(self, message: Message) -> bool:
        """COUNT：每累计 frequency_value 条消息回复一次。"""
        try:
            threshold = max(1, int(round(float(self.config.frequency_value or 1))))
        except (TypeError, ValueError):
            threshold = 1
        key = self._chat_key(message)
        with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            return count >= threshold

    def wait_seconds(self, message: Message) -> float:
        """INTERVAL 策略下距允许回复还需等待的秒数（0=现在可回）。"""
        try:
            if self.config.frequency_strategy != FrequencyStrategy.INTERVAL:
                return 0.0
            interval = max(0.1, float(self.config.frequency_value or 0.1))
            key = self._chat_key(message)
            with self._lock:
                last = self._last_reply_times.get(key)
                if last is None:
                    return 0.0
                return max(0.0, interval - (self._clock() - last))
        except Exception:
            return 0.0

    def on_reply_sent(self, message: Message) -> None:
        """回复发送后回填状态。

        - INTERVAL：记录该会话最近回复时间。
        - COUNT：回复后计数清零，重新开始累计。
        - MENTION_ONLY：无状态，忽略。
        """
        strategy = self.config.frequency_strategy
        key = self._chat_key(message)
        with self._lock:
            if strategy == FrequencyStrategy.INTERVAL:
                self._last_reply_times[key] = self._clock()
            elif strategy == FrequencyStrategy.COUNT:
                self._counts[key] = 0
            # MENTION_ONLY 无跨消息状态，什么都不做。

    def reset(self) -> None:
        """重置所有会话的计数 / 时间状态。"""
        with self._lock:
            self._last_reply_times.clear()
            self._counts.clear()


def create_frequency_policy(config: AppConfig) -> FrequencyPolicy:
    """工厂：由配置创建频率策略实例。"""
    return FrequencyPolicyImpl(config)
