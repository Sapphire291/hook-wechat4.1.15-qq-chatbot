# -*- coding: utf-8 -*-
"""Harness 会话/记忆内核。

设计目标：
- bot 只负责接入/安全/表情/联网等“能力层”，不再每轮自己拼记忆；
- harness 保存会话（近期上下文）与 ECHO 记忆（按联系人摘要），决定本轮给模型注入什么；
- harness 直接调用生成器返回回复，使 bot 侧 system（宪章+底层提示词+每轮能力提示）保持稳定；
- 支持“记忆合并组”：白名单里把多个会话合并成同一个记忆/会话；未合并的保持独立。

实现分两种：
- LocalHarness：项目内进程内实现（默认，复用 `memory.chat_memory.ContactMemory`）
- HttpHarness：预留外部 harness 服务（POST /chat、/record、/history），默认不用
"""
from .base import (HarnessKernel, HarnessReply, HttpHarness, LocalHarness,
                   create_harness, resolve_memory_target)
try:
    from .native import NativeHarness
except Exception:  # pragma: no cover
    NativeHarness = None  # type: ignore

__all__ = ["HarnessKernel", "HarnessReply", "LocalHarness", "HttpHarness",
           "NativeHarness", "create_harness", "resolve_memory_target"]
