# -*- coding: utf-8 -*-
"""按人分开的「摘要即记忆」内核（ECHO 落地版 v2）。

需求（2026-10-26）：
- 每个联系人/群 **一个记忆文件**：memory/<平台>MM/memories/<chat>.md
- 每轮对话结束 **自动生成/更新摘要**（选择性记录：身份/偏好/事件/约定/承诺/关系/雷区/图片解读；
  不记寒暄废话），摘要就是 AI 读取的“真正记忆”
- 8000 字上限；超过时压缩到 4000 字（按配置）；编辑/清理都作用于摘要
- 原始对话按人存 logs/<chat>.jsonl（只用于短期上下文与门牌回查，不直接当记忆）
- 收到图片时把「图片解读」写进本轮摘要输入
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

PUNCT = " \t\r\n，。！？；：、,.!?;:()（）[]【】<>《》\"'“”‘’…—-·•*|"


def _safe(name: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(name or ""))
    return s[:120] or "chat"


def _now() -> float:
    return time.time()


def _date(ts=None) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts or _now())))
    except Exception:
        return ""


def _tokenize(text: str):
    t = str(text or "").lower()
    words = re.findall(r"[a-z0-9_]{2,}", t)
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(seg) <= 2:
            words.append(seg)
        else:
            words.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    out, seen = [], set()
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:64]


class ContactMemory:
    """单平台、按联系人分开的摘要记忆。root 缺省 memory/<wechatMM|QQMM>。"""

    def __init__(self, platform: str, root: str = None, log=None, shared: bool = False):
        self.platform = str(platform or "wechat").lower()
        # shared=True：QQ 与微信共用同一份记忆库（默认用微信记忆目录作为共享根，保留历史数据）
        self.shared = bool(shared)
        if root is None:
            from ..core.paths import platform_memory_dir
            root = platform_memory_dir("wechat" if self.shared else self.platform)
        self.root = root
        self.logs_dir = os.path.join(root, "logs")
        self.mem_dir = os.path.join(root, "memories")
        self.index_dir = os.path.join(root, "index")
        for d in (self.logs_dir, self.mem_dir, self.index_dir):
            os.makedirs(d, exist_ok=True)
        self._log = log or (lambda m: print("[memory]", m, flush=True))
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._contacts = None
        from .exchange_store import ExchangeStore
        self.exchange = ExchangeStore()
        try:
            self.migrate_shared()
        except Exception as e:
            self._log("migrate_shared failed: %r" % (e,))

    # ---------------- 基础 ----------------
    @property
    def state_path(self):
        return os.path.join(self.root, "state.json")

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except Exception:
            pass
        st = {"created": _now(), "turns": 0, "last_turn_at": 0.0}
        try:
            self._write_json(self.state_path, st)
        except Exception:
            pass
        return st

    @staticmethod
    def _write_json(path, data):
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def _save_state(self):
        try:
            self._state["heartbeat"] = _now()
            self._write_json(self.state_path, self._state)
        except Exception:
            pass

    def _read_text(self, path, limit=20000):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read(limit)
        except Exception:
            return ""

    def now_line(self):
        last = float(self._state.get("last_turn_at") or 0)
        delta = ""
        if last:
            sec = max(0, int(_now() - last))
            delta = ("距上轮 %d 分钟" % (sec // 60)) if sec >= 60 else ("距上轮 %d 秒" % sec)
        return "现在 %s（%s）" % (time.strftime("%Y-%m-%d %H:%M", time.localtime()), delta or "首次对话")

    # ---------------- 联系人名（备注优先→昵称）----------------
    def _contacts_map(self):
        if self._contacts is not None:
            return self._contacts
        self._contacts = {}
        try:
            from ..core.paths import data_dir
            path = os.path.join(data_dir(), "wechat_contacts.json")
            d = json.load(open(path, "r", encoding="utf-8"))
            for it in (d.get("friends") or []) + (d.get("groups") or []):
                cid = str(it.get("id") or "")
                if cid:
                    self._contacts[cid] = it
        except Exception:
            pass
        return self._contacts

    def display_name(self, chat):
        cid = str(chat or "").split(":", 1)[-1]
        it = self._contacts_map().get(cid) or {}
        name = (it.get("remark") or it.get("nick_name") or it.get("name") or "").strip()
        return name if name and name != cid else cid

    # ---------------- 墨 ----------------
    def ledger(self):
        if not hasattr(self, "_ledger") or self._ledger is None:
            from .ledger import Ledger
            if self.shared:
                self._ledger = Ledger.shared(self.root)
            else:
                self._ledger = Ledger(self.platform, root=self.root)
        return self._ledger

    def record(self, action, target="", detail=None):
        try:
            rec = dict(detail or {})
            return self.ledger().record(action, target, rec, platform=self.platform)
        except Exception:
            return 0

    def _gen(self, generator, **kw):
        """摘要/压缩不需要深度推理：优先关闭思考，兼容不支持该参数的生成器。"""
        try:
            return generator.generate(thinking="disabled", **kw) or ""
        except TypeError:
            try:
                return generator.generate(**kw) or ""
            except Exception:
                return ""

    # ---------------- L3 原始对话（按人）----------------
    def log_path(self, chat):
        return os.path.join(self.logs_dir, _safe(chat) + ".jsonl")

    def append(self, chat, role, text, sender="", ts=None):
        """完整交流记录写入 data/exchanges.jsonl（记忆只存摘要，不存轮次）。"""
        return self.exchange.append(self.platform, chat, self.display_name(chat),
                                    role, sender, text, ts)

    def entries(self, chat, since_seq=0, limit=100000):
        """从完整交流记录中取某会话的记录（用于近因上下文/调取）。"""
        recs = self.exchange.read(chat=chat, limit=limit + since_seq)
        out = []
        for i, rec in enumerate(recs):
            seq = i + 1
            if seq <= since_seq:
                continue
            out.append({"seq": seq, "d": rec.get("d") or "",
                        "role": rec.get("role") or "user",
                        "text": rec.get("text") or "",
                        "chat": rec.get("chat") or "",
                        "sender": rec.get("sender") or ""})
        return out

    def recent_context(self, chat, max_chars=4000):
        picked, used = [], 0
        for e in reversed(self.entries(chat)):
            txt = str(e.get("text") or "")
            cost = len(txt) + 8
            if used + cost > max_chars and picked:
                break
            picked.append({"role": e.get("role") or "user", "content": txt})
            used += cost
        picked.reverse()
        return picked

    # ---------------- 真记忆：按人摘要 ----------------
    def summary_path(self, chat):
        return os.path.join(self.mem_dir, _safe(chat) + ".md")

    def summary(self, chat):
        return self._read_text(self.summary_path(chat), 20000).strip()

    def write_summary(self, chat, text, generator=None, max_chars=8000, compress_to=4000,
                      audit: bool = True):
        text = str(text or "").strip()
        if len(text) > max_chars and generator is not None:
            text = self._compress_text(generator, text, compress_to)
        if len(text) > max_chars:
            text = text[:max_chars]
        tmp = self.summary_path(chat) + ".tmp"
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.summary_path(chat))
        if audit:
            self._audit_summary(chat, text)
        self.record("memory.summary_write", chat, {"chars": len(text)})
        return text

    def _audit_summary(self, chat, text):
        try:
            rec = {"id": int(self._state.get("summary_versions") or 0) + 1,
                   "chat": chat, "d": _date(), "text": text}
            self._state["summary_versions"] = rec["id"]
            self._save_state()
            with open(os.path.join(self.index_dir, "summary_audit.jsonl"), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _compress_text(self, generator, text, target):
        try:
            out = self._gen(
                generator,
                system_prompt=("把下面的『联系人记忆摘要』压缩到 %d 字以内：只保留身份/关系/偏好/"
                               "重要事件/计划/约定/承诺/雷区/图片要点；删掉重复与寒暄；"
                               "不要编造；直接输出压缩后的完整摘要。" % target),
                style_instruction="简洁", history=[],
                user_message=text) or ""
            out = out.strip()
            if out:
                return out
        except Exception as e:
            self._log("compress summary failed: %r" % (e,))
        return text

    def update_summary(self, chat, sender, user_text, reply_text, generator=None,
                       image_note="", sticker_note="", web_note="", name: str = "",
                       max_chars=8000, compress_to=4000):
        """对话结束后自动更新该联系人的摘要记忆（选择性记录，不记轮次流水）。"""
        old = self.summary(chat)
        name = str(name or self.display_name(chat) or chat)
        exchange = []
        if user_text:
            exchange.append("%s: %s" % (sender or name, user_text))
        if image_note:
            exchange.append("[图片解读] %s" % image_note)
        if reply_text:
            exchange.append("我: %s" % reply_text)
        if sticker_note:
            exchange.append("我发了一张表情包: %s" % sticker_note)
        if web_note:
            # 联网只记「查过什么」（查询词 + 来源 URL），不存网页全文
            exchange.append("[联网查询] %s" % web_note)
        transcript = "\n".join(exchange)
        new_text = ""
        if generator is not None:
            instr = (
                "维护该联系人的记忆：输入旧摘要 + 本轮对话，输出更新后的完整摘要（覆盖旧摘要）。\n"
                "选择性记录（宁缺毋滥）：身份/称呼/关系、偏好与雷区、重要事件/计划/约定/承诺/关键事实/图片解读；"
                "不记寒暄、重复、无信息量内容，不按轮次流水。\n"
                "联网只记一句「查过什么/关键事实（可附来源域名）」，不抄网页全文。\n"
                "不记「系统配图/我不能挑/我不能发」及「不能联网/查不到行情」等过时错误说法（已启用联网）。\n"
                "不用「本轮/本次对话」等时间指代，用事实陈述。条目式，可加小标题；"
                "总长不超过 %d 字；不编造；没有新信息就原样输出旧摘要。" % max_chars)
            try:
                new_text = self._gen(
                    generator,
                    system_prompt=instr, style_instruction="简洁", history=[],
                    user_message="【联系人】%s\n\n【旧记忆摘要】\n%s\n\n【本轮对话】\n%s" % (
                        name, old or "（无）", transcript)) or ""
            except Exception as e:
                self._log("update_summary ai failed: %r" % (e,))
        if not new_text.strip():
            # 摘要失败：保持旧摘要不变（绝不用轮次流水污染记忆）
            self.record("memory.summary_failed", chat, {})
            return old
        # 后处理：去掉时间指代词（本轮/本次对话…）
        for bad in ("本轮", "本回合", "本次对话", "这次对话", "该轮对话", "以上对话", "这轮对话"):
            new_text = new_text.replace(bad, "")
        new_text = "\n".join(ln for ln in new_text.splitlines() if ln.strip())
        return self.write_summary(chat, new_text, generator=generator,
                                  max_chars=max_chars, compress_to=compress_to)

    def build_context(self, chat, user_text=""):
        """返回 (注入 system 的记忆文本, 当前会话短期上下文 history)。"""
        secs = []
        summary = self.summary(chat)
        name = self.display_name(chat)
        if summary:
            secs.append("【关于「%s」的记忆（自动摘要，真正的记忆）】\n%s" % (name, summary))
        else:
            secs.append("【关于「%s」的记忆】\n（暂无，注意不要编造）" % name)
        secs.append("【当前时间】" + self.now_line())
        hist = self.recent_context(chat, max_chars=5000)
        return "\n\n".join(secs), hist

    def working_memory(self, chat, max_chars=8000):
        return self.summary(chat)[:max_chars]

    # ---------------- 观测 ----------------
    def list_chats(self):
        """返回 [(真实chat, 显示名, 交流条数, 摘要字数)]。"""
        out = []
        try:
            for chat, info in self.exchange.chats().items():
                out.append((chat, self.display_name(chat), int(info.get("count") or 0),
                            len(self.summary(chat))))
        except Exception:
            pass
        out.sort(key=lambda x: -x[2])
        return out

    def doctor(self):
        chats = self.list_chats()
        return {
            "platform": self.platform,
            "root": self.root,
            "turns": int(self._state.get("turns") or 0),
            "chats": len(chats),
            "lines": self.exchange.count(),
            "summaries": sum(1 for c in chats if c[3] > 0),
            "summary_chars": sum(c[3] for c in chats),
            "ledger": self.ledger().stats(),
            "exchanges": self.exchange.path,
        }

    def touch_turn(self):
        self._state["turns"] = int(self._state.get("turns") or 0) + 1
        self._state["last_turn_at"] = _now()
        self._save_state()

    # ---------------- 迁移：共享记忆 → 按人 ----------------
    def migrate_shared(self):
        """把旧的按会话日志/共享记忆一次性导入「完整交流记录」文件（留底，幂等）。"""
        marker = os.path.join(self.root, ".migrated_exchanges_v1")
        if os.path.isfile(marker):
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        legacy_dir = os.path.join(self.index_dir, "legacy_logs_" + ts)
        try:
            os.makedirs(legacy_dir, exist_ok=True)
        except Exception:
            pass
        imported = 0
        seen = set()
        try:
            names = [n for n in os.listdir(self.logs_dir) if n.endswith(".jsonl")]
        except Exception:
            names = []
        for name in names:
            path = os.path.join(self.logs_dir, name)
            chat_fallback = "wechat:" + name[:-6]
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        chat = str(rec.get("chat") or chat_fallback)
                        role = rec.get("role") or "user"
                        text = rec.get("text") or ""
                        key = (chat, role, text[:80], rec.get("d") or "")
                        if key in seen:
                            continue
                        seen.add(key)
                        self.exchange.append(self.platform, chat, self.display_name(chat),
                                             role, rec.get("sender") or "", text,
                                             rec.get("t"))
                        imported += 1
            except Exception:
                pass
            try:
                os.replace(path, os.path.join(legacy_dir, name))
            except Exception:
                pass
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write("migrated %s\n" % ts)
        except Exception:
            pass
        if imported:
            self.record("memory.migrate_exchanges", "data/exchanges.jsonl",
                        {"imported": imported})
            self._log("migrated %d records into exchanges" % imported)
