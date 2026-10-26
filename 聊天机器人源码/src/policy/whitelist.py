# -*- coding: utf-8 -*-
"""白名单策略：硬性规则，决定机器人是否允许回复某个会话。

规则：
- 当 ``config.whitelist_enabled`` 为 ``False`` 时，放行所有消息（不做白名单限制）。
- 开启白名单后，仅当消息的 ``platform`` 与 ``chat_id`` 同时命中某条白名单条目时放行；
  白名单之外的任何会话一律拦截（返回 ``False``）。

白名单是最高优先级的安全边界：即使后续频率策略允许回复，白名单未通过也必须拦截，
因此主流程应在进入频率策略之前先调用 :meth:`WhitelistPolicyImpl.is_allowed`。
"""
from __future__ import annotations

from typing import List, Tuple

from ..core.interfaces import WhitelistPolicy
from ..core.models import AppConfig, Message


class WhitelistPolicyImpl(WhitelistPolicy):
    """基于 ``AppConfig.whitelist`` 的白名单实现。"""

    def __init__(self, config: AppConfig):
        self.config = config
        # 预构建 (platform, chat_id) 集合，匹配时 O(1) 查询，且避免每次调用重建。
        self._allowed_keys: List[Tuple[str, str]] = []
        self._rebuild()

    def _rebuild(self) -> None:
        """由 ``config.whitelist`` 重新生成匹配键集合。"""
        self._allowed_keys = [
            (entry.platform.value, entry.chat_id)
            for entry in self.config.whitelist
        ]

    def is_allowed(self, message: Message) -> bool:
        """判断消息所在会话是否在白名单内。

        硬性规则：
        - ``whitelist_enabled`` 为 ``False`` → 放行所有；
        - 否则仅当 ``(platform, chat_id)`` 命中白名单才放行。
        """
        if not self.config.whitelist_enabled:
            return True
        # 每次调用前重建，以支持运行时在界面里增删白名单条目后立即生效。
        self._rebuild()
        platform = message.platform.value
        if (platform, message.chat_id) in self._allowed_keys:
            return True
        # 兼容旧配置：白名单里可能还是显示名/备注；同步到 ID 前也允许按会话名匹配
        if message.chat_name and (platform, message.chat_name) in self._allowed_keys:
            return True
        return False


def create_whitelist_policy(config: AppConfig) -> WhitelistPolicy:
    """工厂：由配置创建白名单策略实例。"""
    return WhitelistPolicyImpl(config)
