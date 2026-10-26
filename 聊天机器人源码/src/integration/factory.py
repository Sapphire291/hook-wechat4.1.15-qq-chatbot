# -*- coding: utf-8 -*-
"""接入层工厂：按配置构建启用的平台适配器。"""
from __future__ import annotations

from typing import Dict

from ..core.interfaces import ChatAdapter
from ..core.models import AppConfig, Platform
from .qq import QQAdapter
from .wechat import WeChatAdapter


def create_adapters(config: AppConfig) -> Dict[Platform, ChatAdapter]:
    """根据配置构建已启用平台的适配器；未启用的平台不加入。

    适配器实例化本身不连接任何外部服务（外部库均懒导入），
    实际连接发生在调用 start() 时，因此本函数始终安全可调用。
    """
    adapters: Dict[Platform, ChatAdapter] = {}
    if config.wechat_enabled:
        adapters[Platform.WECHAT] = WeChatAdapter(config)
    if config.qq_enabled:
        adapters[Platform.QQ] = QQAdapter(config)
    return adapters
