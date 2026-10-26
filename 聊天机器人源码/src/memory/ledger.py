# -*- coding: utf-8 -*-
"""墨 · 只追加账本（仿 dsh-agent-soul 的 ledger）。

原则（照抄仓库）：
  - 文件只被追加，从不重写（各写各的行 → 不需要锁 → 读的时候按时间合并）
  - 每行 = 一条事实：谁、何时、做了什么、对象、结果
  - 不依赖时间戳排序（同一秒并发也安全）：id 单调 + t 记录

用途：记忆压缩/吊销/裁剪、回复发送、配置变更等关键动作留痕，可审计、可回放。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple


def _now() -> float:
    return time.time()


#: 共享账本缓存：微信/QQ 共用同一份记忆时，只能有一个 Ledger 写同一个文件，否则 id 会撞
_SHARED_LEDGERS = {}
_SHARED_LEDGERS_LOCK = threading.Lock()


class Ledger:
    """单平台只追加账本：<memory>/<wechatMM|QQMM>/ledger/ledger.jsonl。"""

    @classmethod
    def shared(cls, root: str, platform: str = "shared") -> "Ledger":
        """同一 root 只返回一个实例（微信/QQ 共用一个账本文件时防止 id 重复）。"""
        with _SHARED_LEDGERS_LOCK:
            key = os.path.abspath(str(root or ""))
            obj = _SHARED_LEDGERS.get(key)
            if obj is None:
                obj = cls(platform, root=root)
                _SHARED_LEDGERS[key] = obj
            return obj

    def __init__(self, platform: str, root: Optional[str] = None):
        self.platform = str(platform or "wechat").lower()
        if root is None:
            from ..core.paths import platform_memory_dir
            root = platform_memory_dir(self.platform)
        self.dir = os.path.join(root, "ledger")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "ledger.jsonl")
        self._lock = threading.Lock()
        self._next_id = self._scan_next_id()

    def _scan_next_id(self) -> int:
        n = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        n = max(n, int(json.loads(line).get("id") or 0))
                    except Exception:
                        pass
        except Exception:
            pass
        return n + 1

    def record(self, action: str, target: str = "", detail: Optional[dict] = None,
               actor: str = "bot", platform: Optional[str] = None) -> int:
        """追加一条账本记录（永不覆盖）。platform 可覆盖默认平台（共享账本时区分来源）。"""
        with self._lock:
            rec = {
                "id": self._next_id,
                "t": _now(),
                "platform": str(platform or self.platform),
                "actor": str(actor or "bot"),
                "action": str(action or ""),
                "target": str(target or ""),
                "detail": detail or {},
            }
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                return 0
            self._next_id += 1
            return rec["id"]

    # ---------------- 读 ----------------
    def tail(self, limit: int = 20) -> List[dict]:
        out: List[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
        return out[-limit:]

    def stats(self) -> dict:
        recs = self.tail(limit=1000000)
        by_action: Dict[str, int] = {}
        for r in recs:
            by_action[r.get("action") or "?"] = by_action.get(r.get("action") or "?", 0) + 1
        size = 0
        try:
            size = os.path.getsize(self.path)
        except Exception:
            pass
        return {
            "platform": self.platform,
            "path": self.path,
            "records": len(recs),
            "by_action": by_action,
            "bytes": size,
            "last_t": recs[-1].get("t") if recs else 0,
        }

    def verify(self) -> Tuple[bool, List[str]]:
        """完整性校验：每行可解析、id 严格递增、字段齐全。"""
        errs: List[str] = []
        last_id = 0
        n = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    n += 1
                    try:
                        rec = json.loads(line)
                    except Exception as e:
                        errs.append("第 %d 行 JSON 损坏: %r" % (i, e))
                        continue
                    rid = int(rec.get("id") or 0)
                    if rid <= last_id:
                        errs.append("第 %d 行 id=%s 非递增（上一条 %s）" % (i, rid, last_id))
                    last_id = rid
                    if not rec.get("t") or not rec.get("action"):
                        errs.append("第 %d 行缺少 t/action" % i)
        except FileNotFoundError:
            return True, []
        except Exception as e:
            errs.append("读取失败: %r" % (e,))
        return (not errs), errs
