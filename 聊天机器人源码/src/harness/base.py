# -*- coding: utf-8 -*-
"""Harness 内核接口（预留）。

定位：作为「旁挂」的智能体内核，负责需要规划/工具调用的任务；
聊天主链路仍走稳定的 prompt 组装（保证缓存命中与延迟）。

当前为空实现；将来可接：
- 本地 DSH/agent 服务（HTTP，如 http://127.0.0.1:8765）
- 其他 agent runtime（提供 run_task / recall 接口即可）
"""
from __future__ import annotations

import json
import urllib.request
from abc import ABC, abstractmethod
from typing import List, Optional


class AgentKernel(ABC):
    """智能体内核接口。"""

    name: str = "null"

    @abstractmethod
    def available(self) -> bool:
        ...

    @abstractmethod
    def run_task(self, chat_key: str, prompt: str, context: str = "") -> str:
        """执行一个需要规划/工具的任务，返回文本结果；失败返回空串。"""

    @abstractmethod
    def recall(self, query: str, chat_key: str = "", top_k: int = 5) -> List[str]:
        """检索与 query 相关的历史记忆片段（供上下文增强）。"""


class NullAgentKernel(AgentKernel):
    name = "null"

    def available(self) -> bool:
        return False

    def run_task(self, chat_key: str, prompt: str, context: str = "") -> str:
        return ""

    def recall(self, query: str, chat_key: str = "", top_k: int = 5) -> List[str]:
        return []


class HttpAgentKernel(AgentKernel):
    """HTTP 旁挂 harness（预留实现，config.harness_url 指向服务）。"""

    name = "http"

    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = max(3, int(timeout or 30))

    def available(self) -> bool:
        if not self.base_url:
            return False
        try:
            req = urllib.request.Request(self.base_url + "/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace") or "{}")

    def run_task(self, chat_key: str, prompt: str, context: str = "") -> str:
        try:
            out = self._post("/run", {"chat_key": chat_key, "prompt": prompt, "context": context})
            return str(out.get("text") or "")
        except Exception:
            return ""

    def recall(self, query: str, chat_key: str = "", top_k: int = 5) -> List[str]:
        try:
            out = self._post("/recall", {"query": query, "chat_key": chat_key, "top_k": top_k})
            items = out.get("items") or []
            return [str(x) for x in items if x][:top_k]
        except Exception:
            return []


def create_agent_kernel(config) -> AgentKernel:
    """按配置创建 harness 内核；默认空实现。"""
    if not bool(getattr(config, "harness_enabled", False)):
        return NullAgentKernel()
    url = str(getattr(config, "harness_url", "") or "")
    if not url:
        return NullAgentKernel()
    return HttpAgentKernel(url, int(getattr(config, "harness_timeout", 30) or 30))
