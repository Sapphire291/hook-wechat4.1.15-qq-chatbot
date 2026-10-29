# -*- coding: utf-8 -*-
"""Harness 内核实现（Local 进程内 + HTTP 外部服务两种）。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core.paths import data_dir, data_home, platform_memory_dir
from ..memory.chat_memory import ContactMemory

# 触发“回忆”的词语：命中时本轮强制注入记忆
_RECALL_WORDS = ("记得", "记不记得", "还记得", "之前", "上次", "以前", "说过",
                 "我跟你说", "回忆", "忘了", "忘了没")
# 兜底：即使没命中条件，也至少每 N 轮注入一次
_EVERY_N_TURNS = 8


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _bigrams(text) -> set:
    t = _norm(text)
    out = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        for i in range(max(0, len(seg) - 1)):
            out.add(seg[i:i + 2])
    for w in re.findall(r"[a-zA-Z0-9_]{3,}", t.lower()):
        out.add(w)
    return out


def _chat_token(platform: str, chat_id: str) -> str:
    return "%s:%s" % (platform, chat_id)


def resolve_memory_target(config, platform: str, chat_id: str):
    """返回 (memory_root_platform, memory_key, display_name, group_or_None)。

    白名单里用“打通记忆”按钮合并过的会话，共享同一个 memory key（同一个 ECHO 摘要/会话）；
    未合并的会话使用自己的 chat key，完全独立。
    """
    token = _chat_token(platform, chat_id)
    for g in (getattr(config, "memory_groups", []) or []):
        try:
            chats = [str(x) for x in (g.get("chats") or [])]
        except Exception:
            chats = []
        if token in chats:
            first = chats[0] if chats else token
            root_platform = first.split(":", 1)[0] or platform
            gid = str(g.get("id") or "0")
            return root_platform, "group:" + gid, str(g.get("name") or "合并会话"), g
    return platform, token, "", None


@dataclass
class HarnessReply:
    text: str = ""
    system_prompt: str = ""
    injected: bool = False
    memory_key: str = ""
    meta: dict = field(default_factory=dict)


class HarnessKernel:
    """harness 内核接口。"""

    def available(self) -> bool:
        raise NotImplementedError

    def warmup(self) -> None:
        """可选预热：App 启动时提前准备 harness（DSH 在 exe 启动时拉起）。"""
        return None

    def history(self, platform: str, chat_id: str) -> List[dict]:
        return []

    def reply(self, platform: str, chat_id: str, chat_name: str, user_text: str,
              system_prompt: str, style_instruction: str, history: Optional[List[dict]],
              thinking=None, reasoning_effort=None, max_tokens=None,
              images=None, generator=None) -> HarnessReply:
        raise NotImplementedError

    def record_turn(self, platform: str, chat_id: str, chat_name: str,
                    user_text: str, reply_text: str, sender: str = "",
                    generator=None, image_note: str = "", sticker_note: str = "",
                    web_note: str = "", max_chars: int = 8000,
                    compress_to: int = 4000, is_group: bool = False) -> None:
        raise NotImplementedError


class LocalHarness(HarnessKernel):
    """进程内 harness：用 ECHO/ContactMemory 保存会话与记忆，条件注入并直接调模型。"""

    def __init__(self, config):
        self.config = config
        self._mems: Dict[str, ContactMemory] = {}
        self._lock = threading.RLock()
        self._state_path = os.path.join(data_home(), "harness_state.json")
        self._state = self._load_state()

    # ---------------- state ----------------
    def _load_state(self) -> dict:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            p = self._state_path
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, p)
        except Exception:
            pass

    # ---------------- memory ----------------
    def available(self) -> bool:
        return bool(getattr(self.config, "harness_enabled", True))

    def _mem(self, platform: str) -> ContactMemory:
        with self._lock:
            mem = self._mems.get(platform)
            if mem is None:
                shared = bool(getattr(self.config, "memory_shared", False))
                mem = ContactMemory(platform, shared=shared)
                self._mems[platform] = mem
            return mem

    def _target(self, platform: str, chat_id: str):
        root, key, name, group = resolve_memory_target(self.config, platform, chat_id)
        mem = self._mem(root)
        if not name:
            name = mem.display_name(key)
        return mem, key, name, group

    def history(self, platform: str, chat_id: str) -> List[dict]:
        try:
            mem, key, _name, _g = self._target(platform, chat_id)
            return mem.recent_context(key, max_chars=5000)
        except Exception:
            return []

    # ---------------- injection policy ----------------
    @staticmethod
    def _keyword_hit(user_text: str, summary: str, name: str) -> bool:
        if not summary:
            return False
        u = _norm(user_text)
        if name and len(name) >= 2 and name in u:
            return True
        overlap = _bigrams(u) & _bigrams(summary)
        return len(overlap) >= 4

    @staticmethod
    def _recall_hit(user_text: str) -> bool:
        u = _norm(user_text)
        return any(w in u for w in _RECALL_WORDS)

    def _decide_inject(self, key: str, summary: str, user_text: str, name: str):
        st = self._state.get(key) or {}
        turns = int(st.get("turns") or 0)
        old_hash = str(st.get("hash") or "")
        new_hash = hashlib.sha1(summary.encode("utf-8")).hexdigest() if summary else ""
        if not summary:
            return False, new_hash
        first = not bool(old_hash)
        changed = old_hash != new_hash
        hit = self._keyword_hit(user_text, summary, name)
        recall = self._recall_hit(user_text)
        fallback = turns > 0 and turns % _EVERY_N_TURNS == 0
        return bool(first or changed or hit or recall or fallback), new_hash

    # ---------------- main ----------------
    def reply(self, platform: str, chat_id: str, chat_name: str, user_text: str,
              system_prompt: str, style_instruction: str, history: Optional[List[dict]],
              thinking=None, reasoning_effort=None, max_tokens=None,
              images=None, generator=None) -> HarnessReply:
        mem, key, name, _group = self._target(platform, chat_id)
        summary = ""
        try:
            summary = mem.summary(key)
        except Exception:
            summary = ""
        inject, new_hash = self._decide_inject(key, summary, user_text, name)
        sys_prompt = str(system_prompt or "").strip()
        if inject:
            sys_prompt += "\n\n【关于「%s」的记忆（自动摘要，真正的记忆）】\n%s" % (name, summary)
        try:
            sys_prompt += "\n\n【当前时间】" + mem.now_line()
        except Exception:
            pass
        # 落盘当前真实 system（供排查/审计）
        try:
            with open(os.path.join(data_dir(), "last_system_prompt.txt"), "w", encoding="utf-8") as f:
                f.write(sys_prompt)
        except Exception:
            pass
        text = generator_generate(generator, sys_prompt, style_instruction, history,
                                  user_text, images, thinking, reasoning_effort, max_tokens)
        st = dict(self._state.get(key) or {})
        st["turns"] = int(st.get("turns") or 0) + 1
        st["hash"] = new_hash
        st["injected"] = bool(inject)
        st["updated"] = time.time()
        with self._lock:
            self._state[key] = st
            self._save_state()
        return HarnessReply(text=text or "", system_prompt=sys_prompt,
                            injected=bool(inject), memory_key=key,
                            meta={"name": name, "group": bool(_group)})

    def _generator(self, *_a, **_k):
        raise RuntimeError("generator not bound")

    def bind_generator(self, generator):
        self._cur_generator = generator

    def record_turn(self, platform: str, chat_id: str, chat_name: str,
                    user_text: str, reply_text: str, sender: str = "",
                    generator=None, image_note: str = "", sticker_note: str = "",
                    web_note: str = "", max_chars: int = 8000,
                    compress_to: int = 4000, is_group: bool = False) -> None:
        mem, key, name, _group = self._target(platform, chat_id)
        now_ts = time.time()
        if not sender:
            sender = (chat_name or "").strip() or name
        fmt = []
        try:
            mem.append(key, "user", user_text, sender=sender, ts=now_ts)
            if image_note:
                mem.append(key, "user", "[图片] " + image_note, sender="图片", ts=now_ts)
            mem.append(key, "assistant", reply_text, sender="我", ts=now_ts)
            mem.touch_turn()
            mem.update_summary(
                key, sender, user_text, reply_text, generator,
                image_note=image_note, sticker_note=sticker_note,
                web_note=str(web_note or ""), name=name,
                max_chars=int(max_chars or 8000), compress_to=int(compress_to or 4000))
            mem.record("reply.text", str(chat_id), {"chars": len(reply_text or ""), "chat": chat_name})
        except Exception:
            raise


def generator_generate(generator, system_prompt, style_instruction, history, user_message,
                       images=None, thinking=None, reasoning_effort=None, max_tokens=None):
    """统一调用生成器，兼容不支持 thinking/max_tokens 的旧接口。"""
    if generator is None:
        return ""
    kwargs = dict(system_prompt=system_prompt, style_instruction=style_instruction,
                  history=history or [], user_message=user_message)
    if images:
        kwargs["images"] = images
    try:
        return generator.generate(thinking=thinking, reasoning_effort=reasoning_effort,
                                  max_tokens=max_tokens, **kwargs) or ""
    except TypeError:
        return generator.generate(**kwargs) or ""


class HttpHarness(HarnessKernel):
    """外部 harness 服务（预留协议）。

    POST {url}/chat:
      {"platform","chat_id","chat_name","user_text","system_prompt","style_instruction",
       "history","thinking","reasoning_effort","max_tokens"}
      -> {"reply": "...", "system_prompt": "...", "injected": true/false, "memory_key": "..."}
    POST {url}/record: 同构的落库请求（无返回要求）
    """

    def __init__(self, config):
        self.config = config
        self.url = str(getattr(config, "harness_url", "") or "").rstrip("/")
        self.timeout = int(getattr(config, "harness_timeout", 30) or 30)

    def available(self) -> bool:
        return bool(getattr(self.config, "harness_enabled", True) and self.url)

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def reply(self, platform, chat_id, chat_name, user_text, system_prompt,
              style_instruction, history, thinking=None, reasoning_effort=None,
              max_tokens=None, images=None, generator=None) -> HarnessReply:
        data = self._post("/chat", {
            "platform": platform, "chat_id": chat_id, "chat_name": chat_name,
            "user_text": user_text, "system_prompt": system_prompt,
            "style_instruction": style_instruction, "history": history or [],
            "thinking": thinking, "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens})
        return HarnessReply(text=str(data.get("reply") or ""),
                            system_prompt=str(data.get("system_prompt") or system_prompt),
                            injected=bool(data.get("injected")),
                            memory_key=str(data.get("memory_key") or ""),
                            meta=dict(data.get("meta") or {}))

    def history(self, platform, chat_id):
        try:
            data = self._post("/history", {"platform": platform, "chat_id": chat_id})
            return list(data.get("history") or [])
        except Exception:
            return []

    def record_turn(self, platform, chat_id, chat_name, user_text, reply_text,
                    sender="", generator=None, image_note="", sticker_note="",
                    web_note="", max_chars=8000, compress_to=4000,
                    is_group: bool = False) -> None:
        self._post("/record", {
            "platform": platform, "chat_id": chat_id, "chat_name": chat_name,
            "user_text": user_text, "reply_text": reply_text, "sender": sender,
            "image_note": image_note, "sticker_note": sticker_note,
            "web_note": web_note, "is_group": bool(is_group)})


def create_harness(config):
    """按配置创建 harness。

    - harness_enabled=false -> None（走旧路径）
    - harness_backend=native -> 内置原生 DSH 兼容核（默认，无外部依赖）
    - harness_url 非空 -> HTTP harness
    - 其余 -> 项目内 LocalHarness
    """
    if not bool(getattr(config, "harness_enabled", False)):
        return None
    backend = str(getattr(config, "harness_backend", "native") or "native").strip().lower()
    if backend == "native":
        try:
            from .native import NativeHarness
            return NativeHarness(config)
        except Exception as exc:
            print("[harness] native backend init failed, fallback local: %r" % (exc,), flush=True)
    url = str(getattr(config, "harness_url", "") or "").strip()
    if url:
        return HttpHarness(config)
    return LocalHarness(config)
