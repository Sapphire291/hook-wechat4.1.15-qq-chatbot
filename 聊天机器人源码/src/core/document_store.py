# -*- coding: utf-8 -*-
"""Per-conversation document storage + automatic retention cleanup.

Incoming WeChat/QQ office files are copied under:

    memory/wechatMM/<chat_id>/documents/
    memory/QQMM/<chat_id>/documents/

The cleanup loop is plain automation (not AI): copies older than 7 days are
deleted. WeChat's own msg/file originals are never touched.
"""
from __future__ import annotations

import os
import re
import time
from typing import List

from .paths import data_home

_SUB = {"wechat": "wechatMM", "qq": "QQMM"}
_LEGACY = ("wechat_docs", "qq_docs")


def _safe(chat_id: str) -> str:
    s = str(chat_id or "unknown")
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return s or "unknown"


def memory_platform_dir(platform: str = "wechat") -> str:
    sub = _SUB.get(str(platform or "wechat").lower(), "wechatMM")
    return os.path.join(data_home(), "memory", sub)


def documents_dir(platform: str = "wechat", chat_id: str = "") -> str:
    p = os.path.join(memory_platform_dir(platform), _safe(chat_id), "documents")
    os.makedirs(p, exist_ok=True)
    return p


def _cleanup_dir(path: str, cutoff: float) -> int:
    removed = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    removed += 1
            except Exception:
                pass
    return removed


def _document_dirs_under(base: str) -> List[str]:
    out: List[str] = []
    if not os.path.isdir(base):
        return out
    for root, dirs, _files in os.walk(base):
        if os.path.basename(root).lower() == "documents":
            out.append(root)
            dirs[:] = []  # no nested documents below a documents dir
    return out


def cleanup_old_documents(max_age_days: int = 7, now: float = None) -> int:
    """Delete bot-downloaded document copies older than max_age_days."""
    cutoff = float(now if now is not None else time.time()) - max(1, int(max_age_days)) * 86400
    total = 0
    for platform in ("wechat", "qq"):
        base = memory_platform_dir(platform)
        for d in _document_dirs_under(base):
            total += _cleanup_dir(d, cutoff)
    legacy_base = os.path.join(data_home(), "data")
    for d in _LEGACY:
        total += _cleanup_dir(os.path.join(legacy_base, d), cutoff)
    return total
