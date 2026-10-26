# -*- coding: utf-8 -*-
"""表情包发送记忆（结构化，独立于摘要记忆）。

记录：发给谁、md5、路径、标签、描述、场景/触发原话、日期、次数、是否 GIF 4帧、发送结果。
用途：
- 同联系人 + 同场景/标签命中 → 直接按 md5/路径复用，不再调用选图/读图 AI；
- 发送失败也记录，下次避开；
- 给摘要记忆提供“表情偏好”聚合（top_tags）。
"""
from __future__ import annotations

import json
import os
import threading
import time


def _date(ts=None):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts or time.time())))
    except Exception:
        return ""


class StickerMemory:
    def __init__(self, platform: str, root: str = None, shared: bool = False):
        self.platform = str(platform or "wechat").lower()
        self.shared = bool(shared)
        if root is None:
            from ..core.paths import platform_memory_dir
            root = platform_memory_dir("wechat" if self.shared else self.platform)
        self.root = root
        self.path = os.path.join(root, "stickers.jsonl")
        os.makedirs(root, exist_ok=True)
        self._lock = threading.RLock()

    # ---------------- 写 ----------------
    def record_send(self, contact="", md5="", path="", tags=None, desc="",
                    scene="", trigger_text="", send_ok=True, ext="",
                    is_gif4f=False) -> bool:
        md5 = str(md5 or "").lower()
        # 路径存成相对数据根的形式，整个目录换盘后仍能复用（读取时再拼回绝对路径）
        try:
            path = str(path or "")
            if path and os.path.isabs(path):
                from ..core.paths import data_home
                root = os.path.abspath(data_home())
                ap = os.path.abspath(path)
                if ap.lower().startswith((root + os.sep).lower()):
                    path = os.path.relpath(ap, root)
        except Exception:
            pass
        if not md5 and path:
            md5 = os.path.splitext(os.path.basename(str(path)))[0].lower()
        if not md5:
            return False
        rec = {
            "d": _date(), "t": time.time(), "platform": self.platform,
            "contact": str(contact or ""), "md5": md5, "path": str(path or ""),
            "ext": str(ext or os.path.splitext(str(path))[1]).lower(),
            "is_gif4f": bool(is_gif4f or str(ext).lower() in (".gif", ".webp")),
            "tags": [str(x) for x in (tags or []) if str(x).strip()][:8],
            "desc": str(desc or "")[:200], "scene": str(scene or "")[:200],
            "trigger_text": str(trigger_text or "")[:200],
            "send_ok": bool(send_ok),
        }
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                return True
            except Exception:
                return False

    # ---------------- 读 ----------------
    def all_records(self):
        out = []
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
        return out

    # ---------------- 反应反馈：喜欢/厌恶 ----------------
    def record_reaction(self, contact, md5, reaction):
        """reaction: like / dislike / neutral；dislike 永久禁用该表情。"""
        md5 = str(md5 or "").lower()
        if not md5:
            return False
        rec = {"kind": "reaction", "d": _date(), "t": time.time(),
               "platform": self.platform, "contact": str(contact or ""),
               "md5": md5, "reaction": str(reaction or "neutral")}
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                return True
            except Exception:
                return False

    def last_sent(self, contact=""):
        """该联系人最近一次发送成功的表情记录（send）。"""
        recs = [r for r in self.all_records()
                if r.get("kind", "send") == "send" and r.get("send_ok", True)
                and (not contact or r.get("contact") == contact)]
        recs.sort(key=lambda r: float(r.get("t") or 0), reverse=True)
        return recs[0] if recs else None

    def banned_md5(self, contact=""):
        """被明确表示厌恶的表情 md5（永久不再复用/发送）。"""
        out = set()
        for r in self.all_records():
            if r.get("kind") == "reaction" and r.get("reaction") == "dislike":
                if not contact or not r.get("contact") or r.get("contact") == contact:
                    out.add(str(r.get("md5") or "").lower())
        return {x for x in out if x}

    def reaction_counts(self, md5):
        likes = dislikes = 0
        for r in self.all_records():
            if r.get("kind") == "reaction" and str(r.get("md5") or "").lower() == str(md5 or "").lower():
                if r.get("reaction") == "like":
                    likes += 1
                elif r.get("reaction") == "dislike":
                    dislikes += 1
        return likes, dislikes

    def reuse_probability(self, contact=""):
        """快速路径命中概率：默认 3/4，随喜欢/讨厌调整，但不超过 4/5。"""
        likes = dislikes = 0
        for r in self.all_records():
            if r.get("kind") == "reaction" and (not contact or r.get("contact") == contact):
                if r.get("reaction") == "like":
                    likes += 1
                elif r.get("reaction") == "dislike":
                    dislikes += 1
        p = 0.75 + 0.01 * likes - 0.01 * dislikes
        return max(0.60, min(0.80, p))

    def find_reuse(self, contact="", tags=None, keywords=None, limit=3):
        """同联系人 + 标签/关键词命中 → 返回可直接复用的记录（按次数/日期排序）。"""
        want = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
        kws = [str(k).strip().lower() for k in (keywords or []) if str(k).strip()]
        banned = self.banned_md5(contact)
        agg = {}
        for rec in self.all_records():
            if rec.get("kind", "send") != "send":
                continue
            if not rec.get("send_ok", True):
                continue
            if str(rec.get("md5") or "").lower() in banned:
                continue
            if contact and rec.get("contact") and rec.get("contact") != contact:
                continue
            md5 = rec.get("md5")
            if not md5:
                continue
            it = agg.setdefault(md5, {"md5": md5, "path": rec.get("path") or "",
                                      "tags": [], "desc": "", "ext": rec.get("ext") or "",
                                      "is_gif4f": bool(rec.get("is_gif4f")),
                                      "used": 0, "last": "", "scene": ""})
            it["used"] += 1
            it["last"] = max(it["last"], rec.get("d") or "")
            if rec.get("path"):
                it["path"] = rec["path"]
            for t in rec.get("tags") or []:
                if t not in it["tags"]:
                    it["tags"].append(t)
            if rec.get("desc"):
                it["desc"] = rec["desc"]
            if rec.get("scene"):
                it["scene"] = rec["scene"]
        scored = []
        for it in agg.values():
            itags = [str(t).lower() for t in it["tags"]]
            blob = (it["desc"] or "").lower() + " " + (it["scene"] or "").lower()
            score = 0
            for w in want:
                if any(w in t or t in w for t in itags):
                    score += 3
                elif w in blob:
                    score += 1
            for w in kws:
                if w in blob:
                    score += 1
            if score > 0:
                scored.append((score, it["used"], it["last"], it))
        scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return [x[3] for x in scored[:limit]]

    def top_tags(self, contact="", limit=10):
        cnt = {}
        for rec in self.all_records():
            if not rec.get("send_ok", True):
                continue
            if contact and rec.get("contact") and rec.get("contact") != contact:
                continue
            for t in rec.get("tags") or []:
                cnt[t] = cnt.get(t, 0) + 1
        return [k for k, _ in sorted(cnt.items(), key=lambda kv: -kv[1])[:limit]]

    def stats(self):
        recs = self.all_records()
        ok = [r for r in recs if r.get("send_ok", True)]
        gif = [r for r in recs if r.get("is_gif4f")]
        return {"records": len(recs), "ok": len(ok), "failed": len(recs) - len(ok),
                "gif4f": len(gif), "unique_md5": len({r.get("md5") for r in recs}),
                "path": self.path}
