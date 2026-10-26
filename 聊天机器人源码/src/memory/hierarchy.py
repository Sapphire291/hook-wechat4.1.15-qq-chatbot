# -*- coding: utf-8 -*-
"""分层记忆（Hierarchical Memory）

规则（按用户要求）：
- 短期窗口 = 3 天；窗口按「首次接入 bot 的时间」为第 0 天对齐：[0,3)、[3,6)、[6,9)…
- 每满 3 天：把该 3 天窗口的原始记忆交给 AI 总结 → 一级总结（权重 = 结束天，如 3/6/9）；
  原始记忆打上已总结标记：仅保存在文件里，不再进入上下文。
- 每凑满 3 个同级总结（例如 3 个一级 = 9 天）：删除这 9 天的短期记忆（从活跃上下文移除、
  文件归档到 archive/），并对这 3 个总结做二次总结（权重 = 结束天，如 9/18/27）。
- 三级、四级……按同样规律递归：3 个同级总结 → 上一级总结，权重随覆盖天数增大。
- 活跃上下文 = 当前 3 天窗口原始对话 + 所有未归档的各层总结（按权重从大到小）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

DAY = 86400.0


def _day_index(ts: float, epoch: float) -> int:
    return int((ts - epoch) // DAY)


class HierarchicalMemory:
    """单个会话的分层记忆（文件存放在 memory/hierarchy/<chat>/）。"""

    def __init__(self, chat_key: str, base_dir: str, period_days: int = 3,
                 merge_count: int = 3, summarize=None, log=None):
        self.chat_key = chat_key
        self.base_dir = os.path.join(base_dir, _safe(chat_key))
        self.period_days = max(1, int(period_days))
        self.merge_count = max(2, int(merge_count))
        self._summarize = summarize  # callable(text, kind) -> str
        self._log = log or (lambda m: None)
        os.makedirs(os.path.join(self.base_dir, "archive"), exist_ok=True)
        self.state = self._load_state()
        self._raw: Optional[List[dict]] = None

    # ------------------------------------------------------------------ #
    # 状态/文件
    # ------------------------------------------------------------------ #
    @property
    def _state_path(self) -> str:
        return os.path.join(self.base_dir, "state.json")

    def _load_state(self) -> dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict):
                st.setdefault("nodes", [])
                return st
        except Exception:
            pass
        return {"epoch": 0.0, "chunk_start": 0.0, "chunk_index": 0,
                "next_id": 1, "nodes": []}

    def _save_state(self) -> None:
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._state_path)
        except Exception as e:
            self._log("[mem] save state failed: %r" % (e,))

    def _node_file(self, node: dict) -> str:
        start_d = _day_index(node["start"], self.state["epoch"])
        end_d = _day_index(node["end"], self.state["epoch"])
        return os.path.join(self.base_dir, "L%d_%04d_%04d.json" % (node["level"], start_d, end_d))

    def _write_node(self, node: dict) -> None:
        try:
            with open(self._node_file(node), "w", encoding="utf-8") as f:
                json.dump(node, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _archive_node(self, node: dict) -> None:
        try:
            src = self._node_file(node)
            if os.path.isfile(src):
                dst = os.path.join(self.base_dir, "archive", os.path.basename(src))
                os.replace(src, dst)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 原始消息缓存
    # ------------------------------------------------------------------ #
    def load_raw(self, history_store=None) -> List[dict]:
        if self._raw is not None:
            return self._raw
        raw: List[dict] = []
        if history_store is not None:
            try:
                raw = history_store.load_all(self.chat_key)
            except Exception:
                raw = []
        self._raw = raw
        return raw

    def set_raw(self, entries: List[dict]) -> None:
        self._raw = entries

    def add_message(self, role: str, content: str, ts: float) -> None:
        if self._raw is None:
            self._raw = []
        self._raw.append({"role": role, "content": content, "ts": ts})

    # ------------------------------------------------------------------ #
    # 上下文
    # ------------------------------------------------------------------ #
    def chunk_start(self, now: float) -> float:
        """当前活跃 3 天窗口的起点（时间戳）。"""
        if not self.state.get("epoch"):
            self.state["epoch"] = now
            self.state["chunk_start"] = now
            self._save_state()
        return float(self.state.get("chunk_start") or now)

    def active_nodes(self) -> List[dict]:
        return [n for n in self.state.get("nodes", []) if not n.get("archived")]

    def render_context(self, max_chars: int = 6000) -> str:
        """把活跃总结按权重从大到小渲染成文本（权重大的更重要，放更前面）。"""
        nodes = sorted(self.active_nodes(), key=lambda n: -float(n.get("weight") or 0))
        lines: List[str] = []
        used = 0
        for n in nodes:
            start_d = _day_index(n["start"], self.state["epoch"])
            end_d = _day_index(n["end"], self.state["epoch"])
            head = "【记忆 L%d｜第%d~%d天｜权重%g】" % (
                int(n["level"]), start_d, end_d, float(n.get("weight") or 0))
            text = head + "\n" + str(n.get("summary") or "").strip()
            if used + len(text) > max_chars and lines:
                break
            lines.append(text)
            used += len(text) + 2
        return "\n\n".join(lines)

    # ------------------------------------------------------------------ #
    # 窗口推进 + 总结 + 合并
    # ------------------------------------------------------------------ #
    def ensure(self, now: float, history_store=None) -> None:
        raw = self.load_raw(history_store) if self._raw is None else self._raw
        if not raw:
            return
        epoch = float(self.state.get("epoch") or 0.0)
        if not epoch:
            pos = [float(e.get("ts") or 0) for e in raw if float(e.get("ts") or 0) > 0]
            epoch = float(min(pos)) if pos else float(now - self.period_days * DAY)
            self.state["epoch"] = epoch
            self.state["chunk_start"] = epoch
            self._save_state()
        # 防呆：epoch 过旧（如旧数据无时间戳被当成 1970）时，收缩到最近 400 天内
        if epoch < now - 400 * DAY:
            epoch = now - 400 * DAY
            self.state["epoch"] = epoch
            self.state["chunk_start"] = max(float(self.state.get("chunk_start") or epoch), epoch)
            self._save_state()
        period = self.period_days * DAY
        start = float(self.state.get("chunk_start") or epoch)
        processed = 0
        t0 = time.time()
        while now >= start + period and processed < 60 and (time.time() - t0) < 120:
            end = start + period
            msgs = [e for e in raw if start <= float(e.get("ts") or 0) < end]
            if msgs:
                summary = self._summarize_chunk(msgs, start, end)
                if not summary:
                    # 总结失败：停在本窗口，下次再试（不丢消息）
                    self._log("[mem] chunk summary failed day %d; stop here"
                              % _day_index(start, epoch))
                    break
                self._new_node(1, start, end, summary, len(msgs))
            start = end
            self.state["chunk_start"] = start
            self.state["chunk_index"] = int(self.state.get("chunk_index") or 0) + 1
            self._save_state()
            self._cascade_merge()
            processed += 1

    def _new_node(self, level: int, start: float, end: float, summary: str, count: int) -> dict:
        nid = int(self.state.get("next_id") or 1)
        self.state["next_id"] = nid + 1
        weight = _day_index(end, float(self.state["epoch"]))
        node = {"id": nid, "level": int(level), "start": float(start), "end": float(end),
                "weight": float(weight), "summary": str(summary), "count": int(count),
                "archived": False, "created": time.time()}
        self.state.setdefault("nodes", []).append(node)
        self._write_node(node)
        self._log("[mem] new L%d node day %d weight %g (%d msgs)"
                  % (node["level"], weight, node["weight"], count))
        return node

    def _summarize_chunk(self, msgs: List[dict], start: float, end: float) -> str:
        if not self._summarize:
            return ""
        lines = []
        for e in sorted(msgs, key=lambda x: float(x.get("ts") or 0)):
            d = _day_index(float(e.get("ts") or start), float(self.state["epoch"]))
            who = "对方" if e.get("role") == "user" else "我"
            lines.append("第%d天 %s: %s" % (d, who, str(e.get("content") or "")[:500]))
        text = "\n".join(lines)[:20000]
        try:
            return self._summarize(text, "chunk") or ""
        except Exception as e:
            self._log("[mem] summarize chunk failed: %r" % (e,))
            return ""

    def _cascade_merge(self) -> None:
        """同级活跃节点凑满 merge_count 个 → 上层总结（可能级联）。"""
        while True:
            levels = {}
            for n in self.state.get("nodes", []):
                if n.get("archived"):
                    continue
                levels.setdefault(int(n["level"]), []).append(n)
            merged = False
            for level in sorted(levels.keys()):
                group = sorted(levels[level], key=lambda n: float(n["start"]))[: self.merge_count]
                if len(group) < self.merge_count:
                    continue
                summaries = []
                for n in group:
                    summaries.append("【第%d~%d天】\n%s" % (
                        _day_index(n["start"], self.state["epoch"]),
                        _day_index(n["end"], self.state["epoch"]),
                        str(n.get("summary") or "")))
                text = "\n\n".join(summaries)[:20000]
                summary = ""
                if self._summarize:
                    try:
                        summary = self._summarize(text, "merge") or ""
                    except Exception as e:
                        self._log("[mem] merge failed: %r" % (e,))
                        summary = ""
                if not summary:
                    continue
                start = min(float(n["start"]) for n in group)
                end = max(float(n["end"]) for n in group)
                count = sum(int(n.get("count") or 0) for n in group)
                new_node = self._new_node(level + 1, start, end, summary, count)
                for n in group:
                    n["archived"] = True
                    n["merged_into"] = new_node["id"]
                    self._archive_node(n)
                self._save_state()
                self._log("[mem] merged %d L%d -> L%d (day %d)"
                          % (len(group), level, level + 1, _day_index(end, self.state["epoch"])))
                merged = True
                break
            if not merged:
                break


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:120] or "chat"
