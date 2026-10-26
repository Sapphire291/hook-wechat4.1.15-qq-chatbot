# -*- coding: utf-8 -*-
"""路径工具：定位项目根目录 / 数据目录 / 资源目录。

兼容两种运行形态：
- 源码运行：__file__ 位于 <root>/src/core/paths.py
- PyInstaller 打包：sys.frozen 为真，根目录为 exe 所在目录
"""
from __future__ import annotations

import os
import sys


def project_root() -> str:
    """返回项目根目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # <root>/src/core/paths.py -> 上溯三级到 <root>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def data_home() -> str:
    """数据根目录（可整体搬迁）。

    优先级：
    1) 环境变量 ``CHATBOT_HOME``（部署/测试可显式指定）；
    2) exe 所在目录（打包运行）或源码根目录（源码运行）。

    这样 exe 连同数据目录一起复制到任意盘符都能直接运行，不依赖固定路径。
    """
    env = (os.environ.get("CHATBOT_HOME") or "").strip()
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    return project_root()


def data_dir() -> str:
    """数据目录（回复记录、告警记录）。"""
    d = os.path.join(data_home(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def memory_dir() -> str:
    """长期记忆根目录：<数据根>/memory（exe 同级；可用 CHATBOT_HOME 覆盖）。"""
    d = os.path.join(data_home(), "memory")
    os.makedirs(d, exist_ok=True)
    return d


#: 平台专属记忆子目录（微信 / QQ 完全独立计时、总结、标记、删除）
PLATFORM_MEMORY_DIRS = {
    "wechat": "wechatMM",
    "qq": "QQMM",
}


def platform_memory_dir(platform_value: str) -> str:
    """返回平台专属记忆目录：<数据根>/memory/wechatMM 或 /QQMM。"""
    key = str(platform_value or "").lower()
    folder = PLATFORM_MEMORY_DIRS.get(key, PLATFORM_MEMORY_DIRS["wechat"])
    d = os.path.join(memory_dir(), folder)
    os.makedirs(d, exist_ok=True)
    return d


def wid_dir() -> str:
    """账号档案目录：<数据根>/WID。"""
    d = os.path.join(data_home(), "WID")
    os.makedirs(d, exist_ok=True)
    return d


def emoje_dir() -> str:
    """账号级表情库根目录：<数据根>/EMOJE。"""
    d = os.path.join(data_home(), "EMOJE")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    """配置文件路径（exe 同级）。"""
    return os.path.join(project_root(), "config.json")


def assets_dir() -> str:
    """资源目录（本地表情图片库等）。"""
    return os.path.join(project_root(), "assets")


def resolve_path(value: str, default: str = "") -> str:
    """把配置里的路径解析成绝对路径（可移植）。

    - 相对路径：``assets/`` 开头按“随包资源目录”解析，其余按“数据根”解析；
    - 绝对路径：存在则原样用；不存在（例如换了盘符）则尝试数据根下的同名文件；
    - 空值用 default。
    """
    p = str(value or "").strip() or str(default or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        if os.path.exists(p):
            return p
        cand = os.path.join(data_home(), os.path.basename(p))
        if os.path.exists(cand):
            return cand
        return p
    norm = p.replace("\\", "/")
    if norm.startswith("assets/"):
        return os.path.join(project_root(), p)
    return os.path.join(data_home(), p)


def resolve_stored_file(path: str, search_dirs=()) -> str:
    """解析记忆/目录文件里保存的绝对文件路径：换盘符后按文件名在新目录里找。

    参数 search_dirs：按顺序尝试的目录（例如 <表情库>/wechat_fav、<表情库>/chat_stickers）。
    """
    p = str(path or "").strip()
    if not p:
        return ""
    if not os.path.isabs(p):
        for d in search_dirs:
            q = os.path.join(str(d), p)
            if os.path.isfile(q):
                return q
        q = os.path.join(data_home(), p)
        return q if os.path.isfile(q) else p
    if os.path.isfile(p):
        return p
    name = os.path.basename(p)
    for d in search_dirs:
        q = os.path.join(str(d), name)
        if os.path.isfile(q):
            return q
    q = os.path.join(data_home(), "EMOJE", name)
    return q if os.path.isfile(q) else p
