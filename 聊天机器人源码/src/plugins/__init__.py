# -*- coding: utf-8 -*-
"""插件体系：插件基类、上下文与加载器。"""
from .api import PluginBase, PluginContext
from .loader import discover_mod_dirs, load_plugin, load_plugins

__all__ = [
    "PluginBase",
    "PluginContext",
    "load_plugins",
    "load_plugin",
    "discover_mod_dirs",
]
