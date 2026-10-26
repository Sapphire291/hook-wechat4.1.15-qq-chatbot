# -*- coding: utf-8 -*-
"""插件 API：插件基类与运行时上下文。

插件放在 mod/<插件名>/ 目录下，必须包含：
- plugin.json   : {"name": "...", "title": "...", "description": "...", "version": "..."}
- plugin.py     : 定义 class Plugin(PluginBase)，可选覆盖 build_content / on_start / on_stop / check
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple


class PluginContext:
    """插件运行时上下文：给插件访问核心能力的入口（配置/事件/数据目录等）。"""

    def __init__(self, config: Any = None, config_store: Any = None,
                 bus: Any = None, queue: Any = None,
                 adapters: Optional[dict] = None, memory_store: Any = None,
                 data_dir: str = "", mod_dir: str = "", napcat: Any = None):
        self.config = config
        self.config_store = config_store
        self.bus = bus
        self.queue = queue
        self.adapters = adapters or {}
        self.memory_store = memory_store
        self.data_dir = data_dir
        self.mod_dir = mod_dir
        self.napcat = napcat

    def log(self, message: str) -> None:
        try:
            import logging
            logging.getLogger("plugin").info(message)
        except Exception:
            pass


class PluginBase:
    """插件基类。子类覆盖 name/title/build_content/check 等。"""

    name: str = ""
    title: str = ""
    description: str = ""
    version: str = "0.0.0"
    # 每个插件预置的 API 接口默认值（可在插件页修改）
    api_defaults: Dict[str, str] = {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    }

    def __init__(self, context: Optional[PluginContext] = None):
        self.context = context or PluginContext()
        self.plugin_dir: str = ""
        self.api: Dict[str, str] = dict(self.api_defaults)
        self._config_path: str = ""

    # ------------------------------------------------------------------ #
    # 配置持久化（mod/<name>/config.json）
    # ------------------------------------------------------------------ #
    def bind_dir(self, plugin_dir: str) -> None:
        self.plugin_dir = plugin_dir
        self._config_path = os.path.join(plugin_dir, "config.json")
        self.load_api()

    def load_api(self) -> None:
        self.api = dict(self.api_defaults)
        if not self._config_path or not os.path.isfile(self._config_path):
            return
        try:
            with open(self._config_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in self.api_defaults:
                    if k in data:
                        self.api[k] = str(data.get(k) or "")
        except Exception:
            pass

    def save_api(self) -> None:
        if not self._config_path:
            return
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(self.api, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 生命周期（子类可覆盖）
    # ------------------------------------------------------------------ #
    def build_content(self, parent) -> None:
        """在插件标签页内构建插件自定义 UI。"""
        pass

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # 自检：返回 (是否就绪, 说明文字)
    # ------------------------------------------------------------------ #
    def check(self) -> Tuple[bool, str]:
        return True, "插件已加载"
