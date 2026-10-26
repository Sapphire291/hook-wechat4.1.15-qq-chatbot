# -*- coding: utf-8 -*-
"""插件加载器：扫描 mod/<插件名>/ 目录并加载插件。

插件加载失败只记录、不抛出，保证主程序不受影响。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import List, Optional

from .api import PluginBase, PluginContext


def discover_mod_dirs(mod_dir: str) -> List[str]:
    """返回 mod/ 下所有含 plugin.json 的子目录。"""
    if not mod_dir or not os.path.isdir(mod_dir):
        return []
    out: List[str] = []
    for name in sorted(os.listdir(mod_dir)):
        path = os.path.join(mod_dir, name)
        if name.startswith(".") or name.startswith("_"):
            continue
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "plugin.json")):
            out.append(path)
    return out


def load_plugin(plugin_dir: str, context: PluginContext) -> Optional[PluginBase]:
    """加载单个插件；任何异常都返回 None。"""
    try:
        meta_path = os.path.join(plugin_dir, "plugin.json")
        with open(meta_path, "r", encoding="utf-8-sig") as f:
            meta = json.load(f)
        entry = os.path.join(plugin_dir, "plugin.py")
        if not os.path.isfile(entry):
            return None
        module_name = "mod_" + os.path.basename(plugin_dir)
        spec = importlib.util.spec_from_file_location(module_name, entry)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        inserted = False
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
            inserted = True
        try:
            spec.loader.exec_module(module)
        finally:
            if inserted:
                try:
                    sys.path.remove(plugin_dir)
                except ValueError:
                    pass
        cls = getattr(module, "Plugin", None)
        if cls is None:
            return None
        plugin = cls(context)
        if not isinstance(plugin, PluginBase):
            # 允许未继承基类但鸭子类型相符的插件：补上必要属性
            pass
        if not getattr(plugin, "name", ""):
            plugin.name = str(meta.get("name") or os.path.basename(plugin_dir))
        if not getattr(plugin, "title", ""):
            plugin.title = str(meta.get("title") or plugin.name)
        if not getattr(plugin, "description", ""):
            plugin.description = str(meta.get("description") or "")
        if not getattr(plugin, "version", ""):
            plugin.version = str(meta.get("version") or "0.0.0")
        plugin.bind_dir(plugin_dir)
        return plugin
    except Exception:
        return None


def load_plugins(mod_dir: str, context: PluginContext) -> List[PluginBase]:
    """加载 mod/ 下全部插件（坏插件自动跳过）。"""
    plugins: List[PluginBase] = []
    for d in discover_mod_dirs(mod_dir):
        plugin = load_plugin(d, context)
        if plugin is not None:
            plugins.append(plugin)
    return plugins
