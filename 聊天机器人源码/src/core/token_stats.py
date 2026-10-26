# -*- coding: utf-8 -*-
"""Token 计费/缓存命中的本地观测。

每次模型调用记录一行 JSON 到 ``<memory>/token_usage.jsonl``：
- prompt_tokens / completion_tokens / total_tokens
- cached_tokens（命中前缀缓存的 token 数，若有）
- platform / model / kind / ts
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from typing import Dict, Optional

from .paths import memory_dir

_LOCK = threading.Lock()


def _path() -> str:
    d = memory_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "token_usage.jsonl")


def record(platform: str, model: str, usage: Dict, kind: str = "chat") -> None:
    """记录一次调用（usage 为 OpenAI 兼容格式）。"""
    try:
        u = usage or {}
        prompt = int(u.get("prompt_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
        total = int(u.get("total_tokens") or (prompt + completion))
        cached = 0
        det = u.get("prompt_tokens_details") or {}
        if isinstance(det, dict):
            cached = int(det.get("cached_tokens") or 0)
        if not cached:
            cached = int(u.get("prompt_cache_hit_tokens") or 0)
        item = {
            "ts": time.time(),
            "platform": str(platform or ""),
            "model": str(model or ""),
            "kind": str(kind or "chat"),
            "prompt": prompt,
            "completion": completion,
            "total": total,
            "cached": cached,
        }
        with _LOCK:
            with io.open(_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def summary(days: int = 7) -> Dict:
    """汇总最近 N 天的 token 使用与缓存命中。"""
    since = time.time() - max(1, int(days)) * 86400
    out = {"calls": 0, "prompt": 0, "completion": 0, "total": 0, "cached": 0,
           "cache_hit_rate": 0.0, "by_platform": {}}
    try:
        p = _path()
        if not os.path.isfile(p):
            return out
        with io.open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    it = json.loads(line)
                except Exception:
                    continue
                if float(it.get("ts") or 0) < since:
                    continue
                out["calls"] += 1
                out["prompt"] += int(it.get("prompt") or 0)
                out["completion"] += int(it.get("completion") or 0)
                out["total"] += int(it.get("total") or 0)
                out["cached"] += int(it.get("cached") or 0)
                plat = str(it.get("platform") or "")
                bp = out["by_platform"].setdefault(plat, {"calls": 0, "prompt": 0, "cached": 0})
                bp["calls"] += 1
                bp["prompt"] += int(it.get("prompt") or 0)
                bp["cached"] += int(it.get("cached") or 0)
        if out["prompt"] > 0:
            out["cache_hit_rate"] = round(out["cached"] / float(out["prompt"]), 4)
    except Exception:
        pass
    return out
