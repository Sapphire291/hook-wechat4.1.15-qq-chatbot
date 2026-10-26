# -*- coding: utf-8 -*-
"""接入层：QQ / 微信消息读取与发送（任务 2 实现）。"""
from __future__ import annotations

from .factory import create_adapters
from .qq import QQAdapter
from .wechat import WeChatAdapter

__all__ = ["WeChatAdapter", "QQAdapter", "create_adapters"]
